from __future__ import annotations

import asyncio
import logging
import async_timeout
from datetime import datetime, timedelta, timezone

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from bleak import BleakError, BleakClient, BleakScanner
from .laifen import Laifen
from .const import CONF_LOW_TRAFFIC_BATTERY, DEVICE_TIMEOUT, DOMAIN, UPDATE_SECONDS
from .low_traffic import (
    PATCH_ID,
    LowTrafficMonitor,
    advertisement_should_connect,
    production_config,
)
from .models import LaifenData, DEVICE_REGISTRY, DEVICE_SIGNAL

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.SELECT, Platform.BINARY_SENSOR]
_LOGGER = logging.getLogger(__name__)

class MockBLEDevice:
    def __init__(self, address):
        self.address = address
        self.name = "Laifen"

LAST_KNOWN_STORE_VERSION = 1
LAST_KNOWN_FILENAME = f"{DOMAIN}_last_known.json"

class LaifenCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        laifen: Laifen,
        device_address: str,
        *,
        low_traffic: bool = False,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None if low_traffic else timedelta(seconds=UPDATE_SECONDS),
        )
        self.laifen = laifen
        self.device_address = device_address
        self.lock = asyncio.Lock()
        self._first_message = True
        self.device_asleep = False
        self._reconnecting = asyncio.Lock()
        self._store = Store(hass, LAST_KNOWN_STORE_VERSION, LAST_KNOWN_FILENAME)
        # Default False. Only the options checkbox for this entry sets it.
        self.low_traffic = low_traffic
        self.monitor: LowTrafficMonitor | None = None


    async def _async_update_data(self):
        if self.low_traffic:
            # Keep entity state stable if something still asks for a refresh.
            # Do not call _aggressive_reconnect from this path.
            if self.laifen and self.laifen.result:
                return self.laifen.result
            restored = await self._async_restore_data()
            return restored or {}

        if self.device_asleep:
            _LOGGER.debug(f"{self.device_address} is asleep. Skipping update.")
            restored = await self._async_restore_data()
            self.async_set_updated_data(restored or {})
            return restored or {}
        
        if not self.laifen:
            _LOGGER.debug(f"{self.device_address}: coordinator has no device yet; restoring cached data.")
            restored = await self._async_restore_data()
            self.async_set_updated_data(restored or {})
            return restored or {}


        try:
            # Add timeout for the entire operation
            async with async_timeout.timeout(30):
                # V2 Pro (Wave Pro) is local_push and streams its full status
                # via notifications (periodic 0xC1-03 broadcasts plus
                # change-triggered 0x81-03 updates). While brushing, it also
                # streams high-frequency 0x82-0x0C/0x82-0x0D telemetry.
                # Issuing an additional GATT *read* every UPDATE_SECONDS on
                # top of that flood was found to overwhelm the connection
                # during brushing, causing repeated disconnects/reconnects
                # and dropped writes (e.g. power-off not taking effect).
                # For V2 Pro, skip the read entirely and rely purely on
                # push notifications — just check connection health below.
                if self.laifen._proto_version != "v2pro":
                    await self.laifen.gatherdata()

                if self.laifen.result:
                    # Always check connection, even if not Running
                    if not self.laifen.client or not self.laifen.client.is_connected:
                        _LOGGER.debug(f"{self.device_address} appears disconnected — attempting immediate reconnect.")
                        if not await self.laifen._aggressive_reconnect(max_attempts=5):
                            _LOGGER.warning("Reconnection failed for %s, marking asleep", self.device_address)
                            self.device_asleep = True
                            return await self._async_restore_data() or {}

                    await self._async_store_data(self.laifen.result)
                    return self.laifen.result

                else:
                    # No new data - use cached but don't mark as asleep yet
                    cached = await self._async_restore_data()
                    if cached:
                        self.async_set_updated_data(cached)
                        return cached
                    
                    # Only mark as asleep if we have no data at all
                    self.device_asleep = True
                    cached = await self._async_restore_data()
                    self.async_set_updated_data(cached or {})
                    return cached or {}

                    
        except (BleakError, asyncio.TimeoutError) as e:
            _LOGGER.debug(f"BLE error during update: {e}. Will retry before marking asleep.")
            # Don't immediately mark as asleep - will happen automatically if retries fail
            cached = await self._async_restore_data()
            self.async_set_updated_data(cached or {})
            return cached or {}

    async def _async_store_data(self, data: dict):
        all_data = await self._store.async_load() or {}
        all_data[self.device_address] = data
        await self._store.async_save(all_data)

    async def _async_restore_data(self) -> dict | None:
        all_data = await self._store.async_load() or {}
        return all_data.get(self.device_address)

    @callback
    def async_handle_notification(self, data):
        # Keep entities live and also persist for future sleeps/restarts
        self.async_set_updated_data(data)
        # Persist in background
        self.hass.async_create_task(self._async_store_data(data))


    async def async_config_entry_first_refresh(self):
        max_attempts = 10
        attempts = 0
        while attempts < max_attempts:
            try:
                await self.async_refresh()
                break
            except UpdateFailed:
                attempts += 1
                # _LOGGER.debug("Device not reachable, retrying in 30 seconds...")
                await asyncio.sleep(30)


def _entry_low_traffic(entry: ConfigEntry) -> bool:
    return bool(entry.options.get(CONF_LOW_TRAFFIC_BATTERY, False))


def _arm_low_traffic(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: LaifenCoordinator,
    laifen: Laifen,
    restored: dict | None,
) -> None:
    """Start the slow battery timer. Does not connect and does not scan."""

    now = datetime.now(timezone.utc)
    source = laifen.result or restored or {}
    status = source.get("status") if isinstance(source, dict) else None
    link_up = bool(laifen.client and getattr(laifen.client, "is_connected", False))
    monitor = LowTrafficMonitor(
        address=laifen.address,
        device=laifen,
        coordinator=coordinator,
        config=production_config(),
        now=now,
    )
    snapshots = hass.data[DOMAIN].setdefault("low_traffic_snapshots", {})
    snapshot = getattr(laifen, "low_traffic_snapshot", None) or snapshots.get(laifen.address)
    if snapshot:
        monitor.policy.restore(snapshot, now, link_up=link_up)
        _LOGGER.info(
            "%s low-traffic schedule restored for %s; next BLE attempt %s (status=%s)",
            PATCH_ID,
            laifen.address,
            monitor.policy.next_attempt_at.isoformat(),
            monitor.policy.last_status,
        )
    else:
        monitor.policy.note_startup_cache(status, now, link_up=link_up)
        _LOGGER.info(
            "%s low-traffic battery mode on for %s; no BLE connect until %s "
            "(startup status=%s). Default one-second polling stays off for this entry only.",
            PATCH_ID,
            laifen.address,
            monitor.policy.next_attempt_at.isoformat(),
            status,
        )
    monitor.attach(async_track_time_interval, hass)
    coordinator.monitor = monitor
    coordinator.low_traffic = True
    entry.async_on_unload(monitor.detach)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Laifen for multiple devices, restoring stored devices and ensuring passive Bluetooth detection."""

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})  # don't overwrite on reload
    entry_data = hass.data[DOMAIN][entry.entry_id]
    laifens = hass.data[DOMAIN].setdefault("laifens", [])

    stored_devices = entry_data
    addresses = entry.data.get("devices", []) or list(stored_devices.keys())

    if not addresses:
        _LOGGER.warning("No Laifen devices found. Setup aborted until a device is added.")
        raise ConfigEntryNotReady

    ble_devices = []
    for addr in addresses:
        device = bluetooth.async_ble_device_from_address(hass, addr.upper(), True)
        if not device:
            _LOGGER.debug(f"[Startup] BLE device {addr} not found (likely sleeping). Will restore passively.")
        ble_devices.append(device)

    low_traffic = _entry_low_traffic(entry)
    held_links = hass.data[DOMAIN].setdefault("held_links", {})

    for addr, ble_device in zip(addresses, ble_devices):
        restored = None  # ✅ Always define restored
        if not low_traffic:
            # The option was on and a Running link was left up. Normal mode
            # always disconnects, so drop that held client before building
            # a new one. Otherwise two clients would target one brush.
            abandoned = held_links.pop(addr, None)
            if abandoned is not None:
                _LOGGER.info(
                    "Low-traffic mode is off for %s; disconnecting the link it was holding",
                    addr,
                )
                try:
                    await abandoned.disconnect()
                except Exception as err:
                    _LOGGER.debug("Disconnect of held link %s failed: %s", addr, err)
        parked = held_links.pop(addr, None) if low_traffic else None

        if parked is not None:
            # Integration reload left this object connected on purpose.
            # Reuse it instead of constructing a second client.
            laifen = parked
            coordinator = LaifenCoordinator(hass, laifen, addr, low_traffic=True)
            laifen.coordinator = coordinator
            entry_data[addr] = LaifenData(entry.title, laifen, coordinator)
            DEVICE_REGISTRY.setdefault(entry.entry_id, {})[addr] = entry_data[addr]
            async_dispatcher_send(hass, f"{DEVICE_SIGNAL}_{entry.entry_id}_{addr}")
            restored = laifen.result or await coordinator._async_restore_data()
            if restored:
                coordinator.data = restored
                coordinator.async_set_updated_data(restored)
                laifen.result = restored
            has_cached_data = restored is not None
        elif addr in entry_data and isinstance(entry_data[addr], LaifenData):
            # Device already initialized
            laifen = entry_data[addr].device
            coordinator = entry_data[addr].coordinator
            coordinator.low_traffic = low_traffic
            if low_traffic:
                coordinator.update_interval = None
            has_cached_data = True  # Because we have previous data for this device
            restored = await coordinator._async_restore_data()
            _LOGGER.debug(f"Restored Laifen {addr} from previous data.")
        else:
            # First time initialization
            coordinator = LaifenCoordinator(hass, None, addr, low_traffic=low_traffic)
            laifen = Laifen(ble_device or MockBLEDevice(addr), coordinator)
            coordinator.laifen = laifen
            entry_data[addr] = LaifenData(entry.title, laifen, coordinator)
            DEVICE_REGISTRY.setdefault(entry.entry_id, {})[addr] = entry_data[addr]
            async_dispatcher_send(hass, f"{DEVICE_SIGNAL}_{entry.entry_id}_{addr}")

            restored = await coordinator._async_restore_data()
            has_cached_data = restored is not None

            if has_cached_data:
                coordinator.data = restored
                coordinator.async_set_updated_data(restored)
                laifen.result = restored
                _LOGGER.warning(f"Restored Laifen {addr} from saved state.")
            else:
                laifen.result = {}
                _LOGGER.warning(f"Initialized new Laifen {addr}.")


        laifens.append(laifen)
        laifen.device_asleep = False

        if low_traffic:
            _arm_low_traffic(hass, entry, coordinator, laifen, restored if isinstance(restored, dict) else None)
            continue

        if ble_device and await laifen.connect():
            await laifen.start_notifications()
            await coordinator.async_request_refresh()
        elif restored:
            # _LOGGER.warning(f"Device {addr} is sleeping. Restoring last known values.")
            coordinator.data = restored
            coordinator.async_set_updated_data(restored)
        else:
            _LOGGER.debug(f"Device {addr} is unavailable and no cached data found. Entities may state unavailable.")

    # ✅ Register Passive Bluetooth Callbacks for Wake-Ups and Data Updates
    for addr in addresses:
        entry.async_on_unload(
            bluetooth.async_register_callback(
                hass,
                lambda service_info, change: asyncio.create_task(_async_device_recovery(hass, entry, service_info)),
                BluetoothCallbackMatcher({ADDRESS: addr}),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )

    return True


async def _async_stop(hass: HomeAssistant, event: Event) -> None:
    """Close connections on shutdown.

    An opted-in entry whose last status is Running does not get an
    explicit disconnect here. The process exit still drops the socket;
    skipping the call avoids adding a motor-stop disconnect on top.
    """
    domain = hass.data.get(DOMAIN, {})
    laifens = domain.get("laifens", [])
    held = domain.setdefault("held_links", {})
    snapshots = domain.setdefault("low_traffic_snapshots", {})
    seen: set[int] = set()
    for laifen in laifens:
        if laifen is None or id(laifen) in seen:
            continue
        seen.add(id(laifen))
        monitor = getattr(getattr(laifen, "coordinator", None), "monitor", None)
        if monitor is not None:
            await monitor.async_unload(held, snapshots)
            continue
        await laifen.stop_notifications()
        await laifen.disconnect()

    # _LOGGER.warning("Laifen devices successfully disconnected.")


async def _async_device_recovery(hass: HomeAssistant, entry: ConfigEntry, service_info):
    """Recover Laifen devices when they wake up via passive Bluetooth events."""

    device_address = service_info.device.address
    _LOGGER.debug(f"Bluetooth recovery callback fired for {device_address}")

    if not advertisement_should_connect(_entry_low_traffic(entry), False):
        # Advertisements are not a battery source. Connecting on each one
        # is the recovery loop low-traffic mode is meant to stop.
        _LOGGER.debug(
            "Low-traffic mode: ignoring advertisement from %s (no connect)",
            device_address,
        )
        return

    entry_devices: dict[str, LaifenData] = hass.data[DOMAIN][entry.entry_id]

    if device_address in entry_devices:
        _LOGGER.debug(f"Laifen {device_address} detected via Bluetooth callback! Restoring connection...")

        laifen_data = entry_devices.get(device_address)
        if not isinstance(laifen_data, LaifenData):
            # _LOGGER.debug(f"Device {device_address} is stored incorrectly. Expected LaifenData but got {type(laifen_data)}.")
            return

        laifen = laifen_data.device
        # laifen.coordinator = laifen_data.coordinator  # ✅ ensure linked
        coordinator = laifen_data.coordinator  # ✅ ensure linked

        if laifen.client and laifen.client.is_connected:
            _LOGGER.debug(f"{device_address} is already connected. Skipping recovery.")
            return

        _LOGGER.debug(f"Old device for {device_address}: {laifen.ble_device}")
        await laifen.set_ble_device(service_info.device)
        _LOGGER.debug(f"Updated device for {device_address}: {laifen.ble_device}")

        # If the passive scan gave an incomplete device, refresh it from a full scan
        if not getattr(service_info.device, "details", None):
            devices = await BleakScanner.discover()
            for dev in devices:
                if dev.address.lower() == device_address.lower():
                    await laifen.set_ble_device(dev)
                    break

        # Force new BleakClient to avoid stale connection object
        laifen.client = BleakClient(laifen.ble_device)

        
        if await laifen.connect():
            await laifen.start_notifications()
            await laifen.coordinator.async_request_refresh()
            _LOGGER.debug(f"Successfully reconnected to {device_address}.")
        else:
            _LOGGER.debug(f"Failed to reconnect {device_address}. Retrying on next callback event.")
    else:
        # ✅ Do not register new devices here
        _LOGGER.debug(f"Laifen {device_address} detected but not found in registered devices. Skipping recovery.")


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload this entry after its options change.

    Reloading rebuilds the coordinator interval and the low-traffic timer.
    It reloads only this config entry, not Home Assistant Core and not the
    other toothbrush entry. Saving the form while status is Running can
    still drop a link at reload time; do it while the brush is idle.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and disconnect devices that are safe to drop."""
    domain = hass.data[DOMAIN]
    entry_devices = domain.get(entry.entry_id, {})
    held = domain.setdefault("held_links", {})
    snapshots = domain.setdefault("low_traffic_snapshots", {})
    for addr, data in entry_devices.items():
        if isinstance(data, LaifenData):
            monitor = getattr(data.coordinator, "monitor", None)
            if monitor is not None:
                await monitor.async_unload(held, snapshots)
                continue
            try:
                await data.device.stop_notifications()
                await data.device.disconnect()
                # _LOGGER.debug(f"Disconnected Laifen device {addr}")
            except Exception as e:
                _LOGGER.debug(f"Error disconnecting {addr}: {e}")

    domain.pop(entry.entry_id, None)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

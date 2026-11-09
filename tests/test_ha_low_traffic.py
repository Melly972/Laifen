"""Smoke tests against Home Assistant 2025.1.4.

Bluetooth hardware is not used. Adapter discovery is the test plugin's
mock, device lookup is stubbed, and ``establish_connection`` is replaced
so no socket is opened. The config flow, coordinator, setup, and sensor
modules under test are the real ones.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.components import bluetooth, usb
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

import custom_components.laifen_ble as integration
from custom_components.laifen_ble import config_flow, sensor
from custom_components.laifen_ble.const import (
    CONF_LOW_TRAFFIC_BATTERY,
    DOMAIN,
    UPDATE_SECONDS,
)
from custom_components.laifen_ble.laifen import laifen as laifen_mod
from custom_components.laifen_ble.models import LaifenData

MAC_CELIAN = "AA:BB:CC:DD:EE:11"
MAC_ELEA = "AA:BB:CC:DD:EE:22"
CHAR_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

assert config_flow.LaifenOptionsFlow
assert sensor.LaifenSensor
assert integration.LaifenCoordinator


def _status_packet(battery: int, running: bool) -> bytes:
    payload = bytearray(36)
    payload[2] = battery
    payload[13] = 1 if running else 0
    return bytes([0x5A, 0xC1, 0x03, 0x00, 0x00, 36]) + bytes(payload) + bytes([0x00])


class _Char:
    handle = 16
    properties = ["notify", "read"]
    service_uuid = "0000ff01-0000-1000-8000-00805f9b34fb"


class _Service:
    uuid = "0000ff01-0000-1000-8000-00805f9b34fb"
    characteristics = [_Char()]


class _Services:
    def get_characteristic(self, _uuid):
        return _Char()

    def __iter__(self):
        return iter([_Service()])


class _Client:
    def __init__(self, address: str, packet: bytes | None, sink: dict):
        self.address = address
        self.is_connected = False
        self.services = _Services()
        self._packet = packet
        self._sink = sink
        self._on_disconnect = None

    def set_disconnected_callback(self, callback):
        self._on_disconnect = callback

    async def start_notify(self, uuid, callback):
        self._sink["notifies"].append((self.address, uuid))
        if self._packet is not None:
            callback(0, self._packet)

    async def stop_notify(self, uuid):
        self._sink["stops"].append((self.address, uuid))

    async def read_gatt_char(self, uuid):
        self._sink["reads"].append((self.address, uuid))
        return b""

    async def write_gatt_char(self, *args, **kwargs):
        self._sink["writes"].append(self.address)
        raise AssertionError(f"unexpected GATT write to {self.address}")

    async def disconnect(self):
        self._sink["disconnects"].append(self.address)
        self.is_connected = False
        if self._on_disconnect is not None:
            self._on_disconnect(self)


class _BLE:
    def __init__(self, address: str):
        self.address = address
        self.name = "LFTB02-S-TEST"
        self.details = {"path": "test"}


@pytest.fixture
def ble(monkeypatch):
    """Stub adapter lookup and connection. Records every GATT operation."""

    sink = {
        "establish": [],
        "discover": [],
        "notifies": [],
        "stops": [],
        "reads": [],
        "writes": [],
        "disconnects": [],
        "callbacks": [],
        "timer_unsubs": [],
        "timers": [],
        "clients": [],
        "packets": {},
    }
    devices = {
        MAC_CELIAN: _BLE(MAC_CELIAN),
        MAC_ELEA: _BLE(MAC_ELEA),
    }

    def lookup(_hass, address, connectable=True):
        return devices.get(address.upper())

    def register(_hass, callback, _matcher, _mode):
        sink["callbacks"].append(callback)
        return lambda: None

    async def establish(_client_cls, device, _name, disconnected_callback=None, **kwargs):
        sink["establish"].append((device.address, kwargs.get("max_attempts")))
        client = _Client(device.address, sink["packets"].get(device.address), sink)
        client.is_connected = True
        client.set_disconnected_callback(disconnected_callback)
        sink["clients"].append(client)
        return client

    async def discover(*_args, **_kwargs):
        sink["discover"].append(1)
        return []

    real_track = integration.async_track_time_interval

    def track(hass, action, interval):
        sink["timers"].append((action, interval))
        unsub = real_track(hass, action, interval)

        def tracked():
            sink["timer_unsubs"].append(interval)
            unsub()

        return tracked

    async def no_usb_monitor(self):
        return None

    monkeypatch.setattr(usb.USBDiscovery, "_async_start_monitor", no_usb_monitor)
    monkeypatch.setattr(bluetooth, "async_ble_device_from_address", lookup)
    monkeypatch.setattr(bluetooth, "async_register_callback", register)
    monkeypatch.setattr(laifen_mod, "establish_connection", establish)
    monkeypatch.setattr(laifen_mod, "BleakClient", lambda device, **_k: _Client(device.address, None, sink))
    monkeypatch.setattr(laifen_mod.BleakScanner, "discover", discover)
    monkeypatch.setattr(integration, "async_track_time_interval", track)
    yield sink
    # Drop live clients before Home Assistant unloads entries. A default
    # entry's disconnect callback would otherwise leave _aggressive_reconnect
    # running after the test loop is gone.
    for client in sink["clients"]:
        client.is_connected = False
        client._on_disconnect = None


def _entry(title: str, mac: str, low_traffic: bool):
    options = {CONF_LOW_TRAFFIC_BATTERY: True} if low_traffic else {}
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={"devices": [mac]},
        options=options,
        unique_id=mac,
    )


def _coordinator(hass, entry, mac: str):
    data = hass.data[DOMAIN][entry.entry_id][mac]
    assert isinstance(data, LaifenData)
    return data.coordinator


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("expected", ["2025.1.4"])
async def test_home_assistant_version(expected):
    assert HA_VERSION == expected


async def test_default_entry_connects_and_low_traffic_entry_does_not(
    hass, enable_custom_integrations, mock_bluetooth, ble
):
    """Eléa keeps the 3.0.4 session. Célian does not connect at startup."""

    elea = _entry("Eléa", MAC_ELEA, False)
    celian = _entry("Célian", MAC_CELIAN, True)
    await _setup(hass, elea)
    await _setup(hass, celian)

    assert ble["establish"] == [(MAC_ELEA, 10)]
    assert ble["discover"] == []
    assert MAC_CELIAN not in {item[0] for item in ble["notifies"]}
    assert MAC_CELIAN not in {item[0] for item in ble["reads"]}
    assert ble["writes"] == []

    elea_coord = _coordinator(hass, elea, MAC_ELEA)
    celian_coord = _coordinator(hass, celian, MAC_CELIAN)
    assert elea_coord.low_traffic is False
    assert elea_coord.update_interval == timedelta(seconds=UPDATE_SECONDS)
    assert elea_coord.monitor is None
    assert celian_coord.low_traffic is True
    assert celian_coord.update_interval is None
    assert celian_coord.monitor is not None

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done()
    await celian_coord.async_request_refresh()
    await hass.async_block_till_done()

    info = SimpleNamespace(device=SimpleNamespace(address=MAC_CELIAN))
    assert ble["callbacks"]
    for callback in ble["callbacks"]:
        callback(info, None)
    await hass.async_block_till_done()

    assert ble["establish"] == [(MAC_ELEA, 10)]
    assert ble["discover"] == []
    assert all(address != MAC_CELIAN for address, _uuid in ble["reads"])
    assert all(address != MAC_CELIAN for address, _uuid in ble["notifies"])
    assert ble["writes"] == []


async def test_low_traffic_notification_reaches_the_battery_entity(
    hass, enable_custom_integrations, mock_bluetooth, ble
):
    """One status notification updates the existing battery entity.

    The packet is injected by the test double. This does not show that the
    toothbrush emits it, and it does not show that a GATT read would.
    """

    ble["packets"][MAC_CELIAN] = _status_packet(battery=21, running=False)
    entry = _entry("Célian", MAC_CELIAN, True)
    await _setup(hass, entry)
    assert ble["establish"] == []
    low_traffic_timers = [
        action
        for action, interval in ble["timers"]
        if interval == timedelta(seconds=60)
    ]
    assert low_traffic_timers
    # The interval listener reads dt_util.utcnow(), which this jump does not
    # move. Invoke the registered callback with a timestamp after the
    # 15-minute startup hold.
    await low_traffic_timers[-1](dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()

    assert ble["establish"] == [(MAC_CELIAN, 1)]
    assert ble["discover"] == []
    assert ble["reads"] == []
    assert ble["writes"] == []
    assert ble["notifies"] == [(MAC_CELIAN, CHAR_UUID)]
    assert ble["stops"]
    assert {item[1] for item in ble["stops"]} == {CHAR_UUID}
    assert ble["disconnects"] == [MAC_CELIAN]

    registry = er.async_get(hass)
    batteries = [
        item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.unique_id == f"{MAC_CELIAN}_battery_level"
    ]
    assert len(batteries) == 1
    state = hass.states.get(batteries[0].entity_id)
    assert state is not None
    assert state.state == "21"
    assert state.attributes.get("unit_of_measurement") == "%"


async def test_options_flow_reload_returns_the_entry_to_default(
    hass, enable_custom_integrations, mock_bluetooth, ble
):
    """Saving the checkbox off reloads only that entry back onto the 1s path."""

    entry = _entry("Célian", MAC_CELIAN, True)
    await _setup(hass, entry)
    assert _coordinator(hass, entry, MAC_CELIAN).low_traffic is True
    assert ble["establish"] == []

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_LOW_TRAFFIC_BATTERY: False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options.get(CONF_LOW_TRAFFIC_BATTERY, False) is False
    coordinator = _coordinator(hass, entry, MAC_CELIAN)
    assert coordinator.low_traffic is False
    assert coordinator.update_interval == timedelta(seconds=UPDATE_SECONDS)
    assert coordinator.monitor is None
    assert ble["establish"] == [(MAC_CELIAN, 10)]
    assert ble["timer_unsubs"]
    assert ble["writes"] == []


async def test_reloading_low_traffic_cancels_the_timer_and_stays_quiet(
    hass, enable_custom_integrations, mock_bluetooth, ble
):
    entry = _entry("Célian", MAC_CELIAN, True)
    await _setup(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = _coordinator(hass, entry, MAC_CELIAN)
    assert coordinator.low_traffic is True
    assert coordinator.monitor is not None
    assert ble["establish"] == []
    assert ble["discover"] == []
    assert ble["reads"] == []
    assert ble["notifies"] == []
    assert ble["timer_unsubs"]
    assert ble["writes"] == []

"""Optional per-entry low-traffic battery monitoring.

The default integration polls about once a second, and a dropped link
schedules an aggressive reconnect. This module is used only when a config
entry opts in. It decides when a single GATT read is allowed. It never
turns the motor on or off, never parses advertisements as battery data,
and never disconnects while status is Running or unknown.

Home Assistant is not imported here so the decision logic can be tested
with mocked Bluetooth objects.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_LOGGER = logging.getLogger(__name__)

PATCH_ID = "low-traffic-battery-1"
RUNNING = "Running"
IDLE = "Idle"

# One local timer. It does not touch Bluetooth unless a tick is allowed
# to connect or to read a link that is already open.
CHECK_INTERVAL = timedelta(seconds=60)
STARTUP_DELAY = timedelta(minutes=15)
STALE_RUNNING = timedelta(minutes=20)
BATTERY_INTERVAL = timedelta(hours=6)
BACKOFF_INITIAL = timedelta(minutes=15)
BACKOFF_MAX = timedelta(hours=6)
UNKNOWN_HOLD = timedelta(minutes=20)


@dataclass(frozen=True)
class LowTrafficConfig:
    """Timings for one opted-in entry. Defaults are hours, not seconds."""

    check_interval: timedelta = CHECK_INTERVAL
    startup_delay: timedelta = STARTUP_DELAY
    stale_running: timedelta = STALE_RUNNING
    battery_interval: timedelta = BATTERY_INTERVAL
    backoff_initial: timedelta = BACKOFF_INITIAL
    backoff_max: timedelta = BACKOFF_MAX
    unknown_hold: timedelta = UNKNOWN_HOLD


def production_config() -> LowTrafficConfig:
    """Return the timings shipped to Home Assistant."""

    return LowTrafficConfig()


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


class LowTrafficPolicy:
    """Pure schedule for one toothbrush.

    ``link_up`` means this process already holds a GATT connection.
    A new connection is never opened while that is true, and it is never
    opened while a Running observation is still inside the stale window.
    """

    def __init__(self, config: LowTrafficConfig, now: datetime) -> None:
        now = _aware(now)
        self.config = config
        self.started_at = now
        self.last_status: str | None = None
        self.last_status_at: datetime | None = None
        self.last_battery: int | None = None
        self.last_battery_at: datetime | None = None
        self.failures = 0
        self.link_up = False
        self.next_attempt_at = now + config.startup_delay

    def note_startup_cache(
        self, status: str | None, now: datetime, *, link_up: bool = False
    ) -> None:
        """Apply the first observation after process start.

        A cached Running value is treated as possibly still brushing, so
        the first connection waits out the stale window. Unknown and Idle
        wait out the startup delay. Neither wait is permanent.
        """

        now = _aware(now)
        self.link_up = link_up
        if status == RUNNING:
            self.last_status = RUNNING
            self.last_status_at = now
            self.next_attempt_at = now + self.config.stale_running
            return
        self.last_status = IDLE if status == IDLE else None
        self.last_status_at = now if status == IDLE else None
        self.next_attempt_at = now + self.config.startup_delay

    def restore(self, snapshot: dict, now: datetime, *, link_up: bool) -> None:
        """Continue a schedule across an integration reload.

        The snapshot lives in memory only. A Core restart has no snapshot
        and uses ``note_startup_cache`` instead.
        """

        now = _aware(now)
        self.last_status = snapshot.get("last_status")
        self.last_status_at = snapshot.get("last_status_at")
        self.last_battery = snapshot.get("last_battery")
        self.last_battery_at = snapshot.get("last_battery_at")
        self.failures = int(snapshot.get("failures") or 0)
        restored_next = snapshot.get("next_attempt_at")
        self.next_attempt_at = (
            _aware(restored_next) if restored_next is not None else now
        )
        self.link_up = link_up
        if not link_up and self.last_status == RUNNING:
            self.note_link_lost(now, intentional=False)

    def snapshot(self) -> dict:
        return {
            "last_status": self.last_status,
            "last_status_at": self.last_status_at,
            "last_battery": self.last_battery,
            "last_battery_at": self.last_battery_at,
            "failures": self.failures,
            "next_attempt_at": self.next_attempt_at,
        }

    def _running_is_fresh(self, now: datetime) -> bool:
        if self.last_status != RUNNING or self.last_status_at is None:
            return False
        return now < self.last_status_at + self.config.stale_running

    def attempt_allowed(self, now: datetime) -> bool:
        """True when a new connection may be opened."""

        now = _aware(now)
        if self.link_up:
            return False
        if self._running_is_fresh(now):
            return False
        return now >= self.next_attempt_at

    def read_on_existing_link_allowed(self, now: datetime) -> bool:
        """True when a held link may be read without opening another one.

        A fresh Running observation blocks the read. Once that window
        passes, or the last read did not parse, one more GATT read on the
        same link is allowed. That retries a stuck or unknown session
        without a reconnect and without a disconnect.
        """

        now = _aware(now)
        if not self.link_up or now < self.next_attempt_at:
            return False
        if self._running_is_fresh(now):
            return False
        return True

    def note_connect_failure(self, now: datetime) -> None:
        now = _aware(now)
        self.failures += 1
        self.link_up = False
        exponent = min(self.failures - 1, 10)
        delay = self.config.backoff_initial * (2**exponent)
        if delay > self.config.backoff_max:
            delay = self.config.backoff_max
        self.next_attempt_at = now + delay

    def note_link_up(self) -> None:
        self.link_up = True

    def note_result(self, status: str | None, battery, now: datetime) -> None:
        """Record one GATT read.

        Only Idle schedules the long battery interval. Running and any
        other payload hold the link and postpone the next new connection.
        """

        now = _aware(now)
        if status in (RUNNING, IDLE):
            self.last_status = status
            self.last_status_at = now
        if battery is not None and status in (RUNNING, IDLE):
            self.last_battery = battery
            self.last_battery_at = now
            self.failures = 0
        if status == IDLE and battery is not None:
            self.next_attempt_at = now + self.config.battery_interval
        elif status == RUNNING:
            self.next_attempt_at = now + self.config.stale_running
        else:
            # Unparsed, or Idle without a battery byte. Do not wait hours,
            # and do not retry on the next one-second tick.
            self.next_attempt_at = now + self.config.unknown_hold

    def note_link_lost(self, now: datetime, *, intentional: bool) -> None:
        now = _aware(now)
        self.link_up = False
        if intentional:
            return
        if self.last_status == RUNNING and self.last_status_at is not None:
            stale_at = self.last_status_at + self.config.stale_running
            if self.next_attempt_at < stale_at:
                self.next_attempt_at = stale_at
            return
        if self.last_status is None:
            hold_at = now + self.config.unknown_hold
            if self.next_attempt_at < hold_at:
                self.next_attempt_at = hold_at

    def disconnect_allowed(self) -> bool:
        """Disconnect only after a parsed Idle status.

        Disconnecting while Running is avoided because that write may
        itself stop the motor. Unknown is treated the same way.
        """

        return self.last_status == IDLE


def should_release_link_on_unload(low_traffic: bool, status: str | None) -> bool:
    """Whether unload should call BLE disconnect.

    Entries that did not opt in keep today's behaviour and always
    disconnect. An opted-in entry disconnects only when status is Idle.
    """

    if not low_traffic:
        return True
    return status == IDLE


def advertisement_should_connect(low_traffic: bool, already_connected: bool) -> bool:
    """Passive advertisements never open a connection in low-traffic mode.

    Battery is not read from advertisement bytes. There is no decoded
    passive battery field in this integration.
    """

    if low_traffic:
        return False
    return not already_connected


def sensor_refresh_allowed(low_traffic: bool) -> bool:
    """The one-second sensor loop may refresh the coordinator only by default."""

    return not low_traffic


class LowTrafficMonitor:
    """Run the policy against one device object.

    ``device`` must provide ``connect(max_attempts=...)``, ``gatherdata``,
    ``disconnect``, ``client``, ``result``, and ``address``. ``gatherdata``
    is a GATT read. This class never calls ``turn_on``, ``turn_off``,
    ``start_notifications``, or a scanner.
    """

    def __init__(
        self,
        *,
        address: str,
        device,
        coordinator,
        config: LowTrafficConfig,
        now: datetime,
    ) -> None:
        self.address = address
        self.device = device
        self.coordinator = coordinator
        self.policy = LowTrafficPolicy(config, now)
        self._unsub = None
        self._task: asyncio.Task | None = None
        self._busy = False
        self._closed = False
        self._unloaded = False

    def attach(self, track_time_interval, hass) -> None:
        """Register the local check timer. The callback does not scan."""

        self._unsub = track_time_interval(
            hass, self._scheduled, self.policy.config.check_interval
        )

    def detach(self) -> None:
        unsub = self._unsub
        self._unsub = None
        if unsub is None:
            return
        try:
            unsub()
        except Exception:  # noqa: BLE001 - timer may already be gone on unload
            _LOGGER.debug("Low-traffic timer for %s was already removed", self.address)

    def _scheduled(self, now: datetime) -> None:
        if self._closed:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.async_tick(now))

    def note_link_lost(self, intentional: bool = False, now: datetime | None = None) -> None:
        """Called from the BLE disconnect callback. Does not reconnect."""

        if self._closed and self._unloaded:
            return
        self.policy.note_link_lost(
            now or datetime.now(timezone.utc), intentional=intentional
        )

    async def async_tick(self, now: datetime) -> str:
        if self._closed:
            return "closed"
        # The flag is set before the first await, so two tasks on this loop
        # cannot both pass it.
        if self._busy:
            return "busy"
        self._busy = True
        try:
            if self._closed:
                return "closed"
            now = _aware(now)
            if self.policy.read_on_existing_link_allowed(now):
                return await self._read(now, connect=False)
            if not self.policy.attempt_allowed(now):
                _LOGGER.debug(
                    "Low-traffic %s: no BLE connect (status=%s, next=%s)",
                    self.address,
                    self.policy.last_status,
                    self.policy.next_attempt_at.isoformat(),
                )
                return "skipped"
            return await self._read(now, connect=True)
        finally:
            self._busy = False

    async def _read(self, now: datetime, *, connect: bool) -> str:
        if connect:
            _LOGGER.info(
                "Low-traffic %s: one GATT connect (max_attempts=1), no motor command",
                self.address,
            )
            ok = await self.device.connect(max_attempts=1)
            if not ok:
                self.policy.note_connect_failure(now)
                _LOGGER.warning(
                    "Low-traffic %s: connect failed (%s in a row); next attempt %s. "
                    "A sleeping brush often refuses GATT.",
                    self.address,
                    self.policy.failures,
                    self.policy.next_attempt_at.isoformat(),
                )
                return "connect_failed"
            self.policy.note_link_up()
        else:
            client = getattr(self.device, "client", None)
            if not client or not getattr(client, "is_connected", False):
                self.policy.note_link_lost(now, intentional=False)
                return "link_already_down"
            _LOGGER.info(
                "Low-traffic %s: reading battery on the link already open",
                self.address,
            )

        parsed = await self.device.gatherdata()
        status = None
        battery = None
        if isinstance(parsed, dict):
            status = parsed.get("status")
            battery = parsed.get("battery_level")
        self.policy.note_result(status, battery, now)
        self._remember_wave_pro(parsed)

        client = getattr(self.device, "client", None)
        if not client or not getattr(client, "is_connected", False):
            self.policy.note_link_lost(now, intentional=False)

        if isinstance(parsed, dict) and battery is not None:
            await self._publish(parsed)
            _LOGGER.info(
                "Low-traffic %s: battery %s%% status %s; next new connect %s",
                self.address,
                battery,
                status,
                self.policy.next_attempt_at.isoformat(),
            )
        elif status == RUNNING:
            _LOGGER.info(
                "Low-traffic %s: status Running; not disconnecting and not reconnecting until %s",
                self.address,
                self.policy.next_attempt_at.isoformat(),
            )
        else:
            _LOGGER.info(
                "Low-traffic %s: GATT read did not yield a battery (status=%s); next attempt %s",
                self.address,
                status,
                self.policy.next_attempt_at.isoformat(),
            )

        if self.policy.disconnect_allowed():
            await self.device.disconnect()
            self.policy.note_link_lost(now, intentional=True)
            return "battery"
        if status == RUNNING:
            return "running_held"
        return "unknown"

    def _remember_wave_pro(self, parsed) -> None:
        """Record a Wave Pro parse without transmitting a command.

        Protocol detection normally happens when a notification arrives.
        This mode does not subscribe, so a later manual control would
        otherwise assume the V1 command set. Setting the marker does not
        read or write GATT.
        """

        if not isinstance(parsed, dict):
            return
        if "deep_clean" not in parsed and "over_pressure" not in parsed:
            return
        if getattr(self.device, "_proto_version", "unset") is not None:
            return
        self.device._proto_version = "v2pro"

    async def _publish(self, parsed: dict) -> None:
        result = getattr(self.device, "result", None) or parsed
        coordinator = self.coordinator
        if coordinator is None:
            return
        setter = getattr(coordinator, "async_set_updated_data", None)
        if setter is not None:
            setter(result)
        store = getattr(coordinator, "_async_store_data", None)
        if store is None:
            return
        try:
            await store(result)
        except Exception:  # noqa: BLE001 - storage must not start a retry storm
            _LOGGER.debug("Low-traffic %s: could not store battery", self.address, exc_info=True)

    async def async_shutdown(self) -> None:
        """Cancel the timer and any in-flight tick. Does not disconnect."""

        self._closed = True
        self.detach()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def async_unload(self, park: dict, snapshots: dict | None = None) -> str:
        """Stop timers and disconnect only when the last status is Idle.

        ``snapshots`` is kept by the caller across an integration reload so
        the next monitor does not restart the startup delay. It is process
        memory, not disk.
        """

        if self._unloaded:
            return "already"
        snapshot = self.policy.snapshot()
        self._unloaded = True
        await self.async_shutdown()
        if snapshots is not None:
            snapshots[self.address] = snapshot
        try:
            self.device.low_traffic_snapshot = snapshot
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Low-traffic %s: device refused snapshot", self.address)
        status = snapshot.get("last_status")
        if should_release_link_on_unload(True, status):
            park.pop(self.address, None)
            await self.device.disconnect()
            return "disconnected"
        park[self.address] = self.device
        _LOGGER.warning(
            "Low-traffic %s: unload leaving the BLE link up because status is %s. "
            "No disconnect is sent. A Home Assistant Core restart still drops the socket.",
            self.address,
            status,
        )
        return "parked"

"""Mocked BLE tests for the optional low-traffic battery mode.

These tests do not import Home Assistant and do not open a Bluetooth
adapter. Home Assistant modules are loaded only as source text where the
test checks that the default one-second path is still present.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "laifen_ble"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses looks the class module up in sys.modules during decoration.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LOW = _load("laifen_low_traffic", COMPONENT / "low_traffic.py")
LAIFEN = _load("laifen_device", COMPONENT / "laifen" / "laifen.py")


def fast_config():
    """Same shape as production, scaled to seconds so tests stay local."""

    return LOW.LowTrafficConfig(
        check_interval=timedelta(seconds=1),
        startup_delay=timedelta(seconds=15),
        stale_running=timedelta(seconds=20),
        battery_interval=timedelta(seconds=60),
        backoff_initial=timedelta(seconds=15),
        backoff_max=timedelta(seconds=40),
        unknown_hold=timedelta(seconds=20),
    )


class FakeClient:
    def __init__(self):
        self.is_connected = False
        self.ops = []

    async def read_gatt_char(self, uuid):
        self.ops.append(("read", uuid))
        return b""

    async def write_gatt_char(self, *args, **kwargs):
        self.ops.append(("write", args, kwargs))
        raise AssertionError("GATT write during battery monitoring")

    async def start_notify(self, *args, **kwargs):
        self.ops.append(("notify", args))
        raise AssertionError("notification subscribe during battery monitoring")

    async def stop_notify(self, *args, **kwargs):
        self.ops.append(("stop_notify", args))

    async def disconnect(self):
        self.ops.append(("client_disconnect",))
        self.is_connected = False


class FakeDevice:
    def __init__(self):
        self.address = "AA:BB:CC:DD:EE:01"
        self.calls = []
        self.result = {}
        self.client = FakeClient()
        self.connect_ok = True
        self.payload = None

    async def connect(self, max_attempts=10):
        self.calls.append(("connect", max_attempts))
        self.client.is_connected = bool(self.connect_ok)
        return bool(self.connect_ok)

    async def disconnect(self):
        self.calls.append(("disconnect",))
        self.client.is_connected = False

    async def gatherdata(self):
        self.calls.append(("gatherdata",))
        if self.payload is None:
            return None
        self.result = dict(self.payload)
        return dict(self.payload)

    def __getattr__(self, name):
        def banned(*_args, **_kwargs):
            self.calls.append((name,))
            raise AssertionError(f"battery monitor called {name}")

        return banned


class SlowDevice(FakeDevice):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def connect(self, max_attempts=10):
        self.calls.append(("connect", max_attempts))
        self.started.set()
        await asyncio.sleep(30)
        self.client.is_connected = True
        return True


class FakeCoordinator:
    def __init__(self):
        self.updates = []
        self.stored = []
        self.low_traffic = True
        self.device_asleep = False

    def async_set_updated_data(self, data):
        self.updates.append(dict(data))

    async def _async_store_data(self, data):
        self.stored.append(dict(data))


def _monitor(device=None, config=None, now=T0):
    device = device or FakeDevice()
    coordinator = FakeCoordinator()
    monitor = LOW.LowTrafficMonitor(
        address=device.address,
        device=device,
        coordinator=coordinator,
        config=config or fast_config(),
        now=now,
    )
    coordinator.monitor = monitor
    return monitor, device, coordinator


def _names(device):
    return [call[0] for call in device.calls]


def test_production_defaults_are_hours_not_seconds():
    config = LOW.production_config()
    assert config.check_interval == timedelta(seconds=60)
    assert config.startup_delay >= timedelta(minutes=10)
    assert config.stale_running >= timedelta(minutes=10)
    assert config.battery_interval >= timedelta(hours=1)
    assert config.backoff_initial >= timedelta(minutes=5)
    assert config.backoff_max >= config.backoff_initial
    assert config.backoff_max <= timedelta(hours=12)
    assert config.unknown_hold >= timedelta(minutes=5)
    assert config.battery_interval >= config.backoff_max


def test_startup_unknown_waits_then_allows_one_window():
    policy = LOW.LowTrafficPolicy(fast_config(), T0)
    policy.note_startup_cache(None, T0)
    assert policy.last_status is None
    assert not policy.attempt_allowed(T0)
    assert not policy.attempt_allowed(T0 + timedelta(seconds=14))
    assert policy.attempt_allowed(T0 + timedelta(seconds=15))
    assert not policy.disconnect_allowed()


def test_startup_cached_running_holds_then_expires():
    policy = LOW.LowTrafficPolicy(fast_config(), T0)
    policy.note_startup_cache("Running", T0)
    assert not policy.attempt_allowed(T0)
    assert not policy.attempt_allowed(T0 + timedelta(seconds=19))
    assert policy.attempt_allowed(T0 + timedelta(seconds=20))


def test_startup_cached_idle_still_waits_out_startup_delay():
    policy = LOW.LowTrafficPolicy(fast_config(), T0)
    policy.note_startup_cache("Idle", T0)
    assert not policy.attempt_allowed(T0 + timedelta(seconds=14))
    assert policy.attempt_allowed(T0 + timedelta(seconds=15))


def test_observed_running_blocks_reconnect_until_stale():
    policy = LOW.LowTrafficPolicy(fast_config(), T0)
    policy.note_startup_cache(None, T0)
    seen = T0 + timedelta(seconds=15)
    policy.note_link_up()
    policy.note_result("Running", 81, seen)
    assert policy.disconnect_allowed() is False
    assert not policy.attempt_allowed(seen)
    assert not policy.attempt_allowed(seen + timedelta(seconds=19))
    assert not policy.read_on_existing_link_allowed(seen + timedelta(seconds=19))
    policy.note_link_lost(seen + timedelta(seconds=5), intentional=False)
    assert not policy.attempt_allowed(seen + timedelta(seconds=5))
    assert not policy.attempt_allowed(seen + timedelta(seconds=19))
    assert policy.attempt_allowed(seen + timedelta(seconds=20))


def test_stale_running_on_a_held_link_is_not_a_lockout():
    policy = LOW.LowTrafficPolicy(fast_config(), T0)
    policy.note_link_up()
    policy.note_result("Running", 70, T0)
    assert policy.read_on_existing_link_allowed(T0 + timedelta(seconds=20))
    assert not policy.attempt_allowed(T0 + timedelta(seconds=20))


def test_connect_failures_back_off_and_stay_bounded():
    config = fast_config()
    policy = LOW.LowTrafficPolicy(config, T0)
    policy.note_startup_cache(None, T0)
    first = T0 + config.startup_delay
    assert policy.attempt_allowed(first)
    policy.note_connect_failure(first)
    assert policy.next_attempt_at == first + config.backoff_initial
    assert not policy.attempt_allowed(first + config.backoff_initial - timedelta(seconds=1))
    second = policy.next_attempt_at
    policy.note_connect_failure(second)
    assert policy.next_attempt_at == second + config.backoff_initial * 2

    cursor = T0
    policy.note_startup_cache(None, T0)
    for _ in range(8):
        assert policy.attempt_allowed(cursor + timedelta(days=1))
        cursor = cursor + timedelta(days=1)
        before = policy.next_attempt_at
        policy.note_connect_failure(cursor)
        delay = policy.next_attempt_at - cursor
        assert delay <= config.backoff_max
        assert policy.next_attempt_at > before or delay == config.backoff_max
    # The cap still expires. The brush is not locked out for good.
    assert policy.attempt_allowed(policy.next_attempt_at)


def test_success_resets_backoff():
    config = fast_config()
    policy = LOW.LowTrafficPolicy(config, T0)
    policy.note_connect_failure(T0)
    policy.note_connect_failure(policy.next_attempt_at)
    policy.note_result("Idle", 40, T0 + timedelta(hours=3))
    assert policy.failures == 0
    failed_at = T0 + timedelta(hours=3)
    policy.note_link_lost(failed_at, intentional=True)
    policy.note_connect_failure(policy.next_attempt_at)
    assert policy.next_attempt_at - (failed_at + config.battery_interval) == config.backoff_initial


def test_advertisements_and_default_unload_decisions():
    assert LOW.advertisement_should_connect(True, False) is False
    assert LOW.advertisement_should_connect(True, True) is False
    assert LOW.advertisement_should_connect(False, False) is True
    assert LOW.advertisement_should_connect(False, True) is False
    assert LOW.sensor_refresh_allowed(False) is True
    assert LOW.sensor_refresh_allowed(True) is False
    assert LOW.should_release_link_on_unload(False, "Running") is True
    assert LOW.should_release_link_on_unload(False, None) is True
    assert LOW.should_release_link_on_unload(True, "Running") is False
    assert LOW.should_release_link_on_unload(True, "Unknown") is False
    assert LOW.should_release_link_on_unload(True, None) is False
    assert LOW.should_release_link_on_unload(True, "Idle") is True


def test_monitor_startup_does_not_connect_until_delay():
    async def scenario():
        monitor, device, _coordinator = _monitor()
        monitor.policy.note_startup_cache(None, T0)
        assert await monitor.async_tick(T0) == "skipped"
        assert await monitor.async_tick(T0 + timedelta(seconds=14)) == "skipped"
        assert device.calls == []
        device.payload = {"status": "Idle", "battery_level": 88}
        result = await monitor.async_tick(T0 + timedelta(seconds=15))
        assert result == "battery"
        assert device.calls == [("connect", 1), ("gatherdata",), ("disconnect",)]

    asyncio.run(scenario())


def test_monitor_running_transition_holds_link_and_skips_repeats():
    async def scenario():
        monitor, device, coordinator = _monitor()
        monitor.policy.note_startup_cache(None, T0)
        device.payload = {"status": "Running", "battery_level": 54}
        opened = T0 + timedelta(seconds=15)
        assert await monitor.async_tick(opened) == "running_held"
        assert ("disconnect",) not in _names(device)
        assert ("turn_on",) not in _names(device)
        assert ("turn_off",) not in _names(device)
        assert coordinator.updates[-1]["battery_level"] == 54
        assert coordinator.stored[-1]["battery_level"] == 54
        for offset in range(1, 19):
            assert await monitor.async_tick(opened + timedelta(seconds=offset)) == "skipped"
        assert _names(device).count("connect") == 1
        # Link drops while the session is still inside the stale window.
        monitor.note_link_lost(intentional=False, now=opened + timedelta(seconds=5))
        device.client.is_connected = False
        assert await monitor.async_tick(opened + timedelta(seconds=10)) == "skipped"
        assert _names(device).count("connect") == 1
        device.payload = {"status": "Idle", "battery_level": 53}
        assert await monitor.async_tick(opened + timedelta(seconds=20)) == "battery"
        assert _names(device).count("connect") == 2
        assert device.calls[-1] == ("disconnect",)
        assert coordinator.updates[-1]["battery_level"] == 53
        # Next battery read waits the long interval, not another second.
        assert await monitor.async_tick(opened + timedelta(seconds=21)) == "skipped"

    asyncio.run(scenario())


def test_stale_running_rereads_existing_link_without_connect_or_disconnect():
    async def scenario():
        monitor, device, coordinator = _monitor()
        device.client.is_connected = True
        monitor.policy.note_startup_cache("Running", T0, link_up=True)
        assert await monitor.async_tick(T0 + timedelta(seconds=19)) == "skipped"
        device.payload = {"status": "Running", "battery_level": 64}
        assert await monitor.async_tick(T0 + timedelta(seconds=20)) == "running_held"
        assert device.calls == [("gatherdata",)]
        assert coordinator.updates[-1]["battery_level"] == 64
        assert ("disconnect",) not in _names(device)
        device.payload = {"status": "Idle", "battery_level": 64}
        assert await monitor.async_tick(T0 + timedelta(seconds=40)) == "battery"
        assert ("connect",) not in _names(device)
        assert device.calls[-1] == ("disconnect",)

    asyncio.run(scenario())


def test_unknown_payload_does_not_disconnect_or_lock_out():
    async def scenario():
        monitor, device, coordinator = _monitor()
        monitor.policy.note_startup_cache(None, T0)
        device.payload = None
        opened = T0 + timedelta(seconds=15)
        assert await monitor.async_tick(opened) == "unknown"
        assert device.calls == [("connect", 1), ("gatherdata",)]
        assert coordinator.updates == []
        assert not monitor.policy.disconnect_allowed()
        assert await monitor.async_tick(opened + timedelta(seconds=19)) == "skipped"
        device.payload = {"status": "Idle", "battery_level": 21}
        assert await monitor.async_tick(opened + timedelta(seconds=20)) == "battery"
        assert _names(device).count("connect") == 1
        assert coordinator.updates[-1]["battery_level"] == 21

    asyncio.run(scenario())


def test_failure_backoff_on_the_monitor():
    async def scenario():
        monitor, device, _coordinator = _monitor()
        monitor.policy.note_startup_cache(None, T0)
        device.connect_ok = False
        first = T0 + timedelta(seconds=15)
        assert await monitor.async_tick(first) == "connect_failed"
        assert await monitor.async_tick(first + timedelta(seconds=14)) == "skipped"
        assert await monitor.async_tick(first + timedelta(seconds=15)) == "connect_failed"
        assert [call[1] for call in device.calls] == [1, 1]
        # A sleeping brush that starts answering is read once, then left alone.
        device.connect_ok = True
        device.payload = {"status": "Idle", "battery_level": 19}
        third = monitor.policy.next_attempt_at
        assert await monitor.async_tick(third - timedelta(seconds=1)) == "skipped"
        assert await monitor.async_tick(third) == "battery"
        assert _names(device).count("connect") == 3

    asyncio.run(scenario())


def test_overlapping_ticks_open_one_connection():
    async def scenario():
        monitor, device, _coordinator = _monitor()
        monitor.policy.note_startup_cache(None, T0)

        async def slow_connect(max_attempts=10):
            device.calls.append(("connect", max_attempts))
            await asyncio.sleep(0.05)
            device.client.is_connected = True
            return True

        device.connect = slow_connect
        device.payload = {"status": "Idle", "battery_level": 33}
        due = T0 + timedelta(seconds=15)
        first, second = await asyncio.gather(
            monitor.async_tick(due), monitor.async_tick(due)
        )
        assert "battery" in (first, second)
        assert "busy" in (first, second)
        assert _names(device).count("connect") == 1

    asyncio.run(scenario())


def test_shutdown_cancels_timer_and_inflight_connect():
    async def scenario():
        device = SlowDevice()
        monitor, device, _coordinator = _monitor(device)
        monitor.policy.note_startup_cache(None, T0)
        cancelled = []

        def track(_hass, callback, interval):
            assert interval == fast_config().check_interval
            track.callback = callback

            def unsub():
                cancelled.append("unsub")

            return unsub

        monitor.attach(track, hass=None)
        due = T0 + timedelta(seconds=15)
        monitor._scheduled(due)
        await device.started.wait()
        park = {}
        snapshots = {}
        outcome = await monitor.async_unload(park, snapshots)
        assert outcome == "parked"
        assert device.address in park
        assert ("disconnect",) not in _names(device)
        assert cancelled == ["unsub"]
        assert snapshots[device.address]["last_status"] is None
        # A later timer fire must not connect again.
        calls_after = len(device.calls)
        monitor._scheduled(due + timedelta(seconds=30))
        await asyncio.sleep(0)
        assert len(device.calls) == calls_after
        assert await monitor.async_unload(park, snapshots) == "already"

    asyncio.run(scenario())


def test_unload_idle_disconnects_and_reload_keeps_the_deadline():
    async def scenario():
        monitor, device, coordinator = _monitor()
        monitor.policy.note_startup_cache(None, T0)
        device.payload = {"status": "Idle", "battery_level": 18}
        due = T0 + timedelta(seconds=15)
        assert await monitor.async_tick(due) == "battery"
        assert coordinator.updates[-1]["battery_level"] == 18
        park = {}
        snapshots = {}
        assert await monitor.async_unload(park, snapshots) == "disconnected"
        assert park == {}
        assert device.calls[-1] == ("disconnect",)
        nxt = snapshots[device.address]["next_attempt_at"]
        assert nxt == due + fast_config().battery_interval

        restored, device2, _coordinator2 = _monitor(config=fast_config(), now=due)
        device2.client.is_connected = False
        restored.policy.restore(snapshots[device.address], due, link_up=False)
        assert await restored.async_tick(due + timedelta(seconds=1)) == "skipped"
        assert device2.calls == []
        device2.payload = {"status": "Idle", "battery_level": 17}
        assert await restored.async_tick(nxt) == "battery"
        assert device2.calls[0] == ("connect", 1)

    asyncio.run(scenario())


def test_running_unload_parks_the_device():
    async def scenario():
        monitor, device, _coordinator = _monitor()
        device.client.is_connected = True
        monitor.policy.note_result("Running", 44, T0)
        monitor.policy.note_link_up()
        park = {}
        outcome = await monitor.async_unload(park, {})
        assert outcome == "parked"
        assert park[device.address] is device
        assert ("disconnect",) not in _names(device)
        assert ("connect",) not in _names(device)

    asyncio.run(scenario())


def test_gatherdata_is_a_read_and_default_connect_still_retries():
    async def scenario():
        payload = bytearray(36)
        payload[2] = 42
        payload[13] = 0
        packet = bytes([0x5A, 0xC1, 0x03, 0x00, 0x00, 36]) + bytes(payload) + bytes([0x00])

        class Device:
            address = "AA:BB:CC:DD:EE:02"
            name = "LFTB02-S-TEST"

        brush = LAIFEN.Laifen(Device(), coordinator=None)
        client = FakeClient()
        client.is_connected = True

        async def read(_uuid):
            client.ops.append(("read", _uuid))
            return packet

        client.read_gatt_char = read
        brush.client = client
        parsed = await brush.gatherdata()
        assert parsed["battery_level"] == 42
        assert parsed["status"] == "Idle"
        assert [op[0] for op in client.ops] == ["read"]
        assert brush.result["battery_level"] == 42

        captured = {}

        async def fake_establish(_client_class, _device, _name, **kwargs):
            captured["max_attempts"] = kwargs["max_attempts"]

            class Connected:
                is_connected = True

                @property
                def services(self):
                    raise RuntimeError("services unused in this test")

            return Connected()

        LAIFEN.establish_connection = fake_establish
        brush.client = FakeClient()
        assert await brush.connect() is True
        assert captured["max_attempts"] == 10
        brush.client = FakeClient()
        assert await brush.connect(max_attempts=1) is True
        assert captured["max_attempts"] == 1
        assert inspect.signature(LAIFEN.Laifen.connect).parameters["max_attempts"].default == 10

    asyncio.run(scenario())


def test_low_traffic_disconnect_does_not_schedule_aggressive_reconnect():
    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return None

    LAIFEN.asyncio.create_task = fake_create_task

    class Device:
        address = "AA:BB:CC:DD:EE:03"
        name = "LFTB02-S-TEST"

    class Monitor:
        def __init__(self):
            self.lost = []

        def note_link_lost(self, intentional=False, now=None):
            self.lost.append(intentional)

    coordinator = FakeCoordinator()
    brush = LAIFEN.Laifen(Device(), coordinator)
    monitor = Monitor()
    coordinator.monitor = monitor
    brush.client = FakeClient()

    coordinator.low_traffic = False
    brush._handle_disconnect(brush.client)
    assert len(scheduled) == 1

    scheduled.clear()
    coordinator.low_traffic = True
    brush._intentional_disconnect = True
    brush._handle_disconnect(brush.client)
    assert scheduled == []
    assert monitor.lost == [True]

    async def discover(*_args, **_kwargs):
        raise AssertionError("low-traffic mode scanned for the brush")

    LAIFEN.BleakScanner.discover = discover

    async def refused():
        assert await brush._aggressive_reconnect() is False

    asyncio.run(refused())


def test_wave_pro_read_records_protocol_and_sends_no_motor_command():
    async def scenario():
        payload = bytearray(36)
        payload[2] = 18
        payload[13] = 1  # Running
        packet = bytes([0x5A, 0xC1, 0x03, 0x00, 0x00, 36]) + bytes(payload) + bytes([0x00])

        class Device:
            address = "AA:BB:CC:DD:EE:09"
            name = "LFTB02-S-TEST"

        brush = LAIFEN.Laifen(Device(), coordinator=None)
        assert brush._proto_version is None
        client = FakeClient()

        async def read(_uuid):
            client.ops.append(("read", _uuid))
            return packet

        client.read_gatt_char = read
        brush.client = client

        async def connect(max_attempts=10):
            brush.calls.append(("connect", max_attempts))
            client.is_connected = True
            return True

        brush.calls = []
        brush.connect = connect
        brush.turn_on = _forbid_motor(brush, "turn_on")
        brush.turn_off = _forbid_motor(brush, "turn_off")

        coordinator = FakeCoordinator()
        monitor = LOW.LowTrafficMonitor(
            address=brush.address,
            device=brush,
            coordinator=coordinator,
            config=fast_config(),
            now=T0,
        )
        monitor.policy.note_startup_cache(None, T0)
        due = T0 + timedelta(seconds=15)
        assert await monitor.async_tick(due) == "running_held"
        assert brush._proto_version == "v2pro"
        assert [op[0] for op in client.ops] == ["read"]
        assert ("disconnect",) not in [call[0] for call in brush.calls]
        assert coordinator.updates[-1]["battery_level"] == 18
        assert coordinator.updates[-1]["status"] == "Running"
        # Still inside the fresh window: no second read and no disconnect.
        assert await monitor.async_tick(due + timedelta(seconds=5)) == "skipped"
        assert [op[0] for op in client.ops] == ["read"]

    asyncio.run(scenario())


def _forbid_motor(brush, name):
    async def _banned():
        brush.calls.append((name,))
        raise AssertionError(name)

    return _banned


def test_default_source_contract_is_unchanged():
    const_text = (COMPONENT / "const.py").read_text()
    init_text = (COMPONENT / "__init__.py").read_text()
    sensor_text = (COMPONENT / "sensor.py").read_text()
    flow_text = (COMPONENT / "config_flow.py").read_text()
    assert "UPDATE_SECONDS = 1" in const_text
    assert 'key="battery_level"' in const_text
    assert 'unique_id="laifen_battery_level"' in const_text
    assert 'native_unit_of_measurement="%"' in const_text
    assert '_attr_unique_id = f"{device.address}_{description.key}"' in sensor_text
    assert "async_request_refresh" in sensor_text
    update_body = init_text.split("async def _async_update_data", 1)[1].split(
        "\n    async def ", 1
    )[0]
    assert update_body.index("if self.low_traffic:") < update_body.index(
        "_aggressive_reconnect"
    )
    assert "timedelta(seconds=UPDATE_SECONDS)" in init_text
    low_branch = init_text.split("if low_traffic:", 1)[1]
    assert low_branch.index("continue") < low_branch.index(
        "if ble_device and await laifen.connect():"
    )
    assert "entry.options.get(CONF_LOW_TRAFFIC_BATTERY, False)" in flow_text
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        json.loads((COMPONENT / name).read_text())

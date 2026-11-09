# Low-traffic battery monitoring (patch `low-traffic-battery-1`)

**Deployment verdict: safe experimental. Not ready for a production install.**

The checkbox defaults to off, so copying the files and restarting Core without enabling it keeps the 3.0.4 Bluetooth session. Enabling it is an experiment. Do not install this onto the live Home Assistant host until a supervised test has shown that one short notification window returns a battery value and does not stop the motor. That test has not been run. This workspace did not connect to the brushes, the proxy, or Home Assistant.

This note is for the installed Laifen BLE integration **3.0.4** on the Melly972 fork. It does not claim that Home Assistant disconnects are what stop the Wave Pro motor. That link was not proven.

The patch does not remove the integration, does not delete entities, and does not change sensor unique IDs. With the new checkbox left off, the Bluetooth behaviour is the 3.0.4 behaviour.

## What was compared before editing

The fork's default branch does not carry the `3.0.4` tag. That tag on public upstream `UrbanTechIO/Laifen` is commit `b1d182ed92985360a6da70b0594fc64c589d828c`. The fork's `main` is that commit plus brand PNG files only (`custom_components/laifen_ble/brand/`). Every Python and JSON file in the installed 3.0.4 tree matches the fork before this patch. The deployable diff below is against tag `3.0.4` and does not add or remove those images.

Confirmed in `manifest.json`: `"version": "3.0.4"`. This patch leaves that version string alone so a HACS version check does not pretend this is an upstream release. After a Core restart the log line contains `low-traffic-battery-1` when the new code is actually loaded.

## What the 3.0.4 code does on a Wave Pro

These are code-path observations, not a diagnosis of the motor.

- `UPDATE_SECONDS` is 1. `LaifenCoordinator` refreshes on that interval.
- For a device already marked `v2pro`, the refresh skips `gatherdata()` (the extra GATT read on top of the push stream). It does **not** skip the connection check. If `client` is missing or not connected, every refresh calls `_aggressive_reconnect(max_attempts=5)`.
- Each reconnect attempt active-scans with `BleakScanner.discover()` and `connect()` calls `establish_connection(..., max_attempts=10)`.
- `_handle_disconnect` always schedules another `_aggressive_reconnect()` (default 10 attempts).
- A passive advertisement for a configured address calls `_async_device_recovery`, which connects when the link is down. That path can also call `set_ble_device()`, and that helper disconnects a client that is still up.
- Every sensor entity registers its own 1-second `async_track_time_interval`. That callback calls `coordinator.async_request_refresh()`. Home Assistant debounces overlapping refreshes, but the cadence stays about one second. The Wave Pro guard does not turn this loop off.
- Battery for the Wave Pro, in this source, is byte `payload[2]` of a status notification (`0xC1-03` or `0x81-03`) on characteristic `0000ff02`. The manifest class is `local_push`. The coordinator comment says those notifications are the status stream, and that an extra `read_gatt_char` on top of them during brushing was enough to overwhelm the link. `gatherdata()` can decode a buffer that already looks like that packet. Nothing in this tree shows a capture where a read, with notifications off, returned that buffer. A plain read is not a proven battery method. This patch does not use one. A sleeping brush often will not accept a connection at all.

The Pi radio is far from the bathroom and the ESPHome proxy is already described as unreliable. Either of those can drop the link with no help from this integration. A reconnect storm can still sit on top of that and line up with several motor stops in one brushing. This patch reduces how often the opted-in entry starts that storm. It cannot prove the storm was the cause, and it cannot make the radio or the proxy reliable.

## What the option changes

Checkbox: **Low-traffic battery monitoring** (`low_traffic_battery_mode`). Default **off**. Stored on the config entry, so Célian's entry and Eléa's entry are independent.

Turn it on only for the config entry whose device is Célian. Leave Eléa unchecked. If one entry's device list contains both brushes, do not enable the option: there is no per-address switch inside a shared entry.

When the checkbox is on for an entry:

| Situation | What the entry does |
| --- | --- |
| Startup, no status, or cached Idle | No GATT connect for 15 minutes. |
| Startup or live status Running | No new connect and no explicit disconnect for 20 minutes after that observation. |
| Running observation older than 20 minutes, link still up | One GATT **read** on that same link. No new connect. No disconnect if the read still says Running. |
| Running observation older than 20 minutes, link down | One connect, `max_attempts=1`. |
| Status notification says Idle and includes a battery byte | Update the existing battery value, stop notifications, disconnect, wait 6 hours. |
| Status notification says Running | Stop the notification subscription (this is not a motor command and not a link disconnect). Leave the GATT link up. Do not reconnect while the observation is fresh. |
| No status notification within 8 seconds | Do not invent a battery. Do not disconnect. The 8 seconds is only a client timeout around the ~5 second full-status cadence described in `laifen.py`. |
| Connect fails (asleep, out of range, proxy) | Wait 15 minutes, then 30, 60, 120, 240, then 6 hours. The wait never grows past 6 hours, so a failure does not lock the entry out. |
| Advertisement | Ignored. No connect, and advertisement bytes are not treated as a battery reading. |
| Sensor tick | The 1-second coordinator refresh is not requested. The synthetic timer sensor still counts locally. |
| Motor on/off | Never sent by this reader. `turn_on` / `turn_off` are not on this path. |

The local check timer is 60 seconds. Most ticks only look at the schedule. They do not scan and do not connect. A due attempt calls `connect(max_attempts=1)`, `start_notifications(max_attempts=1)`, waits for the existing notification parser to count a status packet, then `stop_notifications`. It does not call `read_gatt_char`, `turn_on`, or `turn_off`.

A successful read still writes `battery_level` on the same result dict the battery sensor already uses. Unique id stays `{MAC}_battery_level`. Device class stays battery, unit stays `%`. The 20% notification, whether it is a battery threshold or an automation on that entity, keeps the same entity. The value can sit at the last good reading for hours, and it can sit there longer when the brush is asleep and the connect fails. The notification fires when a later read crosses the threshold, not at the minute the cell actually crosses it.

An integration reload keeps the in-memory schedule (next attempt, failure count, last status). A Core restart does not. After a Core restart the 15-minute or 20-minute startup hold applies again, including when the restored status says Running. That hold expires. It is not a permanent lock.

## What this does not fix

- The notification window is the path this source already uses for status. It is still untested on this Wave Pro. If the brush does not send `0xC1-03` / `0x81-03` within 8 seconds of subscribing, the battery entity stays on its previous value. That previous value is the integration's existing cache, not a new reading, and this patch does not prove the cache is still true.
- Stopping notifications at the end of the window is how the attempt avoids leaving the brushing telemetry stream running. Whether `stop_notify` or the following Idle disconnect affects the motor is unknown. A Running result does not disconnect.
- One connect can still land while someone is brushing. The reader cannot see a Running motor until it has connected or until a previous status is still fresh. A brush that starts during the 15-minute startup delay, or during the 6-hour gap, can overlap that single attempt. During the wait, brushing telemetry can flow for up to 8 seconds.
- If a connect or a disconnect itself stops the motor, that one attempt can still stop it once. The aim is to stop the 5–6 reconnects per session, not to promise zero interaction.
- After a Running read the link is left up, because an explicit disconnect might be the thing that stops the motor. A link that stays up, plus a GATT read every 20 minutes while the status byte stays Running, can still disturb the brush. If the status byte never returns to Idle, that held link continues. That is an intentional tradeoff, not a silent success.
- Eléa's entry is unchanged. If both brushes share the flaky proxy, Eléa's one-second session and active scans still run. This patch will not quiet that traffic.
- Distance and the ESPHome proxy are outside the integration.
- No passive battery advertisement was added. Inventing one would publish numbers the brush does not send.
- A sleeping brush may refuse the GATT read. The last battery value then stays until a later attempt works.
- Manual controls (power, mode, sliders) still send writes if someone uses them. Do not automate those on Célian's entry while this mode is on. The reader records a Wave Pro protocol marker when a status read parses, so a later manual command is less likely to use the V1 frame, but the reader itself never sends the motor command.
- Live brushing sensors, pressure, and the session timer will be stale on the opted-in entry whenever the link is down. Entity rows stay. Eléa still updates at the old rate.

## Deploy

The coordinator copies files and restarts Home Assistant only after reviewing this note and the test report. Nothing here has been installed on the Home Assistant host.

1. Confirm the running integration version is **3.0.4** (Settings → Devices & services → Laifen → an entry, or `custom_components/laifen_ble/manifest.json`).
2. Back up, without deleting the config entry:
   - `config/custom_components/laifen_ble/` (the whole directory)
   - `config/.storage/core.config_entries`
   - `config/.storage/core.entity_registry`
   - `config/.storage/core.device_registry`
   - `config/.storage/laifen_ble_last_known.json` if it exists
3. Overlay **only** the changed files under `custom_components/laifen_ble/` from `laifen-3.0.4-low-traffic-changed-files.tar.gz`, or apply `laifen-3.0.4-low-traffic.patch` to a clean 3.0.4 tree and copy the same paths. Do not replace the directory with an upstream zip. Do not remove the integration in the UI.
4. Restart **Home Assistant Core** once, while neither brush is running if that can be timed. Copying Python into `custom_components` is not picked up by an integration reload.
5. In the log, find `low-traffic-battery-1`. If that string is absent, the new modules are not loaded. Do not enable the checkbox.
6. Settings → Devices & services → the Laifen entry that is Célian (match the renamed device or the MAC, not the title alone). Configure → enable **Low-traffic battery monitoring** → submit. Leave Eléa's entry alone.
7. Submitting the form reloads **that** config entry. Watch the log for `low-traffic battery mode on for <MAC>` and a next-attempt timestamp. The connection sensor for that brush should stay off except during a rare read. The battery entity id should be unchanged.

Do not enable the checkbox during a brushing session. The reload that follows the form is the wrong moment to be holding a live motor session.

## Integration reload and Core restart

| Action | What it does |
| --- | --- |
| Core restart | Required once after the files are copied. Loads the new Python. Drops every BLE socket, including a link this patch refused to disconnect. Also restarts the other toothbrush entry. |
| Options form → submit | Reloads **this** Laifen config entry only, after the new code is already loaded. Does not restart Core. Does not reload the other entry. Rebuilds the coordinator interval and the 60-second timer. |
| Integration reload while the last status is Running | The patched unload does **not** call disconnect for that opted-in device. The BlueZ session can still die when the client object is torn down. Do this while the brush is Idle. |
| Core restart while Running | The process exit drops the socket no matter what the patch skips. Avoid it during brushing. |
| Developer tools → YAML reload | Does not load this custom component and does not apply the checkbox. |

Unmodified 3.0.4 always disconnects on unload and on Core stop. That path is still what Eléa's entry uses.

## HACS

If HACS is tracking `https://github.com/UrbanTechIO/Laifen`, an update replaces `custom_components/laifen_ble` and deletes this patch (`low_traffic.py` and the edits). The manifest version is still `3.0.4`, so HACS will not show this fork patch as a newer upstream version.

- Turn off updates for that repository until the files are under the fork you intend to track, or re-apply the overlay after any HACS install.
- After any overwrite, the `low-traffic-battery-1` log line disappears. The checkbox value may still be stored on the entry. Old code ignores unknown option keys, but it also returns to the one-second session. Re-apply the files and restart Core before expecting the quiet schedule again.
- Do not point HACS at a brand-new repository for this patch. The work stays on the existing fork.

## Rollback

1. Stop using the checkbox only if you want. Restoring the files is the real rollback.
2. Put the backed-up `custom_components/laifen_ble` directory back.
3. Restart Core, preferably while the brush is idle.
4. Do not delete the Laifen config entries, devices, or entities. Unique ids were not migrated.
5. A leftover `low_traffic_battery_mode: true` in the entry options is ignored by unmodified 3.0.4. It does not have to be cleared for the old code to run. Clearing it later, once 3.0.4 is running, is optional and only matters if this patch is copied back.
6. Last-known battery in `.storage/laifen_ble_last_known.json` is the same file 3.0.4 already used. Restoring the backup of that file returns the previous cached values.

## Tests and hardware limits

Two suites, both local, neither pointed at a brush or a Home Assistant host:

- `tests/test_low_traffic.py` — schedule, backoff, Running hold, stale retry, timer cancel, and a real `notification_handler` fed a constructed `0xC1-03` buffer. That buffer proves the parser. It does not prove the brush sends it.
- `tests/test_ha_low_traffic.py` — imports the real config flow, coordinator, setup, and sensor modules under Home Assistant **2025.1.4**. USB monitoring is stubbed (no udev). Adapter discovery is the test plugin's mock. `establish_connection` is replaced so no socket opens. The tests check: Eléa's entry still connects with 10 attempts and a 1-second coordinator; Célian's entry does not connect at startup, does not connect when its coordinator is refreshed, does not connect from an advertisement, and does not active-scan; a timer callback after the startup hold subscribes once on `0000ff02`, does not read or write, and the battery entity (`{MAC}_battery_level`, `%`) becomes `21` from the injected notification; the options form reloads that entry back to the 1-second path; reloading the quiet entry cancels its timer and still does not connect.

Not covered, and required before this is anything but an experiment:

- Whether subscribing, unsubscribing, or disconnecting while Idle stops the motor.
- Whether this Wave Pro actually emits a status notification within 8 seconds when it is awake and not already in the normal session.
- A solitary `read_gatt_char` of `0000ff02`. The low-traffic path does not call it, because this source does not show that a read returns status.
- The ESPHome proxy, bathroom distance, and Eléa's session sharing that proxy.
- The 20% notification on a real multi-hour gap. The automated test only shows an injected notification reaching the existing entity.
- Core restart while the motor is running.

Until those are tried, hold the production install. The quiet schedule is the right shape for an experiment. It is not evidence the motor will keep running, and it is not evidence a sleeping brush will still yield a battery.

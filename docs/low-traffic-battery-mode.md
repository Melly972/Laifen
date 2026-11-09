# Low-traffic battery monitoring (patch `low-traffic-battery-1`)

This note is for the installed Laifen BLE integration **3.0.4** on the Melly972 fork. It describes an optional, per-entry battery reader. It does not claim that Home Assistant disconnects are what stop the Wave Pro motor. That link was not proven.

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
- Battery for the Wave Pro is byte `payload[2]` of a GATT status packet (`0xC1`/`0x81` subcommand `0x03`). This tree has no parser that reads battery from an advertisement. A sleeping brush often will not accept GATT at all.

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
| Read says Idle and includes a battery byte | Update the existing battery value, disconnect, wait 6 hours. |
| Read says Running | Keep the link. Do not disconnect. Do not subscribe to notifications. Do not reconnect while the observation is fresh. |
| Read has no usable battery | Do not disconnect (status is not known to be Idle). Retry the read on the open link after 20 minutes, or a single connect later if the link has dropped. |
| Connect fails (asleep, out of range, proxy) | Wait 15 minutes, then 30, 60, 120, 240, then 6 hours. The wait never grows past 6 hours, so a failure does not lock the entry out. |
| Advertisement | Ignored. No connect, and advertisement bytes are not treated as a battery reading. |
| Sensor tick | The 1-second coordinator refresh is not requested. The synthetic timer sensor still counts locally. |
| Motor on/off | Never sent by this reader. `turn_on` / `turn_off` are not on this path. |

The local check timer is 60 seconds. Most ticks only look at the schedule. They do not scan and do not connect.

A successful read still writes `battery_level` on the same result dict the battery sensor already uses. Unique id stays `{MAC}_battery_level`. Device class stays battery, unit stays `%`. The 20% notification, whether it is a battery threshold or an automation on that entity, keeps the same entity. The value can sit at the last good reading for hours, and it can sit there longer when the brush is asleep and the connect fails. The notification fires when a later read crosses the threshold, not at the minute the cell actually crosses it.

An integration reload keeps the in-memory schedule (next attempt, failure count, last status). A Core restart does not. After a Core restart the 15-minute or 20-minute startup hold applies again, including when the restored status says Running. That hold expires. It is not a permanent lock.

## What this does not fix

- One connect can still land while someone is brushing. The reader cannot see a Running motor until it has connected or until a previous status is still fresh. A brush that starts during the 15-minute startup delay, or during the 6-hour gap, can overlap that single attempt.
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

`tests/test_low_traffic.py` was run with pytest on this workspace: **22 passed**. Bluetooth was mocked. Home Assistant was not imported and was not contacted. No brush, proxy, or Core instance was used.

Covered with fakes: startup unknown, cached Running, cached Idle, connect failure and capped backoff, Running → link drop → stale → Idle battery update, stale read on a link that is already open (no second connect, no disconnect while Running), unparsed GATT (no disconnect, no lockout), overlapping ticks (one connect), timer cancellation and unload, Idle unload disconnect plus a reload that keeps the deadline, Running unload that parks the device, a real Wave Pro status packet that only issues a GATT read, default `connect()` still using 10 attempts, and a low-traffic disconnect that does not schedule `_aggressive_reconnect`.

Not covered, and still required on the real brushes before treating the symptom as gone:

- Whether a single GATT connect or a held link stops the Wave Pro motor.
- Whether `read_gatt_char` on characteristic `0000ff02` returns the 36-byte status payload on this firmware when notifications are not enabled. If it does not, battery will not refresh and the mode will retry later. It will not fall back to motor commands or to the notification flood.
- Behaviour through the ESPHome proxy and at bathroom distance.
- Interaction with Eléa's unchanged one-second session on the same proxy.
- The 20% notification firing on a real state change after a multi-hour gap.
- Core restart and integration reload while the motor is actually running.

Until those are tried, the honest result is: Célian's entry can be made much quieter, on a schedule that gives up rather than reconnects during a fresh Running state. That is not the same as a fix for the motor stopping.

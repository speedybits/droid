# TODO

## Vision: on-robot object detection (text only, no images leave the robot)
Jetson runs a detector and feeds short text descriptions to Claude / the voice model, for privacy.
- [ ] Confirm hardware: Jetson model (Nano 4GB vs 2GB) and camera (USB vs CSI)
- [ ] Detector service under system Python 3.6 (JetPack `jetson-inference`, SSD-MobileNet / COCO); writes latest detections as JSON on localhost (the nimbus venv is 3.10, so keep it a separate process)
- [ ] `see` tool in `head/claude_agent/agent.py`: returns e.g. `person (center, 1.4 m), ball (right, 0.8 m)`, with confidence
- [ ] Fuse camera bearing with LIDAR range at that angle; calibrate camera FOV and alignment to the LIDAR zero angle (ball at known angles)
- [ ] Face gaze follows a detected person
- [ ] Optional, rate-limited, off by default: push "person appeared" to the voice model so it can greet
- [ ] Optional, off by default: `look` tool that sends a real JPEG to Claude for things the detector can't name
- [ ] Safety stays with the LIDAR emergency stop; the camera never gates it

## Presence: who is in the room (Bluetooth, opt-in)
Household phones are paired with the Jetson so BlueZ can resolve their rotating private addresses. Local only; enrolled devices only.
- [ ] Hardware: USB Bluetooth dongle on the Jetson (the Nano dev kit has none); confirm BlueZ sees it
- [ ] Spike: can an iPhone bond with the Jetson so its rotating address resolves to a stable identity? iOS is usually the BLE central, so the Jetson may have to advertise and the phone initiate; verify on hardware before building on it
- [ ] Presence service on the Jetson (`bleak`): scan, map bonded identities to names in `data/presence.json`, keep `{name: last_seen, rssi}`, ignore everything else heard
- [ ] Smooth RSSI (moving average, "gone" after N minutes without a sighting); phones advertise less when idle, so don't treat a short silence as leaving
- [ ] `/presence` endpoint; add "Mike seems to be nearby" to the voice instructions via the brief
- [ ] `who_is_around` tool for Claude; while driving, RSSI trend gives a rough bearing for "go find Mike"
- [ ] Fixed-address BLE tags for anyone who won't pair (kids, guests, pets)
- [ ] Optional: router DHCP/ARP check for house-level "is anyone home"

## Localization: which room is the robot in (BLE outlets + LIDAR, lives mostly in nimbus)
Fixed, always-powered smart outlets act as room anchors. RSSI is noisy (6-10 dB), so use it as a room fingerprint, not for trilateration. Nimbus's map is dead-reckoned with no localization, so this also gives it a global fix after reboot, pick-up or drift.
- [ ] Spike (no mapping): which outlets advertise over BLE, and how steadily? Scan 5 min in a few rooms and check the fingerprints separate. Matter/Thread plugs may not be visible at all; confirm brand/protocol first
- [ ] Share one BLE dongle and one scanner with the presence service above (two processes will fight over the adapter): scanner in nimbus, droid reads it over the API
- [ ] Nimbus: `nimbus/sensors/ble_beacons.py` (`bleak`), smoothed RSSI per outlet address
- [ ] Radio map: while exploring, record the RSSI vector at each known pose (per-outlet heatmap in map coordinates); label rooms once ("kitchen", "den") and persist with the map
- [ ] Room classifier from the current RSSI vector; feed it as a coarse prior to pose estimation (startup, pick-up, drift correction), with LIDAR scan matching refining within the room
- [ ] Expose `room` + `confidence` in nimbus state/API; `where_am_i` tool in `head/claude_agent/agent.py` via `nimbus_client.py`

## Other open items
- [ ] Live-test memory on a real Grok session (tell it your name, STOP, start again, ask if it remembers); tune how it uses what it remembers
- [ ] Add API keys to `.env` and test the voice flow on desktop Chrome, then iPhone (verify Grok token response shape and Gemini setup field names)
- [ ] Idle timeout and session-minutes counter to limit voice billing
- [ ] Gemini 2.5 native-audio model if the silent wait during `ask_claude` is a problem
- [ ] Deploy to the Jetson: clone droid, `.env` on the robot, systemd service, Tailscale Serve
- [ ] `mount/` OpenSCAD phone mount over the LIDAR turret (from `neato_d4.scad`)
- [ ] Nimbus side: face/event endpoint; LIDAR blocked-sector mask for mount posts and camera

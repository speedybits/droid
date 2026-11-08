# TODO

## Simulator: conversations and seeking (first version done, see README)
- [ ] Measure it: score whether droid replies to lone speakers (ground truth is in `Group.solo`) and stays out of private chat; log per-encounter stats
- [ ] Recall check: ask droid in room two what it heard or said in room one and compare with the transcript
- [ ] People that move between rooms, walk away mid-conversation, or leave droid hanging
- [ ] Sim people as LIDAR obstacles (nimbus `raycast`) so droid can't drive through them
- [ ] Replace the line-of-sight "sight" cue with the real camera detector (Vision section below)
- [ ] Optional: synthesised audio of the people through the real mic path, to exercise `music.js` / media detection

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

## Media detection: music / TV vs. real conversation (head/web, fusion of weak cues)
Today's `music.js` (opt-in `?music=1`) only sees beat in post-AEC/NS/AGC audio, takes ~16 s to engage, goes blind while the robot talks, and can't see TV dialogue (speech has no beat). Reframe from "does this sound like music?" to "is anyone talking *to me*?": media doesn't interact with the robot. Fuse cues into one running log-odds `P(media)` driving three states: `ENGAGED` → `SUSPECT` (stay awake, require "Hey Gadget"/tap, no unprompted replies, shorter idle timeout) → `MEDIA` (existing dance/sleep path). "Hey Gadget" or a tap always wins and resets the evidence.
- [ ] Instrument first: log per-200 ms features plus a label (TV / music / talk) from real sessions, replay offline in Node to tune weights and measure latency and false positives; fix the stale "v5" label in `#dbg`
- [ ] New `head/web/media.js`: cue weight table, log-odds fusion with ~20 s decay, three-state machine, asymmetric thresholds (enter `SUSPECT` easily; `MEDIA` needs a strong cue corroborated by a second family); `?media=1` flag while testing
- [ ] Interaction cues: reply contingency (does a human answer within ~0.3-4 s, or talk straight through our TTS without yielding?), speech duty cycle excluding robot turns over 60-120 s (TV ≈ 90%+), unanswered-turn counter
- [ ] Ask the realtime model: `media_detected(kind, confidence)` tool in `providers/grok.js` / `gemini.js` plus a line in `BASE_INSTRUCTIONS` ("if this sounds like TV, an ad or lyrics rather than someone speaking to you, call it and stay silent")
- [ ] Transcript cues in `onUserText`: no addressing / third-person narration, topic jumps, repeated n-grams across turns (lyrics, jingles); stop discarding STT confidence / `no_speech_prob` in the provider adapters
- [ ] Wake-word recogniser while asleep: transcript volume per minute as a free "media is on" meter
- [ ] `music.js` upgrades: echo reference from the TTS bus (`playChunk`/`analyser`) so it keeps scoring while the robot talks; modulation spectrum (speech ~3-6 Hz vs music ~1-3 Hz); spectral flux, tonal persistence, flatness; analyse above 8 kHz; shorter windows so engagement takes seconds, not 16 s
- [ ] Spike: does a second `getUserMedia` with AGC/NS off work on iOS Safari? Crest factor / loudness range separates mastered media from live speech but is flattened by AGC
- [ ] Far-field vs near-field proxy (reverb tail after onsets): a person addressing the phone is near, a TV fills the room
- [ ] Optional, lazy-loaded: pretrained audio tagger (YAMNet, AudioSet Music / Television / Singing / Speech; ~3 MB via onnxruntime-web or TF.js, 0.96 s windows at 16 kHz). Strongest single cue, but breaks the no-deps rule
- [ ] Optional: chroma self-similarity over 4-30 s (music, jingles and theme tunes repeat); run only once cheap cues say `SUSPECT`
- [ ] Optional, rate-limited, `SUSPECT` only: active probe (go quiet a few seconds or play a soft chirp; people react, media doesn't)
- [ ] Ground truth from the host: small server endpoint for "something is playing" (MPRIS/`playerctl`, PipeWire sink-inputs, Spotify/Sonos/Chromecast/Roku/webOS, HDMI-CEC)
- [ ] TV power via the BLE outlet in the Localization section: wattage well above standby means TV on. Use as a prior that scales cue weights, not a verdict (TV on ≠ nobody talking)
- [ ] Later (Jetson camera / LIDAR): is anyone facing the robot or moving their lips; source localization by rotating the base (a fixed source stays fixed, a person doesn't)
- [ ] Policy: TV / dialogue goes to plain sleep or `SUSPECT` with no dance; music keeps the dancing face and beat pulses; keep the tap escape hatch (`hold()`)

## Other open items
- [ ] Live-test memory on a real Grok session (tell it your name, STOP, start again, ask if it remembers); tune how it uses what it remembers
- [ ] Add API keys to `.env` and test the voice flow on desktop Chrome, then iPhone (verify Grok token response shape and Gemini setup field names)
- [ ] Idle timeout and session-minutes counter to limit voice billing
- [ ] Gemini 2.5 native-audio model if the silent wait during `ask_claude` is a problem
- [ ] Deploy to the Jetson: `deploy/install.sh` written (untested: Jetson was off); clone droid, scp `.env`, run it, then confirm `NIMBUS_URL` and that the phone connects
- [ ] `mount/` OpenSCAD phone mount over the LIDAR turret (from `neato_d4.scad`)
- [ ] Nimbus side: face/event endpoint; LIDAR blocked-sector mask for mount posts and camera

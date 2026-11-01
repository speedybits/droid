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

## Other open items
- [ ] Add API keys to `.env` and test the voice flow on desktop Chrome, then iPhone (verify Grok token response shape and Gemini setup field names)
- [ ] Idle timeout and session-minutes counter to limit voice billing
- [ ] Gemini 2.5 native-audio model if the silent wait during `ask_claude` is a problem
- [ ] Deploy to the Jetson: clone droid, `.env` on the robot, systemd service, Tailscale Serve
- [ ] `mount/` OpenSCAD phone mount over the LIDAR turret (from `neato_d4.scad`)
- [ ] Nimbus side: face/event endpoint; LIDAR blocked-sector mask for mount posts and camera

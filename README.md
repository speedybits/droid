# Droid

iPhone "head" for a Neato D4 (running [nimbus](https://github.com/speedybits/nimbus), `neato` branch).
The phone web app does mic, speaker and animated face against the Grok Voice Agent API; Grok hands physical
requests to Claude (`ask_claude` tool), which drives the robot through nimbus's `/api/claude/*` endpoints.

```
iPhone Safari --wss--> Grok Voice          iPhone <--/ws/head--> head server --> Claude --> nimbus REST --> Neato
```

## Run (laptop, phase 1/2)

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env            # XAI_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY (server-side only; fill the ones you use)
.venv/bin/pytest
nimbus run --mock               # in the nimbus repo, or point NIMBUS_URL at the Jetson
.venv/bin/uvicorn head.server.app:app --host 0.0.0.0 --port 8443
```

The phone needs **HTTPS** for the microphone. Easiest: `tailscale serve --bg --https=443 http://localhost:8443`
on the host, then open the `https://<host>.<tailnet>.ts.net` URL in iPhone Safari and tap to wake.
(Desktop Chrome works over `http://localhost:8443` for testing.)

The start screen has **Wake with Grok** / **Wake with Gemini** buttons (`?provider=gemini` highlights one,
`?model=...` overrides the Gemini model). Each backend lives in `head/web/providers/` behind one interface.

## Layout
- `head/web/` phone app (AudioWorklet mic @24 kHz PCM16, playback queue, canvas face)
- `head/server/app.py` token minting (`POST /session`), `/ws/head` bridge, disconnect watchdog (stops robot)
- `head/claude_agent/` Claude tool loop + nimbus client
- `mount/` (todo) OpenSCAD phone mount over the LIDAR turret

Nimbus compatibility: on startup the head server checks nimbus's `/api/health` and `/openapi.json` and logs a
warning for any missing `/api/claude/*` endpoint (it never blocks startup). Verified against the nimbus `neato` branch.

Safety: "stop"/"halt" heard by Grok, the on-screen STOP button, and losing the phone connection all call
nimbus `/api/claude/stop` directly without waiting on an LLM.

## Memory
The robot remembers across conversations. Everything lives as plain text under `data/` (or `$DROID_DATA`), gitignored:
- `transcripts/YYYY-MM-DD.jsonl`: every utterance, both sides, the archive.
- `memory.md`: durable facts (name, preferences, home layout), one bullet each; the voice model saves these with its
  `remember` tool. Edit by hand to correct or forget something.
- `summary.md`: rolling summary of past conversations, rewritten by Claude when a conversation ends (or 90 s after the
  phone disconnects without ending it).

Each time the voice model connects, the server hands it the facts, summary and (after a reconnect) the current
conversation; Claude gets the same when it navigates. These files contain household speech: to wipe, delete `data/`.

## Simulator: people to find, listen to and talk with
`./run sim` starts nimbus's `four_rooms` sim, the head server with `SEEKER=1`, and opens two windows:
- `http://localhost:8443/people.html`: the map with droid, a dashed 1 m hearing ring, people and speech bubbles
  (green bubble = droid can hear it). Yellow-ringed people are alone in their room.
- `http://localhost:8443/?seek=1`: droid's face. Tap it once so the browser allows speech; droid's lines are then spoken
  aloud (browser TTS) with the face's mouth moving, and also shown as a caption.

Each room gets 1-4 people. Groups of 2-4 talk from the moment the sim starts (droid usually arrives mid-conversation and
only hears what is said within 1 m). A lone person stays silent until droid is within 1 m, then talks to it; droid is not
told that, it has to work it out. Dialogue is written by `SIM_MODEL` (Haiku), a short script per call, regenerated when droid
speaks nearby so the people can react. Droid's own decisions use `CLAUDE_MODEL`.

Droid's loop (`head/seeker.py`): patrol rooms (least recently visited first), head for anyone in line of sight within
3.5 m (position only, no words: a stand-in for the planned camera), stand where it can hear the whole group, log everything
overheard, and decide whether to say something at each *pause* (when the speaker stops). Groups leave natural openings
(after a question, or a finished thought) and wait while droid thinks. It cannot tell who is speaking.
Memory: lines go to `data/transcripts/` tagged with the room (`heard` = overheard, `droid` = its own words); each decision
prompt includes the recent lines from every room, so it can bring up room one in room two. The phone voice flow pauses the
seeker while a request is running, and STOP pauses it for 20 s.
Without `ANTHROPIC_API_KEY` the people use scripted lines and droid listens but does not speak. `LOG_LEVEL=DEBUG` for more logs.

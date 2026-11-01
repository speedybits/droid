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

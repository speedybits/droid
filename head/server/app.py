"""Head server: serves the phone web app, mints xAI ephemeral tokens, bridges ask_claude to Claude."""

import asyncio
import base64
import io
import json
import logging
import os
import time
import uuid
import wave
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from head.claude_agent.agent import ClaudeAgent
from head.claude_agent.nimbus_client import NimbusClient
from head.seeker import Seeker
from head.server.memory import MemoryStore
from head.sim.people import PeopleSim

log = logging.getLogger("head")
logging.getLogger("head").setLevel(os.getenv("LOG_LEVEL", "INFO"))  # uvicorn only configures its own loggers
if not logging.getLogger("head").handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
    logging.getLogger("head").addHandler(_h)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
XAI_TTS_URL = "https://api.x.ai/v1/tts"
GROK_VOICES = {"eve", "ara", "rex", "sal", "leo"}
XAI_TOKEN_URL = "https://api.x.ai/v1/realtime/client_secrets"


GEMINI_VOICES = {"Puck", "Charon", "Kore", "Fenrir", "Aoede"}  # keep in step with providers/gemini.js
GEMINI_TTS_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TOKEN_URL = "https://generativelanguage.googleapis.com/v1beta/auth_tokens"


async def _mint_grok(http: httpx.AsyncClient, key: str) -> str:
    r = await http.post(XAI_TOKEN_URL, headers={"Authorization": f"Bearer {key}"},
                        json={"expires_after": {"seconds": 300}})
    if r.status_code != 200:
        raise HTTPException(502, f"xAI token request failed ({r.status_code})")
    body = r.json()
    # The response shape isn't documented; accept the likely spellings.
    secret = body.get("value") or body.get("client_secret") or body.get("token")
    if isinstance(secret, dict):
        secret = secret.get("value")
    if not secret:
        raise HTTPException(502, f"unrecognised xAI token response (keys: {sorted(body)})")
    return secret


async def _mint_gemini(http: httpx.AsyncClient, key: str) -> str:
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    r = await http.post(GEMINI_TOKEN_URL, headers={"x-goog-api-key": key}, json={
        "uses": 1,
        "expireTime": (now + timedelta(minutes=30)).strftime(fmt),
        "newSessionExpireTime": (now + timedelta(minutes=2)).strftime(fmt),
    })
    if r.status_code != 200:
        raise HTTPException(502, f"Gemini token request failed ({r.status_code})")
    name = r.json().get("name")  # the token string lives in `name`
    if not name:
        raise HTTPException(502, "unrecognised Gemini token response")
    return name


_MINTERS = {"grok": ("XAI_API_KEY", _mint_grok), "gemini": ("GEMINI_API_KEY", _mint_gemini)}


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


SUMMARY_DELAY = 90.0  # a dropped head link may reconnect; only summarize once it has stayed gone


class NoCacheStatic(StaticFiles):
    """Revalidate on every load so edits to the web app reach the phone (Safari caches module URLs hard)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(agent: ClaudeAgent | None = None, nimbus: NimbusClient | None = None,
               memory: MemoryStore | None = None, seeker: Seeker | None = None) -> FastAPI:
    """Factory so tests can inject a fake agent/nimbus/memory/seeker."""
    _load_dotenv()
    memory = memory or MemoryStore(Path(os.getenv("DROID_DATA", "data")))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.nimbus is None:
            app.state.nimbus = NimbusClient(os.getenv("NIMBUS_URL", "http://localhost:8080"))
        if app.state.agent is None:
            app.state.llm = app.state.llm or AsyncAnthropic()
            app.state.agent = ClaudeAgent(
                app.state.llm, app.state.nimbus, os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), memory)
        tasks: list[asyncio.Task] = []
        if app.state.seeker is None and os.getenv("SEEKER"):
            app.state.seeker = await _build_seeker(app)
        if app.state.seeker is not None:
            sk = app.state.seeker
            sk.on_say, sk.on_face = on_seeker_say, on_seeker_face
            tasks += [asyncio.create_task(sk.people.run()), asyncio.create_task(sk.run())]
        # Warn about nimbus drift at startup; never block, since the robot may simply be off.
        if hasattr(app.state.nimbus, "check"):
            problems = await app.state.nimbus.check()
            for p in problems:
                log.warning("nimbus check: %s", p)
            if not problems:
                log.info("nimbus check: API compatible")
        yield
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def broadcast(msg: dict):
        for send in list(app.state.heads):
            try:
                await send(msg)
            except Exception:
                app.state.heads.discard(send)

    async def on_seeker_say(text: str):
        await broadcast({"type": "say", "text": text})

    async def on_seeker_face(state: str):
        await broadcast({"type": "face", "state": state})

    async def _build_seeker(app) -> Seeker | None:
        """Sim people + the seeking brain. Needs nimbus running with --sim; otherwise stays off."""
        try:
            world = await app.state.nimbus.sim_world()
            pose = await app.state.nimbus.sim_state()
        except Exception as e:
            log.warning("seeker disabled: nimbus is not running a simulation (%s)", e)
            return None
        has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
        llm = app.state.llm if has_key else None
        if llm is None:
            log.warning("ANTHROPIC_API_KEY not set: people use scripted lines and droid will not speak")
        people = PeopleSim(world, llm, os.getenv("SIM_MODEL", "claude-haiku-5-5"), avoid=(pose["x"], pose["y"]))
        log.info("seeker on: %d people in %d groups", sum(len(g.people) for g in people.groups), len(people.groups))
        # By default the phone's live Grok/Gemini session does all listening and speaking, as on the real robot
        # (it hears the people through the laptop speakers). The seeker only drives. SEEKER_SPEAKS=1 restores
        # the old text-injection brain that decides and speaks by itself.
        brain = llm if os.getenv("SEEKER_SPEAKS") else None
        return Seeker(app.state.nimbus, people, memory, brain, app.state.model)

    app = FastAPI(title="droid head", lifespan=lifespan)
    app.state.heads = set()  # send-callables of connected /ws/head sockets, for broadcasts
    app.state.seeker = seeker
    app.state.agent = agent
    app.state.nimbus = nimbus
    app.state.memory = memory
    app.state.llm = None
    app.state.model = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    app.state.pending_end = {}  # session id -> delayed summarize task
    # [{"role": "user"|"voice"|"claude", "text": str}]; seeded from disk so a restart keeps context
    app.state.transcript = memory.recent()

    @app.post("/tts")
    async def tts(body: dict):
        """Speak a line in a chosen Gemini voice (the key stays here). Returns a WAV the page can decode."""
        text, voice = str(body.get("text", "")).strip()[:400], body.get("voice") or "Puck"
        if not text:
            raise HTTPException(400, "no text")
        if body.get("provider") == "grok":
            if voice not in GROK_VOICES:
                raise HTTPException(400, f"unknown voice {voice!r}")
            key = os.getenv("XAI_API_KEY")
            if not key:
                raise HTTPException(500, "XAI_API_KEY not set")
            async with httpx.AsyncClient(timeout=30) as http:
                try:
                    r = await http.post(XAI_TTS_URL, headers={"Authorization": f"Bearer {key}"},
                                        json={"text": text, "voice_id": voice, "language": "en"})
                except httpx.HTTPError as e:
                    raise HTTPException(502, f"Grok TTS failed ({type(e).__name__})")
            if r.status_code != 200:
                raise HTTPException(502, f"Grok TTS failed (status {r.status_code})")
            return Response(r.content, media_type=r.headers.get("content-type", "audio/mpeg"))
        if voice not in GEMINI_VOICES:
            raise HTTPException(400, f"unknown voice {voice!r}")
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise HTTPException(500, "GEMINI_API_KEY not set")
        model = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
        payload = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
        pcm, why = None, ""
        # the preview TTS model fails now and then (429/5xx, or a reply with no audio); retry so the voice stays the same
        async with httpx.AsyncClient(timeout=30) as http:
            for attempt in range(3):
                try:
                    r = await http.post(GEMINI_TTS_URL.format(model=model), headers={"x-goog-api-key": key}, json=payload)
                    if r.status_code == 200:
                        pcm = base64.b64decode(r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
                        break
                    why = f"status {r.status_code}"
                    if r.status_code == 429:  # quota, not a blip: retrying only wastes requests
                        try:
                            why += ": " + r.json()["error"]["message"].split("\n")[1].strip("* ")[:140]
                        except (KeyError, IndexError, ValueError):
                            pass
                        logging.getLogger("head").warning("Gemini TTS quota: %s", why)
                        break
                except (KeyError, IndexError, ValueError):
                    why = "no audio in reply"
                except httpx.HTTPError as e:
                    why = type(e).__name__
                logging.getLogger("head").warning("Gemini TTS attempt %d failed: %s", attempt + 1, why)
                await asyncio.sleep(0.4 * (attempt + 1))
        if pcm is None:
            raise HTTPException(502, f"Gemini TTS failed ({why})")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
        return Response(buf.getvalue(), media_type="audio/wav")

    app.state.voice_error = None  # (message, time) of the last speech failure, shown in the people view

    @app.post("/voice_error")
    async def voice_error(body: dict):
        app.state.voice_error = (str(body.get("message", "unknown"))[:200], datetime.now(timezone.utc).timestamp())
        logging.getLogger("head").warning("voice error: %s", app.state.voice_error[0])
        return {"ok": True}

    @app.get("/sim/world")
    async def sim_world():
        return await app.state.nimbus.sim_world()

    @app.post("/sim/hear")
    async def sim_hear(body: dict):
        """The laptop mic heard droid's voice (from the phone): groups within hearing range react to it."""
        sk = app.state.seeker
        if sk is None:
            raise HTTPException(404, "seeker is off")
        text = str(body.get("text", "")).strip()[:300]
        if not text:
            raise HTTPException(400, "text required")
        try:
            st = await app.state.nimbus.sim_state()
            sk.people.update_droid(st["x"], st["y"])
        except Exception:
            pass
        n = sk.people.hear_droid(text)
        app.state.mic = (text, n, time.time())
        logging.getLogger("head").info("mic heard droid (%d group(s) in range): %s", n, text)
        return {"heard_by": n}

    @app.get("/sim/people")
    async def sim_people():
        """Everything the people view draws: pose, people, speech bubbles, droid's bubble and state."""
        sk = app.state.seeker
        if sk is None:
            raise HTTPException(404, "seeker is off (start with SEEKER=1 against a nimbus --sim)")
        try:
            st = await app.state.nimbus.sim_state()
            sk.people.update_droid(st["x"], st["y"])
            pose = {"x": st["x"], "y": st["y"], "theta": st.get("theta", 0.0)}
        except Exception:
            pose = None
        snap = sk.people.snapshot()
        snap["pose"] = pose
        snap["seeker"] = sk.snapshot()
        mic = getattr(app.state, "mic", None)
        snap["mic"] = {"text": mic[0], "heard_by": mic[1]} if mic and time.time() - mic[2] < 10 else None
        snap["tokens"] = {"sim": sk.people.tokens, "droid": sk.tokens}
        ve = app.state.voice_error
        snap["voice_error"] = ve[0] if ve and datetime.now(timezone.utc).timestamp() - ve[1] < 15 else None
        return snap

    @app.get("/memory/brief")
    async def memory_brief(sid: str = ""):
        return {"brief": memory.brief(sid or None)}

    async def summarize_later(sid: str, delay: float):
        await asyncio.sleep(delay)
        app.state.pending_end.pop(sid, None)
        if app.state.llm is not None:
            await memory.summarize(app.state.llm, app.state.model, sid)

    @app.post("/session")
    async def session(provider: str = "grok"):
        """Mint a short-lived voice-provider token so API keys never reach the phone.

        Returns {"provider": ..., "token": ...} regardless of provider.
        """
        if provider not in _MINTERS:
            raise HTTPException(400, f"unknown provider {provider!r}")
        env, mint = _MINTERS[provider]
        key = os.getenv(env)
        if not key:
            raise HTTPException(500, f"{env} not set")
        async with httpx.AsyncClient(timeout=15) as http:
            token = await mint(http, key)
        return {"provider": provider, "token": token}

    @app.websocket("/ws/head")
    async def head_ws(ws: WebSocket, sid: str = ""):
        await ws.accept()
        sid = sid or uuid.uuid4().hex
        if (t := app.state.pending_end.pop(sid, None)):
            t.cancel()  # same session reconnected after a blip
        ended = False
        tasks: set[asyncio.Task] = set()

        async def send(msg: dict):
            await ws.send_text(json.dumps(msg))

        app.state.heads.add(send)
        sk = lambda: app.state.seeker  # noqa: E731  (may be injected or built in lifespan)

        async def run_request(req_id: str, request: str):
            async def on_status(text: str):
                await send({"type": "claude_status", "id": req_id, "text": text})
            async def on_turn(d: int):
                await send({"type": "turn", "dir": d})
            await send({"type": "face", "state": "thinking"})
            before = getattr(app.state.agent, "tokens", 0)
            if sk():
                sk().pause()  # a person is asking for something: the seeker steps aside
            try:
                text = await app.state.agent.handle(request, app.state.transcript, on_status, on_turn)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("agent failed")
                text = f"Sorry, something went wrong: {e}"
            app.state.transcript.append({"role": "claude", "text": text})
            memory.log(sid, "claude", text)
            await send({"type": "usage", "claude_tokens": getattr(app.state.agent, "tokens", 0) - before})
            await send({"type": "claude_result", "id": req_id, "text": text})
            await send({"type": "face", "state": "idle"})
            if sk():
                sk().resume()

        async def halt():
            if sk():
                sk().pause(20)  # STOP means stop: stay put a while before seeking again
            for t in list(tasks):
                t.cancel()
            try:
                await app.state.nimbus.stop()
            except Exception:
                log.warning("nimbus stop failed")

        try:
            while True:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type")
                if kind == "transcript":
                    app.state.transcript.append({"role": msg["role"], "text": msg["text"]})
                    memory.log(sid, msg["role"], msg["text"])
                    log.info("%s: %s", msg["role"], msg["text"])
                elif kind == "remember":
                    if memory.add_fact(str(msg.get("fact", ""))):
                        log.info("remembered: %s", msg["fact"])
                elif kind == "end":
                    ended = True
                elif kind == "ask_claude":
                    t = asyncio.create_task(run_request(msg["id"], msg["request"]))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                elif kind == "voice_busy":  # the live voice session is mid-turn: the seeker stands still
                    if sk():
                        sk().hold(bool(msg.get("busy")))
                elif kind == "stop":
                    await halt()
        except WebSocketDisconnect:
            pass
        finally:
            app.state.heads.discard(send)
            if sk():
                sk().hold(False, 0)
            # Watchdog: losing the phone means nobody is listening; stop the robot.
            await halt()
            # An explicit end summarizes now; a bare disconnect waits in case the phone reconnects.
            t = asyncio.create_task(summarize_later(sid, 0 if ended else SUMMARY_DELAY))
            app.state.pending_end[sid] = t

    app.mount("/", NoCacheStatic(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app()

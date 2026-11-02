"""Head server: serves the phone web app, mints xAI ephemeral tokens, bridges ask_claude to Claude."""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from head.claude_agent.agent import ClaudeAgent
from head.claude_agent.nimbus_client import NimbusClient
from head.server.memory import MemoryStore

log = logging.getLogger("head")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
XAI_TOKEN_URL = "https://api.x.ai/v1/realtime/client_secrets"


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
               memory: MemoryStore | None = None) -> FastAPI:
    """Factory so tests can inject a fake agent/nimbus/memory."""
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
        # Warn about nimbus drift at startup; never block, since the robot may simply be off.
        if hasattr(app.state.nimbus, "check"):
            problems = await app.state.nimbus.check()
            for p in problems:
                log.warning("nimbus check: %s", p)
            if not problems:
                log.info("nimbus check: API compatible")
        yield

    app = FastAPI(title="droid head", lifespan=lifespan)
    app.state.agent = agent
    app.state.nimbus = nimbus
    app.state.memory = memory
    app.state.llm = None
    app.state.model = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    app.state.pending_end = {}  # session id -> delayed summarize task
    # [{"role": "user"|"voice"|"claude", "text": str}]; seeded from disk so a restart keeps context
    app.state.transcript = memory.recent()

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

        async def run_request(req_id: str, request: str):
            async def on_status(text: str):
                await send({"type": "claude_status", "id": req_id, "text": text})
            await send({"type": "face", "state": "thinking"})
            try:
                text = await app.state.agent.handle(request, app.state.transcript, on_status)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("agent failed")
                text = f"Sorry, something went wrong: {e}"
            app.state.transcript.append({"role": "claude", "text": text})
            memory.log(sid, "claude", text)
            await send({"type": "claude_result", "id": req_id, "text": text})
            await send({"type": "face", "state": "idle"})

        async def halt():
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
                elif kind == "stop":
                    await halt()
        except WebSocketDisconnect:
            pass
        finally:
            # Watchdog: losing the phone means nobody is listening; stop the robot.
            await halt()
            # An explicit end summarizes now; a bare disconnect waits in case the phone reconnects.
            t = asyncio.create_task(summarize_later(sid, 0 if ended else SUMMARY_DELAY))
            app.state.pending_end[sid] = t

    app.mount("/", NoCacheStatic(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app()

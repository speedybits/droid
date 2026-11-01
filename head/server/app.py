"""Head server: serves the phone web app, mints xAI ephemeral tokens, bridges ask_claude to Claude."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from head.claude_agent.agent import ClaudeAgent
from head.claude_agent.nimbus_client import NimbusClient

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


def create_app(agent: ClaudeAgent | None = None, nimbus: NimbusClient | None = None) -> FastAPI:
    """Factory so tests can inject a fake agent/nimbus."""
    _load_dotenv()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.nimbus is None:
            app.state.nimbus = NimbusClient(os.getenv("NIMBUS_URL", "http://localhost:8080"))
        if app.state.agent is None:
            app.state.agent = ClaudeAgent(
                AsyncAnthropic(), app.state.nimbus, os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"))
        yield

    app = FastAPI(title="droid head", lifespan=lifespan)
    app.state.agent = agent
    app.state.nimbus = nimbus
    app.state.transcript = []  # [{"role": "user"|"voice"|"claude", "text": str}]

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
    async def head_ws(ws: WebSocket):
        await ws.accept()
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
                    log.info("%s: %s", msg["role"], msg["text"])
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

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app()

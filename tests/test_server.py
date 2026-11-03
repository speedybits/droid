from fastapi.testclient import TestClient

from head.server.app import create_app


class FakeAgent:
    async def handle(self, request, transcript, on_status, on_turn=None):
        await on_status("working")
        return f"did: {request}"


class FakeNimbus:
    def __init__(self):
        self.stopped = 0

    async def stop(self):
        self.stopped += 1


def test_ask_claude_roundtrip_and_disconnect_stops_robot():
    nimbus = FakeNimbus()
    with TestClient(create_app(FakeAgent(), nimbus)) as c:
        with c.websocket_connect("/ws/head") as ws:
            ws.send_json({"type": "transcript", "role": "user", "text": "hi"})
            ws.send_json({"type": "ask_claude", "id": "1", "request": "move"})
            seen = [ws.receive_json() for _ in range(4)]
            types = [m["type"] for m in seen]
            assert "claude_result" in types and "claude_status" in types
            assert {"type": "claude_result", "id": "1", "text": "did: move"} in seen
        assert nimbus.stopped >= 1  # watchdog on disconnect


def test_session_requires_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with TestClient(create_app(FakeAgent(), FakeNimbus())) as c:
        assert c.post("/session").status_code == 500


def test_session_unknown_provider_and_missing_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with TestClient(create_app(FakeAgent(), FakeNimbus())) as c:
        assert c.post("/session?provider=nope").status_code == 400
        assert c.post("/session?provider=gemini").status_code == 500


async def test_mint_gemini_returns_token_name_with_constraints():
    import httpx
    from head.server.app import _mint_gemini
    seen = {}

    def handler(req: httpx.Request):
        seen["key"] = req.headers["x-goog-api-key"]
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"name": "auth_tokens/abc"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await _mint_gemini(http, "k") == "auth_tokens/abc"
    assert seen["key"] == "k" and '"uses":1' in seen["body"].replace(" ", "")


async def test_mint_grok_accepts_nested_and_flat_shapes():
    import httpx
    from head.server.app import _mint_grok
    for body in ({"value": "xai-client-secret.t"}, {"client_secret": {"value": "xai-client-secret.t"}}):
        t = httpx.MockTransport(lambda req, b=body: httpx.Response(200, json=b))
        async with httpx.AsyncClient(transport=t) as http:
            assert await _mint_grok(http, "k") == "xai-client-secret.t"

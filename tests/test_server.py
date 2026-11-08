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


def test_sim_people_404_without_seeker():
    with TestClient(create_app(FakeAgent(), FakeNimbus())) as c:
        assert c.get("/sim/people").status_code == 404


def test_seeker_say_is_broadcast_and_people_view_served():
    import asyncio
    import random

    from head.sim.people import PeopleSim
    from conftest import four_rooms_world

    class NimbusSim(FakeNimbus):
        async def sim_state(self):
            return {"x": 1.0, "y": 1.0, "theta": 0.0}

        async def sim_world(self):
            return four_rooms_world()

    class FakeSeeker:
        state, tokens, on_say, on_face = "seek", 0, None, None

        def __init__(self):
            self.people = PeopleSim(four_rooms_world(), rng=random.Random(1))
            self.paused = []

        async def run(self):
            await asyncio.sleep(3600)

        def snapshot(self):
            return {"state": "seek", "paused": False, "room": "kitchen", "pose": (1, 1, 0)}

        def pause(self, secs=None):
            self.paused.append(secs)

        def hold(self, on, grace=3.0):
            self.holds = getattr(self, "holds", []) + [on]

        def resume(self):
            self.paused.append("resume")

    sk = FakeSeeker()
    orig_run = sk.people.run

    async def idle(interval=0.25):
        await asyncio.sleep(3600)

    sk.people.run = idle
    with TestClient(create_app(FakeAgent(), NimbusSim(), seeker=sk)) as c:
        snap = c.get("/sim/people").json()
        assert snap["people"] and snap["droid"]["room"] == "kitchen" and snap["hear_range"] == 1.0
        assert c.get("/people.html").status_code == 200
        with c.websocket_connect("/ws/head") as ws:
            c.portal.call(sk.on_say, "hello humans")
            assert ws.receive_json() == {"type": "say", "text": "hello humans"}
            ws.send_json({"type": "ask_claude", "id": "1", "request": "move"})
            [ws.receive_json() for _ in range(4)]
        assert None in sk.paused and "resume" in sk.paused  # seeker stepped aside for the request, then resumed


def test_tts_validates_input_and_needs_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with TestClient(create_app(FakeAgent(), FakeNimbus())) as c:
        assert c.post("/tts", json={"text": ""}).status_code == 400
        assert c.post("/tts", json={"text": "hi", "voice": "Nope"}).status_code == 400
        assert c.post("/tts", json={"text": "hi", "voice": "Kore"}).status_code == 500

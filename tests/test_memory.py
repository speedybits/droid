import json
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from head.server.app import create_app
from head.server.memory import MAX_FACTS, MemoryStore


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []
        self.messages = NS(create=self._create)

    async def _create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        return NS(content=[NS(type="text", text=self.reply)])


def test_log_and_recent_roundtrip(tmp_path):
    m = MemoryStore(tmp_path)
    m.log("s1", "user", "hello")
    m.log("s1", "voice", "hi there")
    assert m.recent() == [{"role": "user", "text": "hello"}, {"role": "voice", "text": "hi there"}]
    assert len(list((tmp_path / "transcripts").glob("*.jsonl"))) == 1


def test_torn_line_is_skipped(tmp_path):
    m = MemoryStore(tmp_path)
    m.log("s1", "user", "ok")
    next((tmp_path / "transcripts").glob("*.jsonl")).open("a").write('{"ts": "x", "sess')
    assert m.recent() == [{"role": "user", "text": "ok"}]


def test_facts_dedup_and_cap(tmp_path):
    m = MemoryStore(tmp_path)
    assert m.add_fact("Mike likes the kitchen clean")
    assert not m.add_fact("mike likes the kitchen CLEAN!")
    for i in range(MAX_FACTS + 5):
        m.add_fact(f"fact number {i}")
    assert len(m.facts()) == MAX_FACTS and m.facts()[-1] == f"fact number {MAX_FACTS + 4}"


def test_brief_includes_facts_summary_and_current_session(tmp_path):
    m = MemoryStore(tmp_path)
    m.add_fact("name is Mike")
    m._write_summary("We talked about the garage.")
    m.log("cur", "user", "go left")
    m.log("old", "user", "unrelated")
    b = m.brief("cur")
    assert "name is Mike" in b and "garage" in b and "go left" in b and "unrelated" not in b
    assert "go left" not in m.brief()


async def test_summarize_folds_session_and_skips_short(tmp_path):
    m = MemoryStore(tmp_path)
    llm = FakeLLM('noise {"summary": "Mike met the robot.", "facts": ["name is Mike"]} noise')
    m.log("s", "user", "hi")
    assert not await m.summarize(llm, "x", "s")  # one user turn: not worth a call
    m.log("s", "voice", "hello")
    m.log("s", "user", "my name is Mike")
    assert await m.summarize(llm, "x", "s")
    assert m.summary() == "Mike met the robot." and m.facts() == ["name is Mike"]
    assert not await m.summarize(llm, "x", "s") and len(llm.prompts) == 1  # idempotent


async def test_summarize_failure_keeps_old_summary_and_allows_retry(tmp_path):
    m = MemoryStore(tmp_path)
    m._write_summary("old")
    for t in ("a", "b"):
        m.log("s", "user", t)
    assert not await m.summarize(FakeLLM("not json"), "x", "s")
    assert m.summary() == "old"
    assert await m.summarize(FakeLLM('{"summary": "new", "facts": []}'), "x", "s")


class FakeAgent:
    async def handle(self, request, transcript, on_status):
        return "done"


class FakeNimbus:
    async def stop(self):
        pass


def test_server_logs_remembers_and_serves_brief(tmp_path):
    mem = MemoryStore(tmp_path)
    with TestClient(create_app(FakeAgent(), FakeNimbus(), mem)) as c:
        with c.websocket_connect("/ws/head?sid=abc") as ws:
            ws.send_json({"type": "transcript", "role": "user", "text": "call me Mike"})
            ws.send_json({"type": "remember", "fact": "name is Mike"})
            ws.send_json({"type": "end"})
        brief = c.get("/memory/brief?sid=abc").json()["brief"]
    assert "name is Mike" in brief and "call me Mike" in brief
    assert json.loads(next((tmp_path / "transcripts").glob("*.jsonl")).read_text())["session"] == "abc"
    # a fresh server on the same dir still has it all
    assert "name is Mike" in MemoryStore(tmp_path).brief()
    assert create_app(FakeAgent(), FakeNimbus(), MemoryStore(tmp_path)).state.transcript[0]["text"] == "call me Mike"

from types import SimpleNamespace as NS

from head.claude_agent.agent import ClaudeAgent


class FakeNimbus:
    def __init__(self):
        self.calls = []

    async def move(self, distance, **_):
        self.calls.append(("move", distance))
        return {"result": "success", "actual": distance}

    async def scan(self):
        self.calls.append(("scan",))
        return {"front": 1.2}

    async def stop(self):
        self.calls.append(("stop",))
        return {}


class FakeLLM:
    """Replays scripted responses; .messages.create is what ClaudeAgent calls."""
    def __init__(self, script):
        self.script = list(script)
        self.messages = NS(create=self._create)

    async def _create(self, **_):
        return self.script.pop(0)


def tool(name, id, **inp):
    return NS(type="tool_use", name=name, id=id, input=inp)


def text(t):
    return NS(type="text", text=t)


async def noop(_):
    pass


async def test_scan_then_move_then_reply():
    nimbus = FakeNimbus()
    llm = FakeLLM([NS(content=[tool("scan", "a")]), NS(content=[tool("move", "b", distance=0.5)]),
                   NS(content=[text("Moved half a meter.")])])
    out = await ClaudeAgent(llm, nimbus, "m").handle("go forward a bit", [], noop)
    assert out == "Moved half a meter."
    assert nimbus.calls == [("scan",), ("move", 0.5)]


async def test_move_distance_is_clamped():
    nimbus = FakeNimbus()
    llm = FakeLLM([NS(content=[tool("move", "a", distance=50)]), NS(content=[text("ok")])])
    await ClaudeAgent(llm, nimbus, "m").handle("go far", [], noop)
    assert nimbus.calls == [("move", 2.0)]


async def test_step_cap_stops_robot():
    nimbus = FakeNimbus()
    llm = FakeLLM([NS(content=[tool("scan", str(i))]) for i in range(20)])
    out = await ClaudeAgent(llm, nimbus, "m").handle("loop forever", [], noop)
    assert "stopped" in out and nimbus.calls[-1] == ("stop",)


async def test_tool_error_is_reported_to_claude_not_raised():
    class Boom(FakeNimbus):
        async def scan(self):
            raise RuntimeError("serial down")
    llm = FakeLLM([NS(content=[tool("scan", "a")]), NS(content=[text("I can't see right now.")])])
    assert await ClaudeAgent(llm, Boom(), "m").handle("look", [], noop) == "I can't see right now."

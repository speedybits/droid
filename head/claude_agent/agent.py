"""Claude navigation agent: turns a spoken request into nimbus tool calls."""

import asyncio
import json
from typing import Awaitable, Callable

from .nimbus_client import NimbusClient

MAX_STEPS = 12
MAX_SECONDS = 120.0
MAX_MOVE_M = 2.0

SYSTEM = """You are the navigation brain of a small wheeled robot (a Neato D4 with an iPhone head).
A separate voice assistant talks to the person; it hands you physical requests. You move the robot
with the tools and then reply with ONE or TWO short sentences describing what happened, which the
voice assistant will say aloud. Rules:
- Look before you move: call scan first unless you just did. Prefer short moves (<= 0.5 m) and re-scan.
- Never move more than 2 m in one call. Obstacles stop motion automatically; report it honestly.
- Use speak_status only for long tasks, to give a brief progress line.
- If asked to stop, call stop immediately. If a request is unsafe or unclear, say so instead of moving.
- Positive turn degrees = left (counter-clockwise), negative = right. Positive distance = forward."""

TOOLS = [
    {"name": "move", "description": "Drive forward (+) or backward (-) by distance in meters. Blocks until done.",
     "input_schema": {"type": "object", "properties": {"distance": {"type": "number"}}, "required": ["distance"]}},
    {"name": "turn", "description": "Rotate in place by degrees (+ left, - right). Blocks until done.",
     "input_schema": {"type": "object", "properties": {"degrees": {"type": "number"}}, "required": ["degrees"]}},
    {"name": "goto", "description": "Navigate to map coordinates (meters) with obstacle avoidance.",
     "input_schema": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                      "required": ["x", "y"]}},
    {"name": "scan", "description": "Processed surroundings: wall distances, openings, closest obstacle.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "map", "description": "Accumulated obstacle map as ASCII art plus metadata.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "status", "description": "Current movement command status.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "stop", "description": "Cancel the current movement immediately.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "speak_status", "description": "Send a brief progress line to be spoken to the person.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]

StatusCallback = Callable[[str], Awaitable[None]]


class ClaudeAgent:
    def __init__(self, anthropic_client, nimbus: NimbusClient, model: str):
        self._llm = anthropic_client
        self._nimbus = nimbus
        self._model = model

    async def _run_tool(self, name: str, args: dict, on_status: StatusCallback):
        if name == "move":
            d = max(-MAX_MOVE_M, min(MAX_MOVE_M, float(args["distance"])))
            return await self._nimbus.move(d)
        if name == "turn":
            return await self._nimbus.turn(float(args["degrees"]))
        if name == "goto":
            return await self._nimbus.goto(float(args["x"]), float(args["y"]))
        if name == "scan":
            return await self._nimbus.scan()
        if name == "map":
            return await self._nimbus.map()
        if name == "status":
            return await self._nimbus.status()
        if name == "stop":
            return await self._nimbus.stop()
        if name == "speak_status":
            await on_status(str(args["text"]))
            return {"ok": True}
        raise ValueError(f"unknown tool {name}")

    async def handle(self, request: str, transcript: list[dict], on_status: StatusCallback) -> str:
        """Run one request to completion; returns the sentence(s) for the voice assistant to speak."""
        try:
            return await asyncio.wait_for(self._loop(request, transcript, on_status), MAX_SECONDS)
        except asyncio.TimeoutError:
            await self._safe_stop()
            return "I ran out of time on that, so I stopped."
        except asyncio.CancelledError:
            await self._safe_stop()
            raise

    async def _safe_stop(self):
        try:
            await self._nimbus.stop()
        except Exception:
            pass

    async def _loop(self, request, transcript, on_status):
        recent = "\n".join(f"{t['role']}: {t['text']}" for t in transcript[-12:])
        messages = [{"role": "user", "content": f"Recent conversation:\n{recent}\n\nRequest: {request}"}]
        for _ in range(MAX_STEPS):
            resp = await self._llm.messages.create(
                model=self._model, max_tokens=1024, system=SYSTEM, tools=TOOLS, messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                return "".join(b.text for b in resp.content if b.type == "text").strip() or "Done."
            results = []
            for call in calls:
                try:
                    out = await self._run_tool(call.name, call.input, on_status)
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(out)})
                except Exception as e:  # report to Claude so it can recover or explain
                    results.append({"type": "tool_result", "tool_use_id": call.id,
                                    "content": f"error: {e}", "is_error": True})
            messages.append({"role": "user", "content": results})
        await self._safe_stop()
        return "I took too many steps on that, so I stopped to be safe."

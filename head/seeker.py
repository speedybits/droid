"""Droid's main goal in the simulator: seek out people, listen, and join conversations when it fits.

State machine, ticked every ~0.4 s:
  seek      patrol rooms (least recently visited first); head for any person it can see
  approach  drive to ~0.7 m from a group so it is inside hearing range
  listen    stand still, log everything overheard, decide (one LLM call per burst) whether to speak
Droid cannot tell who is speaking: heard lines are logged to memory with the room and no speaker.
"""

import asyncio
import json
import logging
import math
import time
import uuid
from typing import Awaitable, Callable

from .jsonutil import parse_json
from .sim.people import HEAR_RANGE, PeopleSim

log = logging.getLogger("head.seeker")

QUIET_S = 0.4  # decide once the speaker has been quiet this long: that pause is droid's opening
HOLD_S = 8.0  # people wait this long for droid while it thinks
SPEAK_COOLDOWN_S = 6.0
SILENCE_LEAVE_S = 25.0  # nothing heard this long: move on
MAX_ENCOUNTER_S = 100.0
REVISIT_S = 150.0  # don't go straight back to a group we just left
GOTO_TIMEOUT = 25.0
LINGER_S = 8.0  # after arriving beside a silent person, wait this long for them to start talking
LEAVE_DEAF_S = 8.0  # after finishing an encounter, don't get drawn straight back in while walking away

DECIDE_SYSTEM = """You are Droid, a small, curious wheeled home robot. Your main goal right now is to find people,
listen to them, and join in the conversation when that is appropriate. You can hear only people within about a
metre, and you cannot tell who is speaking: you only know what words you heard, in what order, and which room
you are in. You are asked at a moment when the speaker has just stopped, so this IS a natural opening.
Reply with ONLY JSON: {"say": "<one short sentence>" or null, "remember": "<durable fact>" or null}.
Guidance:
- You are here to take part, not just to watch. At an opening, join in when: the last line was a question or open to
  anyone (even if not aimed at you); someone seems to address or look to you; a thought just finished and you can add
  something friendly, funny or relevant; or you have not spoken for a while and can contribute to the topic.
- Stay silent only if the moment is clearly private, tense or emotional, or you would add nothing. Do not speak
  twice in a row without being answered.
- If only one voice is speaking, aloud to nobody in particular, asking things, greeting, or pausing as if for an
  answer, it is probably talking to you: reply warmly and answer what they asked.
- Keep it natural and brief (under 20 words). You may bring up something you overheard in another room if it
  genuinely fits, saying that you heard it elsewhere, without inventing who said it.
- Never claim to know names or who said what. Use they/them for people.
- "remember" is only for a lasting fact worth keeping (a plan, a preference, a detail about the home), else null."""


class Seeker:
    def __init__(self, nimbus, people: PeopleSim, memory, llm, model: str,
                 on_say: Callable[[str], Awaitable[None]] | None = None,
                 on_face: Callable[[str], Awaitable[None]] | None = None, clock=time.monotonic):
        self.nimbus, self.people, self.memory, self.llm, self.model = nimbus, people, memory, llm, model
        self.on_say, self.on_face = on_say, on_face
        self.clock = clock
        self.state = "seek"
        self.tokens = 0
        self._paused_until: float | None = None
        self._paused = False
        self._hold = False        # the phone's voice session is mid-turn
        self._hold_until = 0.0
        self._move: asyncio.Task | None = None
        self._face = ""
        self._pose = (0.0, 0.0, 0.0)
        self._room = ""
        self._room_visit: dict[str, float] = {}
        self._engaged: dict[int, float] = {}  # group id -> when we last finished with it
        self._target_gid: int | None = None
        self._approach_tries = 0
        self._linger_until = 0.0
        self._deaf_until = 0.0
        # current encounter
        self._sid = ""
        self._enc_start = 0.0
        self._enc_gid: int | None = None
        self._enc_lines: list[tuple[float, str, str]] = []  # (t, kind "heard"|"droid", text)
        self._decided = 0  # how many entries of _enc_lines the last decision covered
        self._last_heard = 0.0
        self._last_said = -1e9
        self._deciding = False
        self._quiet_since: float | None = None
        self._bg: set[asyncio.Task] = set()

    # ---- control ----
    def pause(self, secs: float | None = None) -> None:
        self._paused = True
        self._paused_until = self.clock() + secs if secs else None

    def resume(self) -> None:
        self._paused = False
        self._paused_until = None

    def hold(self, on: bool, grace: float = 3.0) -> None:
        """The phone's voice session is talking/thinking: stand still until a moment after it finishes."""
        self._hold = on
        if not on:
            self._hold_until = self.clock() + grace

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                await asyncio.sleep(0.4)
            except asyncio.CancelledError:
                await self._halt_move()
                raise
            except Exception as e:
                log.warning("seeker tick failed: %s", e)
                await asyncio.sleep(2.0)

    # ---- one tick ----
    async def tick(self) -> None:
        now = self.clock()
        if self._paused and self._paused_until and now >= self._paused_until:
            self.resume()
        if self._paused or self._hold or now < self._hold_until:
            await self._halt_move()
            if self._paused:
                await self._set_face("idle")
            return
        st = await self.nimbus.sim_state()
        self._pose = (st["x"], st["y"], st.get("theta", 0.0))
        self.people.update_droid(self._pose[0], self._pose[1])
        room = self.people.layout.room_at(*self._pose[:2])
        self._room = room.name if room else ""
        self._room_visit[self._room] = now

        heard = self.people.heard() if (self.state == "listen" or now >= self._deaf_until) else []
        if heard and self.state != "listen":
            await self._halt_move()
            self._begin_encounter(self.people.visible())
        for u in heard:
            self._enc_lines.append((now, "heard", u.text))
            self.memory.log(self._sid, "heard", u.text, self._room)
            self._last_heard = now
            log.info("heard [%s]: %s", self._room, u.text)
        if self.state == "listen":
            await self._listen(now)
        else:
            await self._seek(now)

    # ---- seek / approach ----
    async def _seek(self, now: float) -> None:
        if self._move and not self._move.done():
            return
        if self.state == "approach":  # the drive finished
            self._finish_move()
            tgt = self._target_gid
            seen = {v["gid"]: v for v in self.people.visible()}
            v = seen.get(tgt)
            if v and v["dist"] <= HEAR_RANGE:
                self.state, self._linger_until = "linger", now + LINGER_S  # a lone person only talks once we are here
                return
            if v and self._approach_tries < 3:
                self._approach_tries += 1  # fell short (an obstacle?): try again from here
                self._start_approach(v)
                return
            self._engaged[tgt] = now
            self.state = "seek"
        elif self.state == "linger":
            if now < self._linger_until:
                return
            self._engaged[self._target_gid] = now  # they never spoke: move on
            self.state = "seek"
        self._finish_move()
        self._approach_tries = 0
        fresh = [v for v in self.people.visible() if now - self._engaged.get(v["gid"], -1e9) > REVISIT_S]
        if fresh:
            self._start_approach(fresh[0])
            return
        self._start_patrol(now)

    def _finish_move(self) -> None:
        if self._move and self._move.done() and not self._move.cancelled() and self._move.exception():
            log.warning("move failed: %s", self._move.exception())
        self._move = None

    def _start_approach(self, v: dict) -> None:
        self.state = "approach"
        self._target_gid = v["gid"]
        spot = self._spot_near(v["members"])
        log.info("approaching a group in the %s (%.1f m)", v["room"], v["dist"])
        self._move = asyncio.ensure_future(self._follow([spot]))
        asyncio.ensure_future(self._set_face("moving"))

    def _spot_near(self, members: list[tuple[float, float]]) -> tuple[float, float]:
        """Where to stand so everyone in the group is within hearing range if possible (else the nearest decent spot)."""
        g = self.people.grid
        cx = sum(m[0] for m in members) / len(members)
        cy = sum(m[1] for m in members) / len(members)
        me = self._pose[:2]
        best, best_key = None, None
        for rad in (0.3, 0.4, 0.5, 0.6, 0.7, 0.9):
            for k in range(0, 360, 15):
                c = (cx + rad * math.cos(math.radians(k)), cy + rad * math.sin(math.radians(k)))
                if not g.clear(c[0], c[1], 0.2) or not g.los(c, (cx, cy)):
                    continue
                if min(math.dist(c, m) for m in members) < 0.35:
                    continue  # don't stand on someone's toes
                excess = max(0.0, max(math.dist(c, m) for m in members) - HEAR_RANGE * 0.93)
                key = (round(excess / 0.05), math.dist(c, me))  # everyone in earshot first, then the shortest drive
                if best_key is None or key < best_key:
                    best, best_key = c, key
        return best or (cx, cy)

    def _start_patrol(self, now: float) -> None:
        lay = self.people.layout
        others = [r for r in lay.rooms if r.name != self._room]
        if not others:  # a single room: wander to a different spot
            r = lay.rooms[0]
            pts = [(r.cx, r.cy)]
        else:
            tgt = min(others, key=lambda r: self._room_visit.get(r.name, -1e9))
            pts = lay.route(self._room, tgt.name) or [(tgt.cx, tgt.cy)]
        self.state = "seek"
        self._move = asyncio.ensure_future(self._follow(pts))
        asyncio.ensure_future(self._set_face("moving"))

    async def _follow(self, waypoints: list[tuple[float, float]]) -> None:
        for wx, wy in waypoints:
            ox, oy = await self._to_odom(wx, wy)
            res = await self.nimbus.goto(ox, oy, timeout=GOTO_TIMEOUT)
            if isinstance(res, dict) and res.get("result") in ("stuck", "timeout") and not res.get("success", True):
                log.info("goto %s; giving up on this route", res.get("result"))
                return

    async def _to_odom(self, wx: float, wy: float) -> tuple[float, float]:
        """nimbus goto uses its odometry frame (origin at boot); the sim reports ground truth. Map between them."""
        try:
            st = await self.nimbus.sim_state()
            sc = await self.nimbus.scan()
            p = sc["pose"]
            oth = math.radians(90.0 - float(p["heading"]))
            phi = oth - st.get("theta", 0.0)
            dx, dy = wx - st["x"], wy - st["y"]
            return (float(p["x"]) + dx * math.cos(phi) - dy * math.sin(phi),
                    float(p["y"]) + dx * math.sin(phi) + dy * math.cos(phi))
        except Exception:
            return wx, wy  # identity if the pose can't be read

    async def _halt_move(self) -> None:
        if self._move and not self._move.done():
            self._move.cancel()
            try:
                await self.nimbus.stop()
            except Exception:
                log.warning("nimbus stop failed")
        self._move = None

    # ---- listen ----
    def _begin_encounter(self, visible: list[dict]) -> None:
        self.state = "listen"
        self._sid = f"enc-{uuid.uuid4().hex[:8]}"
        self._enc_start = self._last_heard = self.clock()
        self._enc_lines = []
        self._decided = 0
        self._quiet_since = None
        near = min(visible, key=lambda v: v["dist"])["gid"] if visible else None
        self._enc_gid = near
        asyncio.ensure_future(self._set_face("listening"))
        log.info("listening in the %s", self._room)

    async def _listen(self, now: float) -> None:
        if self.llm is None:  # no brain to decide with: just listen
            self._decided = len(self._enc_lines)
        talking = self.people.speaking_near()
        if talking:
            self._quiet_since = None
        elif self._quiet_since is None:
            self._quiet_since = now
        pending = len(self._enc_lines) > self._decided
        opening = self._quiet_since is not None and now - self._quiet_since >= QUIET_S
        if pending and opening and not self._deciding and now - self._last_said >= SPEAK_COOLDOWN_S:
            self._deciding = True
            self.people.hold_floor(HOLD_S)  # they leave the pause open while we think
            self._track(asyncio.ensure_future(self._decide()))
        if (now - self._last_heard > SILENCE_LEAVE_S and not self._deciding) or now - self._enc_start > MAX_ENCOUNTER_S:
            await self._end_encounter(now)

    async def _decide(self) -> None:
        try:
            lines = list(self._enc_lines)
            covered = len(lines)
            t0 = lines[0][0] if lines else self.clock()
            convo = "\n".join(f"  [{t - t0:4.0f}s] {'you said' if k == 'droid' else 'heard'}: {x}" for t, k, x in lines[-14:])
            memory = self.memory.recall(self._room)
            facts = self.memory.facts()
            prompt = (f"You are in the {self._room}.\n"
                      + (f"Things you know:\n" + "\n".join(f"- {f}" for f in facts) + "\n\n" if facts else "")
                      + (f"Your memory of other times and rooms:\n{memory}\n\n" if memory else "")
                      + f"This encounter so far (you stand about a metre from them):\n{convo}\n\n"
                      + f"The speaker has just stopped: there is a pause now. It is {self.clock() - self._last_said:.0f} s since "
                        f"you last spoke{' (you have not spoken yet here)' if self._last_said < self._enc_start else ''}. "
                        f"The last thing heard {'was a question' if self._ends_q(lines) else 'was a statement'}.\n"
                      + "Should you say something now?")
            data = {}
            for attempt in range(2):  # an empty or garbled reply gets one retry
                resp = await self.llm.messages.create(model=self.model, max_tokens=500, system=DECIDE_SYSTEM,
                                                      messages=[{"role": "user", "content": prompt}])
                u = getattr(resp, "usage", None)
                self.tokens += (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "output_tokens", 0) or 0)
                raw = "".join(b.text for b in resp.content if b.type == "text")
                try:
                    data = parse_json(raw)
                    break
                except ValueError as e:
                    log.warning("decision reply unusable (attempt %d, stop=%s): %s | %r", attempt + 1,
                                getattr(resp, "stop_reason", "?"), e, raw[:400])
            self._decided = covered
            if isinstance(data.get("remember"), str) and data["remember"].strip():
                self.memory.add_fact(data["remember"])
            say = data.get("say")
            if isinstance(say, str) and say.strip() and self.state == "listen":
                await self.speak(say.strip())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("decision failed: %s", e)
            self._decided = len(self._enc_lines)  # don't hammer a failing model
        finally:
            self._deciding = False
            self.people.release_floor()

    @staticmethod
    def _ends_q(lines) -> bool:
        heard = [x for _, k, x in lines if k == "heard"]
        return bool(heard) and heard[-1].rstrip().endswith("?")

    async def speak(self, text: str) -> None:
        now = self.clock()
        self._last_said = now
        self._enc_lines.append((now, "droid", text))
        self._decided = len(self._enc_lines)
        self.memory.log(self._sid or "seeker", "droid", text, self._room)
        self.people.say_bubble(text)
        heard_by = self.people.hear_droid(text)
        log.info("droid says [%s] (%d group(s) heard): %s", self._room, heard_by, text)
        self._last_heard = now  # they will answer; don't leave while waiting
        if self.on_say:
            await self.on_say(text)

    async def _end_encounter(self, now: float) -> None:
        log.info("leaving the %s after %.0f s", self._room, now - self._enc_start)
        if self._enc_gid is not None:
            self._engaged[self._enc_gid] = now
        for v in self.people.visible():  # anyone else in earshot of where we stood counts as visited
            if v["dist"] <= HEAR_RANGE * 1.5:
                self._engaged[v["gid"]] = now
        sid, heard = self._sid, sum(k == "heard" for _, k, _ in self._enc_lines)
        if heard >= 2 and self.llm is not None:
            self._track(asyncio.ensure_future(self.memory.summarize(self.llm, self.model, sid)))
        self.state = "seek"
        self._deaf_until = now + LEAVE_DEAF_S
        self._enc_lines, self._decided, self._sid = [], 0, ""
        self._target_gid = None
        await self._set_face("idle")

    # ---- helpers ----
    def _track(self, t: asyncio.Task) -> None:
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _set_face(self, state: str) -> None:
        if state != self._face and self.on_face:
            self._face = state
            await self.on_face(state)

    def snapshot(self) -> dict:
        return {"state": self.state, "paused": self._paused or self._hold, "room": self._room, "pose": self._pose}

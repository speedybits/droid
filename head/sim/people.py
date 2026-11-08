"""Simulated people having conversations in the rooms of the nimbus sim world.

Multi-person groups talk from the moment the sim starts, whether or not droid is near, so droid usually
arrives mid-conversation. A person who is alone says nothing until droid comes within HEAR_RANGE, then talks
to it. Names exist only here (for the viewer): droid is never told who is speaking.

Lines come from a cheap Claude model, a whole short script per call (coherent and inexpensive), played back
on a timer. If droid speaks near a group the rest of the script is dropped and regenerated with droid's line
in the history, so the people can react. With no LLM (tests, no API key) a scripted pool is used.
"""

import asyncio
import itertools
import json
import logging
import math
import random
import time
from dataclasses import dataclass, field

from ..jsonutil import parse_json
from .geometry import Grid, Layout

log = logging.getLogger("head.people")

HEAR_RANGE = 1.0  # metres: droid hears a speaker only within this distance
SIGHT_RANGE = 3.5  # metres: LIDAR range, used for "sight" (who is where, no words)
BUBBLE_MIN_S = 3.5

TOPICS = [
    "what to cook for dinner tonight", "a weekend trip to the coast", "a show they have been binge-watching",
    "whether the neighbour's dog barks too much", "planning a surprise birthday party", "a leaky kitchen tap",
    "learning to play the guitar", "their favourite childhood snacks", "how bad the traffic was this morning",
    "adopting a cat", "a book someone just finished", "whether pineapple belongs on pizza",
    "fixing the bike in the garage", "a strange dream from last night", "the best way to learn a language",
    "growing tomatoes on the balcony", "a video game they keep losing at", "the new coffee place downtown",
    "who forgot to take the bins out", "what the weather will do this weekend",
]
NAMES = ["Ava", "Ben", "Chloe", "Dev", "Elena", "Femi", "Gus", "Hana", "Ivan", "Jo", "Kai", "Lena", "Milo",
         "Noor", "Omar", "Pia", "Quinn", "Rosa", "Sam", "Tara", "Uma", "Vik", "Wren", "Yara"]
VIBES = ["cheerful", "dry-humoured", "a bit tired", "excitable", "skeptical", "thoughtful", "chatty", "laid-back"]

GROUP_SYSTEM = """You write realistic overheard chatter between people in a house. Reply with ONLY JSON:
{"lines": [{"who": <person index>, "text": "<what they say>"}, ...]}
Each line is one or two short sentences of natural speech (under 25 words), no stage directions, no names
in brackets. People answer each other and the topic can wander a little. Never mention scripts or writing."""

SOLO_SYSTEM = """You write what a person says out loud when they are alone in a room and a small wheeled
robot has just rolled up beside them. They talk TO the robot the way you would to a curious pet or a helper:
musing, asking it things, commenting on the room, sometimes pausing for an answer. Do not say the robot
must reply. Reply with ONLY JSON: {"lines": [{"who": 0, "text": "<what they say>"}, ...]}
Each line is one or two short sentences (under 25 words), natural speech, no stage directions."""

FALLBACK_GROUP = [
    "Did you see what happened with {t}? I could not stop thinking about it.",
    "Honestly I still have no idea what to make of {t}.",
    "Right? That is exactly what I said last time.",
    "Hold on, are we actually going to do something about {t} or just talk?",
    "Ha, fair. Let me think about it a bit more.",
    "You always say that, and then it turns out you were right.",
    "Okay, okay. Maybe tomorrow, once we have slept on it.",
    "Anyway, back to {t}. What would you do?",
]
FALLBACK_SOLO = [
    "Oh, hello there. Where did you come from?",
    "Quiet in here today, isn't it? I was just thinking about {t}.",
    "Do you ever wonder about {t}? Seems like a silly thing to ponder.",
    "What do you think? Go on, I am listening.",
    "Hm. Well, that is a thought. I will chew on it.",
]
FALLBACK_REACT = ["Oh! Did you hear that? Something just said something.", "Ha, you are a chatty little thing."]


@dataclass
class Person:
    name: str
    vibe: str
    x: float
    y: float


@dataclass
class Utterance:
    id: int
    gid: int
    room: str
    who: int
    name: str
    x: float
    y: float
    text: str
    t0: float
    t1: float


@dataclass
class Group:
    id: int
    room: str
    people: list[Person]
    solo: bool  # alone: talks to droid, only when droid is in range
    topic: str = ""
    history: list[str] = field(default_factory=list)
    script: list[tuple[int, str]] = field(default_factory=list)
    current: Utterance | None = None
    next_at: float = 0.0
    spoken: int = 0
    max_lines: int = 10
    pending: asyncio.Task | None = None
    reacting: bool = False  # droid just spoke; the next script should react to it
    last_near: float = 0.0  # last time droid was in range (solo groups)
    hold_until: float = 0.0  # people pause while droid is deciding whether to speak

    @property
    def center(self) -> tuple[float, float]:
        return (sum(p.x for p in self.people) / len(self.people), sum(p.y for p in self.people) / len(self.people))


def partition(n: int, rng: random.Random) -> list[int]:
    """Split n people into conversations. Only a lone occupant is a group of one; nobody is left out alone."""
    if n == 4 and rng.random() < 0.5:
        return [2, 2]
    return [n]


class PeopleSim:
    def __init__(self, world: dict, llm=None, model: str = "claude-haiku-5-5", rng: random.Random | None = None,
                 avoid: tuple[float, float] | None = None, clock=time.monotonic, max_llm: int = 3):
        self.grid = Grid(world)
        self.layout = Layout(self.grid)
        self.rng = rng or random.Random()
        self.llm, self.model = llm, model
        self.clock = clock
        self.groups: list[Group] = []
        self.droid: tuple[float, float] | None = None
        self.droid_bubble: tuple[str, float] | None = None  # (text, expires)
        self._ids = itertools.count(1)
        self._delivered: set[int] = set()
        self._sem = asyncio.Semaphore(max_llm)
        self.tokens = 0
        self._build(avoid)

    # ---- construction ----
    def _build(self, avoid) -> None:
        gid = itertools.count(1)
        used_names: list[str] = []
        for room in self.layout.rooms:
            n = self.rng.randint(1, 4)
            centers: list[tuple[float, float]] = []
            for size in partition(n, self.rng):
                g = self._place_group(room, size, centers, avoid, next(gid), used_names)
                if g:
                    centers.append(g.center)
                    self.groups.append(g)

    def _place_group(self, room, size, centers, avoid, gid, used_names) -> Group | None:
        for sep in (2.2, 1.6, 1.0):
            for _ in range(60):
                c = self.layout.random_free(room, self.rng, r=0.65 if size > 1 else 0.4)
                if not c:
                    return None
                if any(math.dist(c, o) < sep for o in centers):
                    continue
                if avoid and math.dist(c, avoid) < 1.6:
                    continue
                pts = self._ring(c, size)
                if pts:
                    pool = [n for n in NAMES if n not in used_names]
                    people = []
                    for (x, y) in pts:
                        name = self.rng.choice(pool)
                        pool.remove(name)
                        used_names.append(name)
                        people.append(Person(name, self.rng.choice(VIBES), x, y))
                    g = Group(gid, room.name, people, solo=(size == 1), topic=self.rng.choice(TOPICS))
                    g.max_lines = self.rng.randint(8, 15)
                    g.next_at = self.clock() + self.rng.uniform(0.2, 2.5)  # conversations are already under way
                    return g
        return None

    def _ring(self, c, size) -> list[tuple[float, float]] | None:
        if size == 1:
            return [c]
        rad = 0.4 if size == 2 else 0.5
        base = self.rng.uniform(0, math.tau)
        pts = [(c[0] + rad * math.cos(base + k * math.tau / size), c[1] + rad * math.sin(base + k * math.tau / size))
               for k in range(size)]
        return pts if all(self.grid.clear(x, y, 0.2) for x, y in pts) else None

    # ---- droid-facing API ----
    def update_droid(self, x: float, y: float) -> None:
        self.droid = (x, y)

    def _near(self, g: Group) -> bool:
        return self.droid is not None and any(math.dist((p.x, p.y), self.droid) <= HEAR_RANGE for p in g.people)

    def heard(self, now: float | None = None) -> list[Utterance]:
        """Lines being spoken right now by someone within HEAR_RANGE of droid, each delivered once."""
        now = self.clock() if now is None else now
        out = []
        for g in self.groups:
            u = g.current
            if u and u.id not in self._delivered and now < u.t1 and self.droid \
                    and math.dist((u.x, u.y), self.droid) <= HEAR_RANGE:
                self._delivered.add(u.id)
                out.append(u)
        return out

    def hear_droid(self, text: str, now: float | None = None) -> int:
        """Droid spoke aloud: groups within HEAR_RANGE hear it and will react. Returns how many heard."""
        now = self.clock() if now is None else now
        heard = 0
        for g in self.groups:
            if self._near(g):
                heard += 1
                g.history.append(f"[a small robot that rolled up beside them]: {text}")
                g.script.clear()
                if g.pending:
                    g.pending.cancel()
                    g.pending = None
                g.reacting = True
                g.next_at = max(g.current.t1 if g.current else now, now) + self.rng.uniform(1.0, 2.0)
        return heard

    def speaking_near(self, now: float | None = None) -> bool:
        """Is anyone in a group within earshot of droid mid-sentence right now? (droid can see their mouths)"""
        now = self.clock() if now is None else now
        return any(g.current and now < g.current.t1 and self._near(g) for g in self.groups)

    def hold_floor(self, secs: float, now: float | None = None) -> None:
        """Nearby groups wait a moment (they notice the robot is about to say something)."""
        now = self.clock() if now is None else now
        for g in self.groups:
            if self._near(g):
                g.hold_until = max(g.hold_until, now + secs)

    def release_floor(self) -> None:
        for g in self.groups:
            g.hold_until = 0.0

    def say_bubble(self, text: str, secs: float | None = None, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        self.droid_bubble = (text, now + (secs or max(BUBBLE_MIN_S, 2 + 0.06 * len(text))))

    def visible(self) -> list[dict]:
        """Groups droid could see: nearest person within SIGHT_RANGE and in line of sight. Position only, no words."""
        if not self.droid:
            return []
        out = []
        for g in self.groups:
            best = min(g.people, key=lambda p: math.dist((p.x, p.y), self.droid))
            d = math.dist((best.x, best.y), self.droid)
            if d <= SIGHT_RANGE and self.grid.los(self.droid, (best.x, best.y)):
                out.append({"gid": g.id, "room": g.room, "dist": d, "x": best.x, "y": best.y, "people": len(g.people),
                            "members": [(p.x, p.y) for p in g.people]})
        return sorted(out, key=lambda v: v["dist"])

    # ---- playback ----
    def step(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        for g in self.groups:
            if g.current and now >= g.current.t1:
                g.current = None
            if g.current or now < g.next_at or now < g.hold_until:
                continue
            if g.solo:
                if self._near(g):
                    g.last_near = now
                elif g.script and now - g.last_near > 20:
                    g.script.clear()  # they wandered off; start afresh next visit
                    g.spoken = 0
                    continue
                else:
                    continue
            if g.script:
                self._speak(g, now)
            elif not g.pending:
                if not g.solo and g.spoken >= g.max_lines:  # that conversation has run its course
                    g.topic, g.history, g.spoken = self.rng.choice(TOPICS), [], 0
                    g.max_lines = self.rng.randint(8, 15)
                    g.next_at = now + self.rng.uniform(3, 8)
                    continue
                self._request_script(g)
                if g.script:  # scripted fallback is instant; an LLM script arrives later via the task
                    self._speak(g, now)

    def _speak(self, g: Group, now: float) -> None:
        who, text = g.script.pop(0)
        who = min(max(who, 0), len(g.people) - 1)
        p = g.people[who]
        dur = max(BUBBLE_MIN_S, 2.0 + 0.06 * len(text))
        g.current = Utterance(next(self._ids), g.id, g.room, who, p.name, p.x, p.y, text, now, now + dur)
        g.history.append(f"{p.name}: {text}")
        g.history = g.history[-12:]
        g.spoken += 1
        if g.solo:
            gap = self.rng.uniform(4, 7)
        elif text.rstrip().endswith("?") or self.rng.random() < 0.3:
            gap = self.rng.uniform(3.5, 6.0)  # a question hangs in the air, or a thought is finished: room for someone else
        else:
            gap = self.rng.uniform(0.8, 2.2)
        g.next_at = now + dur + gap

    def _request_script(self, g: Group) -> None:
        reacting, g.reacting = g.reacting, False
        if self.llm is None:
            g.script = self._fallback(g, reacting)
            return
        g.pending = asyncio.ensure_future(self._generate(g, reacting))

    def _fallback(self, g: Group, reacting: bool) -> list[tuple[int, str]]:
        n = len(g.people)
        if g.solo:
            pool = FALLBACK_REACT + FALLBACK_SOLO if reacting else FALLBACK_SOLO
            return [(0, s.format(t=g.topic)) for s in pool]
        lines = [(i % n, s.format(t=g.topic)) for i, s in enumerate(FALLBACK_GROUP)]
        return ([(0, FALLBACK_REACT[0])] + lines) if reacting else lines

    async def _generate(self, g: Group, reacting: bool) -> None:
        try:
            async with self._sem:
                who = "\n".join(f"{i}: {p.name} ({p.vibe})" for i, p in enumerate(g.people))
                hist = "\n".join(g.history[-8:]) or "(they have only just started talking)"
                if g.solo:
                    system = SOLO_SYSTEM
                    ask = (f"{g.people[0].name} is {g.people[0].vibe} and alone in the {g.room}. They were thinking "
                           f"about: {g.topic}.\nWhat has been said so far:\n{hist}\n"
                           f"Write the next {self.rng.randint(3, 5)} lines.")
                else:
                    system = GROUP_SYSTEM
                    ask = (f"People in the {g.room}:\n{who}\nTopic: {g.topic}\nConversation so far:\n{hist}\n"
                           + ("Someone just noticed the small robot's remark above; one or two people react naturally, "
                              "then the chat carries on. " if reacting else "")
                           + f"Write the next {self.rng.randint(5, 8)} lines, continuing naturally.")
                data = None
                for attempt in range(2):  # models now and then return nothing usable: ask once more
                    resp = await self.llm.messages.create(model=self.model, max_tokens=900, system=system,
                                                          messages=[{"role": "user", "content": ask}])
                    u = getattr(resp, "usage", None)
                    self.tokens += (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "output_tokens", 0) or 0)
                    try:
                        data = parse_json("".join(b.text for b in resp.content if b.type == "text"))
                        break
                    except ValueError as e:
                        log.warning("dialogue reply unusable (attempt %d): %s", attempt + 1, e)
                if data is None:
                    raise ValueError("no usable reply")
                lines = [(int(l.get("who", 0)), str(l["text"]).strip()[:200])
                         for l in data["lines"] if str(l.get("text", "")).strip()]
                if not lines:
                    raise ValueError("no lines")
                g.script = lines
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("dialogue generation failed (%s); using scripted lines", e)
            g.script = self._fallback(g, reacting)
        finally:
            g.pending = None

    async def run(self, interval: float = 0.25) -> None:
        while True:
            self.step()
            await asyncio.sleep(interval)

    # ---- view ----
    def snapshot(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        people, bubbles = [], []
        for g in self.groups:
            for i, p in enumerate(g.people):
                speaking = bool(g.current and g.current.who == i and now < g.current.t1)
                people.append({"name": p.name, "x": p.x, "y": p.y, "gid": g.id, "room": g.room, "solo": g.solo,
                               "speaking": speaking})
            if g.current and now < g.current.t1:
                u = g.current
                bubbles.append({"gid": g.id, "x": u.x, "y": u.y, "text": u.text, "name": u.name,
                                "heard": bool(self.droid and math.dist((u.x, u.y), self.droid) <= HEAR_RANGE)})
        droid = None
        if self.droid:
            r = self.layout.room_at(*self.droid)
            bub = self.droid_bubble[0] if self.droid_bubble and now < self.droid_bubble[1] else None
            droid = {"x": self.droid[0], "y": self.droid[1], "room": r.name if r else None, "bubble": bub}
        return {"people": people, "bubbles": bubbles, "droid": droid, "hear_range": HEAR_RANGE,
                "rooms": [{"name": r.name, "x": r.cx, "y": r.cy} for r in self.layout.rooms]}

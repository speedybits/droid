import asyncio
import math
import random

from head.seeker import Seeker
from head.server.memory import MemoryStore
from head.sim.people import Group, PeopleSim, Person


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class Resp:
    usage = None

    def __init__(self, text):
        self.content = [Block(text)]


class FakeLLM:
    """Decision calls get a canned reply; people-dialogue calls (GROUP/SOLO system) get a canned script."""

    def __init__(self, say='{"say": "Hi, I am Droid!", "remember": null}'):
        self.say, self.calls = say, []

        class _M:
            async def create(inner, **kw):
                self.calls.append(kw)
                if "overheard chatter" in kw["system"] or "robot has just rolled up" in kw["system"]:
                    return Resp('{"lines": [{"who": 0, "text": "line one"}, {"who": 1, "text": "line two"}]}')
                return Resp(self.say)

        self.messages = _M()


class FakeNimbus:
    """Ground-truth pose in world coordinates; goto teleports there (odometry frame == world frame)."""

    def __init__(self, x, y):
        self.x, self.y, self.theta = x, y, 0.0
        self.gotos, self.stops = [], 0

    async def sim_state(self):
        return {"x": self.x, "y": self.y, "theta": self.theta}

    async def scan(self):
        return {"pose": {"x": self.x, "y": self.y, "heading": 90.0 - math.degrees(self.theta)}}

    async def goto(self, x, y, timeout=60.0):
        self.gotos.append((x, y))
        self.x, self.y = x, y
        return {"success": True, "result": "reached"}

    async def stop(self):
        self.stops += 1


def rig(world, tmp_path, groups, start, llm=None):
    clock = Clock()
    sim = PeopleSim(world, rng=random.Random(2), clock=clock)
    sim.groups = groups
    nim = FakeNimbus(*start)
    mem = MemoryStore(tmp_path / "mem")
    said = []

    async def on_say(t):
        said.append(t)

    sk = Seeker(nim, sim, mem, llm or FakeLLM(), "m", on_say=on_say, clock=clock)
    return clock, sim, nim, mem, sk, said


async def settle(sk):
    for _ in range(5):
        await asyncio.sleep(0)
    while sk._bg:
        await asyncio.gather(*list(sk._bg))


async def test_find_lone_person_listen_and_speak_back(world, tmp_path):
    ava = Group(1, "kitchen", [Person("Ava", "calm", 2.5, 2.5)], solo=True, topic="pizza")
    clock, sim, nim, mem, sk, said = rig(world, tmp_path, [ava], start=(1.0, 1.0))

    await sk.tick()                       # sees her (no words) -> approach
    assert sk.state == "approach"
    await sk._move
    clock.t += 0.4
    await sk.tick()                       # arrived, within 1 m -> linger so she can start talking
    assert sk.state == "linger"
    assert math.dist((nim.x, nim.y), (2.5, 2.5)) < 1.0
    sim.step()                            # she hears the robot is near and starts to speak
    await sk.tick()                       # hears her -> listening
    assert sk.state == "listen"
    clock.t += 1.0
    await sk.tick()
    assert not sk._deciding               # she is still mid-sentence: not droid's moment yet
    clock.t += 4.0
    sim.step()
    await sk.tick()                       # she finished: a pause opens
    clock.t += 0.5
    await sk.tick()                       # quiet long enough -> decide
    assert sim.groups[0].hold_until > clock.t  # and she waits for droid while it thinks
    await settle(sk)
    assert said == ["Hi, I am Droid!"]
    assert sim.groups[0].hold_until == 0.0
    recs = mem._records()
    assert [r["role"] for r in recs] == ["heard", "droid"] and all(r["room"] == "kitchen" for r in recs)
    assert ava.reacting                   # she heard the robot and will respond
    assert "[a small robot" in ava.history[-1]
    assert sim.snapshot()["droid"]["bubble"] == "Hi, I am Droid!"


async def test_does_not_hear_beyond_one_metre_and_wanders_on(world, tmp_path):
    g = Group(1, "kitchen", [Person("A", "x", 3.4, 3.4), Person("B", "x", 3.4, 3.1)], solo=False)
    g.script = [(0, "chatter") for _ in range(30)]
    g.next_at = 0
    clock, sim, nim, mem, sk, said = rig(world, tmp_path, [g], start=(2.2, 3.3))  # 1.2 m away, in sight
    sim.step()
    assert sim.groups[0].current is not None               # they are talking
    await sk.tick()
    assert sk.state == "approach" and mem._records() == []  # seen, but nothing heard beyond 1 m
    await sk._move
    clock.t += 0.4
    sim.step()
    await sk.tick()
    sim.step()
    assert math.dist((nim.x, nim.y), (3.4, 3.4)) < 1.0     # now in range
    clock.t += 5
    sim.step()
    await sk.tick()
    assert mem._records() and all(r["role"] == "heard" for r in mem._records())


async def test_patrols_to_another_room_when_nobody_in_sight(world, tmp_path):
    clock, sim, nim, mem, sk, said = rig(world, tmp_path, [], start=(-3.0, -3.0))
    await sk.tick()
    await sk._move
    assert nim.gotos and sk.state == "seek"


async def test_carries_memory_to_the_next_room(world, tmp_path):
    llm = FakeLLM()
    kit = Group(1, "kitchen", [Person("A", "x", 2.5, 2.5), Person("B", "x", 2.8, 2.5)], solo=False)
    clock, sim, nim, mem, sk, said = rig(world, tmp_path, [kit], start=(2.5, 1.8), llm=llm)
    kit.script = [(0, "we should book the Lisbon flights")]
    kit.next_at = clock.t
    sim.step()
    await sk.tick()                       # starts listening
    assert sk.state == "listen"
    kit.hold_until = clock.t + 100        # they pause after that line
    clock.t += 6.0
    sim.step()
    await sk.tick()
    clock.t += 0.5
    await sk.tick()
    await settle(sk)
    # walk to the office and decide there: the prompt must include what happened in the kitchen
    nim.x, nim.y = -2.5, 2.5
    sk._room = "office"
    mem.log("enc-x", "heard", "the tap is leaking", "office")
    sk._enc_lines.append((clock.t, "heard", "the tap is leaking"))
    sk._decided = 0
    sk._last_said = -1e9
    sk._last_heard = clock.t - 5
    llm.calls.clear()
    await sk._decide()
    prompt = llm.calls[-1]["messages"][0]["content"]
    assert "Lisbon flights" in prompt and "In the kitchen" in prompt and "you said: Hi, I am Droid!" in prompt
    assert "you are here now" in prompt


async def test_pause_stops_the_robot_and_resume_restarts(world, tmp_path):
    clock, sim, nim, mem, sk, said = rig(world, tmp_path, [], start=(-3.0, -3.0))
    await sk.tick()
    assert sk._move is not None
    sk.pause()
    await sk.tick()
    assert sk._move is None
    sk.resume()
    await sk.tick()
    assert sk.state == "seek" and sk._move is not None


async def test_people_script_comes_from_llm_and_reaction_regenerates(world, tmp_path):
    llm = FakeLLM()
    clock = Clock()
    sim = PeopleSim(world, llm=llm, model="h", rng=random.Random(5), clock=clock)
    g = Group(1, "kitchen", [Person("A", "x", 2.5, 2.5), Person("B", "x", 2.8, 2.5)], solo=False, topic="pizza")
    sim.groups = [g]
    g.next_at = clock.t
    sim.step()
    await g.pending
    assert [t for _, t in g.script] == ["line one", "line two"]
    sim.step()
    assert g.current.text == "line one"
    sim.update_droid(2.5, 3.2)
    sim.hear_droid("hello")
    assert g.script == [] and g.reacting
    clock.t += 10
    sim.step()                            # needs a new script: LLM asked with the robot's line in history
    await g.pending
    assert "small robot" in llm.calls[-1]["messages"][0]["content"] and "hello" in llm.calls[-1]["messages"][0]["content"]


async def test_llm_failure_falls_back_to_scripted_lines(world):
    class Boom:
        class messages:
            @staticmethod
            async def create(**kw):
                raise RuntimeError("down")

    clock = Clock()
    sim = PeopleSim(world, llm=Boom(), rng=random.Random(5), clock=clock)
    g = Group(1, "kitchen", [Person("A", "x", 2.5, 2.5), Person("B", "x", 2.8, 2.5)], solo=False, topic="pizza")
    sim.groups = [g]
    g.next_at = clock.t
    sim.step()
    await g.pending
    assert g.script


async def test_decision_retries_once_on_empty_reply(world, tmp_path):
    replies = iter(["", '```json\n{"say": "Hello!", "remember": "the couch is big"}\n```'])

    class Flaky:
        class messages:
            @staticmethod
            async def create(**kw):
                return Resp(next(replies))

    ava = Group(1, "kitchen", [Person("Ava", "calm", 2.5, 2.5)], solo=True)
    clock, sim, nim, mem, sk, said = rig(world, tmp_path, [ava], start=(2.5, 2.0), llm=Flaky())
    sk.state, sk._room, sk._sid = "listen", "kitchen", "enc-1"
    sk._enc_lines.append((clock.t, "heard", "hello little robot"))
    await sk._decide()
    assert said == ["Hello!"] and mem.facts() == ["the couch is big"]


async def test_approach_spot_is_within_earshot_of_everyone(world, tmp_path):
    for n, rad in ((2, 0.4), (4, 0.5)):
        cx, cy = 2.5, 2.5
        people = [Person(f"P{k}", "x", cx + rad * math.cos(k * math.tau / n), cy + rad * math.sin(k * math.tau / n))
                  for k in range(n)]
        g = Group(1, "kitchen", people, solo=False)
        clock, sim, nim, mem, sk, said = rig(world, tmp_path, [g], start=(1.0, 1.0))
        await sk.tick()
        await sk._move
        assert all(math.dist((nim.x, nim.y), (p.x, p.y)) <= 1.0 for p in people), n


async def test_questions_and_finished_thoughts_leave_a_pause(world):
    clock = Clock()
    gaps = {"q": [], "s": []}
    for seed in range(40):
        sim = PeopleSim(world, rng=random.Random(seed), clock=clock)
        g = Group(1, "kitchen", [Person("A", "x", 2.5, 2.5), Person("B", "x", 2.8, 2.5)], solo=False)
        sim.groups = [g]
        for text, key in (("Are you coming too?", "q"), ("It rained all day.", "s")):
            g.script = [(0, text)]
            g.current, g.next_at = None, 0
            sim._speak(g, clock.t)
            gaps[key].append(g.next_at - g.current.t1)
    assert min(gaps["q"]) >= 3.5                       # a question always leaves room to answer
    assert max(gaps["s"]) >= 3.5 and min(gaps["s"]) < 2.3  # statements sometimes do, sometimes run on


async def test_held_floor_pauses_the_group_until_released(world):
    clock = Clock()
    sim = PeopleSim(world, rng=random.Random(1), clock=clock)
    g = Group(1, "kitchen", [Person("A", "x", 2.5, 2.5), Person("B", "x", 2.8, 2.5)], solo=False)
    g.script = [(0, "one"), (1, "two")]
    sim.groups = [g]
    sim.update_droid(2.5, 3.0)
    sim.step()
    assert g.spoken == 1 and sim.speaking_near()
    clock.t += 20
    sim.step()
    assert g.spoken == 2                                # ran on without a hold
    g.script, g.next_at, g.current = [(0, "three")], clock.t, None
    sim.hold_floor(8)
    sim.step()
    assert g.spoken == 2                                # waiting for droid
    sim.release_floor()
    sim.step()
    assert g.spoken == 3

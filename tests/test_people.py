import math
import random

from head.sim.geometry import Grid, Layout
from head.sim.people import HEAR_RANGE, Group, Person, PeopleSim


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(world, seed=1, **kw):
    clock = Clock()
    return PeopleSim(world, rng=random.Random(seed), clock=clock, **kw), clock


def test_layout_finds_four_rooms_and_doors(world):
    lay = Layout(Grid(world))
    assert sorted(r.name for r in lay.rooms) == ["bedroom", "kitchen", "living room", "office"]
    assert len(lay.doors) == 4
    for d in lay.doors:  # each door sits in a gap of the cross wall
        assert not lay.grid.blocked(d.x, d.y)
    # diagonal rooms need two hops through a door each: 3 waypoints per hop plus the destination
    assert len(lay.route("living room", "kitchen")) == 7
    assert lay.route("living room", "living room") == [(lay.room_named("living room").cx, lay.room_named("living room").cy)]


def test_one_to_four_people_per_room_on_free_cells(world):
    for seed in range(15):
        sim, _ = make(world, seed)
        by_room = {}
        for g in sim.groups:
            by_room.setdefault(g.room, []).append(g)
            for p in g.people:
                assert sim.grid.clear(p.x, p.y, 0.15)
                assert sim.layout.room_at(p.x, p.y).name == g.room
        assert set(by_room) == {r.name for r in sim.layout.rooms}
        for gs in by_room.values():
            n = sum(len(g.people) for g in gs)
            assert 1 <= n <= 4
            # a person is alone in a group only when alone in the whole room
            assert all(len(g.people) >= 2 for g in gs) or n == 1


def test_four_people_can_form_two_conversations(world):
    shapes = set()
    for seed in range(60):
        sim, _ = make(world, seed)
        for room in {g.room for g in sim.groups}:
            gs = [g for g in sim.groups if g.room == room]
            if sum(len(g.people) for g in gs) == 4:
                shapes.add(tuple(sorted(len(g.people) for g in gs)))
    assert (2, 2) in shapes and (4,) in shapes


def test_groups_talk_from_the_start_without_droid(world):
    sim, clock = make(world, 3)
    multi = [g for g in sim.groups if not g.solo]
    assert multi
    for _ in range(20):
        clock.t += 0.5
        sim.step()
    assert all(g.spoken > 0 or g.current for g in multi)  # already mid-conversation, droid or no droid


def test_lone_person_silent_until_droid_in_range_then_talks(world):
    sim, clock = make(world)
    sim.groups = [Group(1, "kitchen", [Person("Ava", "calm", 2.0, 2.0)], solo=True, topic="pizza")]
    sim.update_droid(0.8, 1.0)  # 1.5 m away
    for _ in range(40):
        clock.t += 0.5
        sim.step()
    assert sim.groups[0].current is None and sim.groups[0].spoken == 0
    sim.update_droid(2.0, 2.8)  # 0.8 m
    clock.t += 0.5
    sim.step()
    clock.t += 0.3
    sim.step()
    assert sim.groups[0].spoken >= 1


def test_hearing_range_boundary_and_delivered_once(world):
    sim, clock = make(world)
    sim.groups = [Group(1, "kitchen", [Person("Ava", "calm", 2.0, 2.0), Person("Ben", "calm", 2.3, 2.0)],
                        solo=False, topic="pizza")]
    sim.groups[0].next_at = clock.t
    sim.step()
    u = sim.groups[0].current
    assert u is not None
    sx, sy = u.x, u.y
    sim.update_droid(sx + HEAR_RANGE + 0.01, sy)
    assert sim.heard() == []
    sim.update_droid(sx + HEAR_RANGE - 0.01, sy)
    assert [h.text for h in sim.heard()] == [u.text]
    assert sim.heard() == []  # same line is not delivered twice
    clock.t = u.t1 + 0.1
    sim.step()
    assert sim.heard() == []  # nobody speaking, nothing heard


def test_droid_speech_reaches_only_groups_in_range_and_triggers_reaction(world):
    sim, clock = make(world)
    near = Group(1, "kitchen", [Person("A", "x", 2.0, 2.0), Person("B", "x", 2.3, 2.0)], solo=False)
    far = Group(2, "office", [Person("C", "x", -2.0, 2.0), Person("D", "x", -2.3, 2.0)], solo=False)
    near.script, far.script = [(0, "old line")], [(0, "old line")]
    sim.groups = [near, far]
    sim.update_droid(2.0, 2.7)
    assert sim.hear_droid("hello there") == 1
    assert near.reacting and near.script == [] and "hello there" in near.history[-1]
    assert not far.reacting and far.script


def test_visible_needs_line_of_sight_and_range(world):
    sim, _ = make(world)
    sim.groups = [Group(1, "kitchen", [Person("A", "x", 1.5, 1.5)], solo=True),
                  Group(2, "office", [Person("B", "x", -1.5, 1.5)], solo=True)]
    sim.update_droid(0.8, 1.5)  # kitchen person is 0.7 m away; the office person is behind the cross wall
    vis = sim.visible()
    assert [v["gid"] for v in vis] == [1]
    sim.update_droid(3.9, -3.9)
    assert sim.visible() == []  # too far / blocked


def test_snapshot_marks_heard_bubbles(world):
    sim, clock = make(world)
    g = Group(1, "kitchen", [Person("A", "x", 2.0, 2.0), Person("B", "x", 2.3, 2.0)], solo=False)
    g.next_at = clock.t
    sim.groups = [g]
    sim.update_droid(2.0, 2.5)
    sim.step()
    snap = sim.snapshot()
    assert snap["bubbles"] and snap["bubbles"][0]["heard"] is True
    assert snap["droid"]["room"] == "kitchen"

"""Sim-world geometry from nimbus's /api/sim/world: free-space tests, rooms, doors, routes.

World coordinates match nimbus: meters, origin at the map centre, x right, y up. `rows[gy][gx]` is "#" for
a wall/obstacle cell and gy counts upward.
"""

import math
import random
from collections import deque
from dataclasses import dataclass

QUADRANT_NAMES = {(-1, -1): "living room", (1, -1): "bedroom", (-1, 1): "office", (1, 1): "kitchen"}


class Grid:
    def __init__(self, world: dict):
        self.rows: list[str] = world["rows"]
        self.res = float(world["resolution"])
        self.ox = float(world["origin_x"])
        self.oy = float(world["origin_y"])
        self.w = int(world["width"])
        self.h = int(world["height"])

    def cell(self, x: float, y: float) -> tuple[int, int]:
        return int(math.floor((x + self.ox) / self.res)), int(math.floor((y + self.oy) / self.res))

    def center(self, gx: int, gy: int) -> tuple[float, float]:
        return gx * self.res - self.ox + self.res / 2, gy * self.res - self.oy + self.res / 2

    def blocked(self, x: float, y: float) -> bool:
        gx, gy = self.cell(x, y)
        if not (0 <= gx < self.w and 0 <= gy < self.h):
            return True
        row = self.rows[gy]
        return gx >= len(row) or row[gx] == "#"

    def clear(self, x: float, y: float, r: float) -> bool:
        """No wall or obstacle within r metres of (x, y)."""
        if self.blocked(x, y):
            return False
        for k in range(12):
            a = k * math.pi / 6
            for rr in (r, r / 2):
                if self.blocked(x + rr * math.cos(a), y + rr * math.sin(a)):
                    return False
        return True

    def los(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        """True if no wall cell lies on the straight line a -> b."""
        d = math.dist(a, b)
        n = max(1, int(d / (self.res / 2)))
        for i in range(1, n):
            t = i / n
            if self.blocked(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t):
                return False
        return True


@dataclass
class Room:
    name: str
    cx: float  # a clear point near the middle of the room (used as the patrol target)
    cy: float
    sx: int = 0  # quadrant signs (0 for a single-room map)
    sy: int = 0


@dataclass
class Door:
    a: str  # room names it joins
    b: str
    x: float
    y: float
    nx: float  # unit normal pointing from a toward b
    ny: float


class Layout:
    """Rooms, doors and room-to-room routes for the world."""

    def __init__(self, grid: Grid):
        self.grid = grid
        self.rooms: list[Room] = []
        self.doors: list[Door] = []
        self._detect()

    # ---- detection ----
    def _nearest_clear(self, x: float, y: float, r: float = 0.4) -> tuple[float, float]:
        for k in range(0, 60):
            for a in range(0, 360, 20):
                px = x + k * 0.05 * math.cos(math.radians(a))
                py = y + k * 0.05 * math.sin(math.radians(a))
                if self.grid.clear(px, py, r):
                    return px, py
        return x, y

    def _detect(self) -> None:
        g = self.grid
        mx, my = g.w // 2, g.h // 2
        col = sum(g.rows[gy][mx] == "#" for gy in range(g.h) if mx < len(g.rows[gy])) / max(1, g.h)
        row = sum(c == "#" for c in g.rows[my]) / max(1, g.w)
        if col > 0.7 and row > 0.7:  # a cross wall: four quadrant rooms
            half_w, half_h = g.w * g.res / 4, g.h * g.res / 4
            for (sx, sy), name in QUADRANT_NAMES.items():
                cx, cy = self._nearest_clear(sx * half_w, sy * half_h)
                self.rooms.append(Room(name, cx, cy, sx, sy))
            self._find_doors(mx, my)
        else:
            cx, cy = self._nearest_clear(0.0, 0.0)
            self.rooms.append(Room("the room", cx, cy))

    def _runs(self, cells: list[bool]) -> list[tuple[int, int]]:
        runs, start = [], None
        for i, free in enumerate(cells + [False]):
            if free and start is None:
                start = i
            elif not free and start is not None:
                runs.append((start, i - 1))
                start = None
        return runs

    def _find_doors(self, mx: int, my: int) -> None:
        g = self.grid
        wx = mx * g.res - g.ox + g.res / 2
        wy = my * g.res - g.oy + g.res / 2
        for s, e in self._runs([g.rows[gy][mx] != "#" for gy in range(1, g.h - 1)]):  # gap in the vertical wall
            y = (s + e + 3) / 2 * g.res - g.oy
            a, b = self.room_at(wx - 0.6, y), self.room_at(wx + 0.6, y)
            if a and b:
                self.doors.append(Door(a.name, b.name, wx, y, 1.0, 0.0))
        for s, e in self._runs([g.rows[my][gx] != "#" for gx in range(1, g.w - 1)]):  # gap in the horizontal wall
            x = (s + e + 3) / 2 * g.res - g.ox
            a, b = self.room_at(x, wy - 0.6), self.room_at(x, wy + 0.6)
            if a and b:
                self.doors.append(Door(a.name, b.name, x, wy, 0.0, 1.0))

    # ---- queries ----
    def room_at(self, x: float, y: float) -> Room | None:
        if len(self.rooms) == 1:
            return self.rooms[0]
        for r in self.rooms:
            if (x >= 0) == (r.sx > 0) and (y >= 0) == (r.sy > 0):
                return r
        return None

    def room_named(self, name: str) -> Room | None:
        return next((r for r in self.rooms if r.name == name), None)

    def route(self, src: str, dst: str) -> list[tuple[float, float]]:
        """Waypoints from room src to room dst through doors (ends at dst's patrol point)."""
        if src == dst:
            return [(self.room_named(dst).cx, self.room_named(dst).cy)]
        prev: dict[str, tuple[str, Door, bool]] = {}
        seen, q = {src}, deque([src])
        while q:
            cur = q.popleft()
            for d in self.doors:
                for here, there, fwd in ((d.a, d.b, True), (d.b, d.a, False)):
                    if here == cur and there not in seen:
                        seen.add(there)
                        prev[there] = (cur, d, fwd)
                        q.append(there)
        if dst not in prev:
            return []
        hops, node = [], dst
        while node != src:
            p, d, fwd = prev[node]
            hops.append((d, fwd))
            node = p
        pts: list[tuple[float, float]] = []
        for d, fwd in reversed(hops):
            s = 1.0 if fwd else -1.0
            pts += [(d.x - s * d.nx * 0.6, d.y - s * d.ny * 0.6), (d.x, d.y), (d.x + s * d.nx * 0.6, d.y + s * d.ny * 0.6)]
        dest = self.room_named(dst)
        return pts + [(dest.cx, dest.cy)]

    def random_free(self, room: Room, rng: random.Random, r: float = 0.4, tries: int = 300) -> tuple[float, float] | None:
        g = self.grid
        for _ in range(tries):
            if room.sx == 0:
                x, y = rng.uniform(-g.ox, g.w * g.res - g.ox), rng.uniform(-g.oy, g.h * g.res - g.oy)
            else:
                x = rng.uniform(0.3, g.w * g.res / 2 - 0.3) * room.sx
                y = rng.uniform(0.3, g.h * g.res / 2 - 0.3) * room.sy
            if g.clear(x, y, r):
                return x, y
        return None

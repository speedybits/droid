"""Thin async client for the nimbus REST API (the /api/claude/* endpoints)."""

import httpx


REQUIRED_ENDPOINTS = [
    ("POST", "/api/claude/move"), ("POST", "/api/claude/turn"), ("POST", "/api/claude/goto"),
    ("GET", "/api/claude/scan"), ("GET", "/api/claude/map"), ("GET", "/api/claude/status"),
    ("POST", "/api/claude/stop"),
]


class NimbusClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self._http = client or httpx.AsyncClient(base_url=base_url, timeout=90.0)

    async def _call(self, method: str, path: str, **params):
        resp = await self._http.request(method, path, params=params or None)
        resp.raise_for_status()
        return resp.json()

    async def move(self, distance: float, speed: float = 0.15, timeout: float = 30.0):
        return await self._call("POST", "/api/claude/move", distance=distance, speed=speed, timeout=timeout)

    async def turn(self, degrees: float, speed: float = 0.5, timeout: float = 30.0):
        return await self._call("POST", "/api/claude/turn", degrees=degrees, speed=speed, timeout=timeout)

    async def goto(self, x: float, y: float, timeout: float = 60.0):
        return await self._call("POST", "/api/claude/goto", x=x, y=y, timeout=timeout)

    async def scan(self):
        return await self._call("GET", "/api/claude/scan")

    async def map(self):
        return await self._call("GET", "/api/claude/map")

    async def status(self):
        return await self._call("GET", "/api/claude/status")

    async def stop(self):
        return await self._call("POST", "/api/claude/stop")

    async def check(self) -> list[str]:
        """Compare nimbus's published API with what droid calls. Returns problems; [] means compatible.

        Read-only (health + openapi.json), so it never moves the robot.
        """
        try:
            (await self._http.get("/api/health")).raise_for_status()
        except Exception as e:
            return [f"nimbus unreachable at {self._http.base_url}: {e}"]
        try:
            resp = await self._http.get("/openapi.json")
            resp.raise_for_status()
            paths = resp.json().get("paths", {})
        except Exception as e:
            return [f"nimbus is up but its API schema could not be read ({e}); cannot verify endpoints"]
        problems = []
        for method, path in REQUIRED_ENDPOINTS:
            if method.lower() not in paths.get(path, {}):
                problems.append(f"nimbus is missing {method} {path} (wrong branch or version? droid expects the neato branch)")
        return problems

    async def aclose(self):
        await self._http.aclose()

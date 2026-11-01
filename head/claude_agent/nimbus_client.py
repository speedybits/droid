"""Thin async client for the nimbus REST API (the /api/claude/* endpoints)."""

import httpx


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

    async def aclose(self):
        await self._http.aclose()

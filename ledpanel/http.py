"""Tiny async HTTP GET on the standard library (runs urllib in a worker thread)."""

from __future__ import annotations

import asyncio
import json
import urllib.request


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ledpanel"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


async def get(url: str) -> bytes:
    return await asyncio.to_thread(_get, url)


async def get_json(url: str):
    return json.loads(await get(url))

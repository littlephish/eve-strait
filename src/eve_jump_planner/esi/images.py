"""Fetch and cache EVE type render images (used for station/structure photos)."""
from __future__ import annotations

import requests

from ..config import CACHE_DIR

_IMG_DIR = CACHE_DIR / "images"
_IMG_DIR.mkdir(parents=True, exist_ok=True)


def render_bytes(type_id: int, size: int = 128) -> bytes | None:
    """PNG bytes for a type's in-game render, cached on disk. None on failure."""
    if not type_id:
        return None
    path = _IMG_DIR / f"{type_id}_{size}.png"
    if path.exists():
        return path.read_bytes()
    url = f"https://images.evetech.net/types/{type_id}/render?size={size}"
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return resp.content
    except (requests.RequestException, OSError):
        return None

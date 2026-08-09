"""Cliente de la API pública de Kick para listar clips de un canal.

Kick expone /api/v2/channels/{slug}/clips sin autenticación. Devuelve páginas de 20
clips y un `nextCursor` que es un JSON serializado ({"view": N, "id": "clip_..."}),
no un offset numérico: hay que reenviarlo tal cual en el parámetro `cursor`.
"""

from __future__ import annotations

import time
from typing import Iterator
from urllib.parse import urlencode

import requests

BASE = "https://kick.com/api/v2"

# Kick responde 403 a clientes sin cabeceras de navegador.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}


class KickError(RuntimeError):
    pass


def _get(url: str, *, retries: int = 3, timeout: int = 30) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 429:
                # Kick no documenta rate limits; backoff exponencial y reintento.
                time.sleep(2 ** attempt * 2)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - reintentamos cualquier fallo de red
            last = exc
            time.sleep(2 ** attempt)
    raise KickError(f"GET {url} falló tras {retries} intentos: {last}")


def get_channel(slug: str) -> dict:
    return _get(f"{BASE}/channels/{slug}")


def iter_clips(
    slug: str,
    *,
    sort: str = "view",
    time_range: str = "all",
    max_pages: int = 50,
) -> Iterator[dict]:
    """Itera los clips de un canal paginando con el cursor opaco de Kick.

    sort: "view" | "date"
    time_range: "day" | "week" | "month" | "all"
    """
    cursor: str | None = None
    seen: set[str] = set()

    for _ in range(max_pages):
        params = {"sort": sort, "time": time_range}
        if cursor:
            params["cursor"] = cursor
        payload = _get(f"{BASE}/channels/{slug}/clips?{urlencode(params)}")

        clips = payload.get("clips") or []
        if not clips:
            return

        # El cursor se agota devolviendo la misma página; cortamos por ids repetidos.
        fresh = [c for c in clips if c["id"] not in seen]
        if not fresh:
            return
        for clip in fresh:
            seen.add(clip["id"])
            yield clip

        cursor = payload.get("nextCursor")
        if not cursor:
            return
        time.sleep(0.4)  # cortesía con la API, no está documentado el límite


def normalize(clip: dict) -> dict:
    """Campos que el pipeline usa igual en las dos plataformas."""
    return {
        **clip,
        "vod_offset": clip.get("vod_starts_at") or None,
        "livestream_id": str(clip.get("livestream_id") or "") or None,
        "clip_page_url": f"https://kick.com/clips/{clip['id']}",
    }

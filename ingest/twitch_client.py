"""Cliente de la API oficial de Twitch (Helix) para clips.

A diferencia de Kick, aquí los clips SÍ tienen vistas reales y la API devuelve el top
ordenado por `view_count` dentro de una ventana de tiempo. Es la fuente con señal buena.

Usa el flujo client_credentials (App Access Token): sólo leemos datos públicos, no
necesitamos que ningún usuario inicie sesión.

Expone la misma interfaz que kick_client para que pipeline.py trate ambas igual.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.twitch.tv/helix"

# Ventanas equivalentes a las de Kick, en días. None = sin límite.
TIME_RANGES = {"day": 1, "week": 7, "month": 30, "all": None}

_token: dict = {"value": None, "expires_at": 0.0}
_games_cache: dict[str, str] = {}


class TwitchError(RuntimeError):
    pass


class TwitchNotConfigured(TwitchError):
    pass


def _get_token() -> str:
    if _token["value"] and time.time() < _token["expires_at"] - 60:
        return _token["value"]

    creds = config.twitch_credentials()
    if not creds:
        raise TwitchNotConfigured(
            "Faltan TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET en el .env. "
            "Sácalas en https://dev.twitch.tv/console/apps"
        )
    client_id, client_secret = creds

    resp = requests.post(TOKEN_URL, params={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }, timeout=30)
    if resp.status_code != 200:
        raise TwitchError(f"No se pudo obtener el token ({resp.status_code}): {resp.text[:200]}")

    data = resp.json()
    _token["value"] = data["access_token"]
    _token["expires_at"] = time.time() + data.get("expires_in", 3600)
    return _token["value"]


def _headers() -> dict:
    creds = config.twitch_credentials()
    if not creds:
        raise TwitchNotConfigured("Faltan credenciales de Twitch en el .env")
    return {"Client-Id": creds[0], "Authorization": f"Bearer {_get_token()}"}


def _api(path: str, params: dict, *, retries: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(f"{API}/{path}", headers=_headers(), params=params, timeout=30)
            if resp.status_code == 401:
                _token["value"] = None  # token vencido: forzamos refresco
                continue
            if resp.status_code == 429:
                time.sleep(2 ** attempt * 2)
                continue
            resp.raise_for_status()
            return resp.json()
        except TwitchNotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 ** attempt)
    raise TwitchError(f"GET {path} falló tras {retries} intentos: {last}")


def get_channel(slug: str) -> dict:
    """Datos del canal. Lanza TwitchError si el usuario no existe."""
    data = _api("users", {"login": slug.lower()})
    items = data.get("data") or []
    if not items:
        raise TwitchError(f"El canal '{slug}' no existe en Twitch")
    u = items[0]
    return {
        "id": u["id"],
        "user": {"username": u.get("display_name") or u.get("login")},
        "profile_picture": u.get("profile_image_url"),
        "description": u.get("description"),
    }


def _game_names(game_ids: list[str]) -> dict[str, str]:
    """Resuelve nombres de juegos/categorías en lotes de 100, con caché."""
    missing = [g for g in {g for g in game_ids if g} if g not in _games_cache]
    for i in range(0, len(missing), 100):
        chunk = missing[i:i + 100]
        try:
            data = _api("games", [("id", g) for g in chunk])
        except TwitchError:
            continue
        for g in data.get("data") or []:
            _games_cache[g["id"]] = g.get("name") or ""
    return _games_cache


def _normalize(clip: dict, games: dict[str, str]) -> dict:
    """Convierte un clip de Twitch al mismo shape que usa el pipeline para Kick.

    Twitch no expone URL de video descargable en la API. El viejo truco de derivar el
    MP4 del thumbnail (`-preview-480x272.jpg` -> `.mp4`) murió: el CDN nuevo sirve
    `.../<uuid>/landscape/thumb/thumb-...jpg` y no hay MP4 hermano (probado, 404).

    Así que:
      - preview en el panel -> `embed_url`, el reproductor oficial en iframe
      - descarga para render -> yt-dlp sobre `clip_page_url`
    """
    return {
        "id": clip["id"],
        "title": clip.get("title"),
        "views": clip.get("view_count") or 0,
        "view_count": clip.get("view_count") or 0,
        "likes": 0,                                   # Twitch no expone likes en clips
        "duration": int(round(clip.get("duration") or 0)),
        "created_at": clip.get("created_at"),
        "video_url": None,
        "embed_url": clip.get("embed_url"),
        "clip_page_url": clip.get("url"),
        "thumbnail_url": clip.get("thumbnail_url"),
        "category": {"name": games.get(clip.get("game_id") or "", "")},
        "creator": {"username": clip.get("creator_name")},
        "is_mature": False,
        "privacy": "public",
        # Posición dentro del VOD: permite detectar cuándo varias personas clipearon
        # el mismo instante, que es la señal de que ese momento estuvo bueno.
        "vod_offset": clip.get("vod_offset"),
        "livestream_id": clip.get("video_id") or None,
    }


def iter_clips(
    slug: str,
    *,
    sort: str = "view",          # Twitch siempre ordena por vistas; se acepta por simetría
    time_range: str = "all",
    max_pages: int = 50,
) -> Iterator[dict]:
    """Itera clips del canal, ya ordenados por vistas descendente."""
    channel = get_channel(slug)
    broadcaster_id = channel["id"]

    params: dict = {"broadcaster_id": broadcaster_id, "first": 100}
    days = TIME_RANGES.get(time_range)
    if days:
        now = datetime.now(timezone.utc)
        params["started_at"] = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["ended_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    cursor: str | None = None
    for _ in range(max_pages):
        page = dict(params)
        if cursor:
            page["after"] = cursor

        data = _api("clips", page)
        clips = data.get("data") or []
        if not clips:
            return

        games = _game_names([c.get("game_id") for c in clips])
        for clip in clips:
            yield _normalize(clip, games)

        cursor = (data.get("pagination") or {}).get("cursor")
        if not cursor:
            return
        time.sleep(0.2)


def get_live_streams(logins: list[str]) -> dict[str, dict]:
    """Quién está en directo ahora mismo, de una lista de canales.

    Helix admite hasta 100 `user_login` por llamada y cuesta 1 unidad de cuota, así que
    sondear cada par de minutos es prácticamente gratis.
    Devuelve {login: {stream_id, title, category, viewers, started_at}}.
    """
    live: dict[str, dict] = {}
    for i in range(0, len(logins), 100):
        chunk = [l.lower() for l in logins[i:i + 100]]
        try:
            data = _api("streams", [("user_login", l) for l in chunk] + [("first", "100")])
        except TwitchError:
            continue
        for s in data.get("data") or []:
            live[s["user_login"].lower()] = {
                "stream_id": s["id"],
                "title": s.get("title"),
                "category": s.get("game_name"),
                "viewers": s.get("viewer_count") or 0,
                "started_at": s.get("started_at"),
            }
    return live


# ── DESCARGA DIRECTA DE CLIPS ──────────────────────────────────────────────────
# yt-dlp rompe su extractor de Twitch cada vez que Twitch toca su GraphQL interno
# (hoy mismo falla con KeyError('data'), y ni la versión nightly lo arregla). Como el
# endpoint sí responde bien, pedimos la URL nosotros: dos llamadas y sin depender de
# que publiquen un parche.
GQL_URL = "https://gql.twitch.tv/gql"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"   # client-id público del reproductor web
CLIP_TOKEN_HASH = "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4c35796eb11"


def clip_slug(url_or_slug: str) -> str:
    """Extrae el identificador del clip de una URL de Twitch."""
    s = (url_or_slug or "").strip().rstrip("/")
    for marca in ("/clip/", "clips.twitch.tv/"):
        if marca in s:
            s = s.split(marca)[-1]
    return s.split("?")[0].split("/")[-1]


def clip_download_url(url_or_slug: str) -> tuple[str, dict]:
    """URL directa al MP4 del clip, en la mejor calidad disponible.

    Devuelve (url, info) donde info trae quality y frameRate.
    """
    slug = clip_slug(url_or_slug)
    payload = [{
        "operationName": "VideoAccessToken_Clip",
        "variables": {"slug": slug},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": CLIP_TOKEN_HASH}},
    }]
    resp = requests.post(GQL_URL, json=payload,
                         headers={"Client-Id": GQL_CLIENT_ID}, timeout=30)
    if resp.status_code != 200:
        raise TwitchError(f"GQL respondió {resp.status_code} para el clip {slug}")

    try:
        clip = resp.json()[0]["data"]["clip"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise TwitchError(f"Respuesta inesperada de GQL para {slug}: {exc}")
    if not clip:
        raise TwitchError(f"El clip {slug} no existe o fue borrado")

    calidades = clip.get("videoQualities") or []
    if not calidades:
        raise TwitchError(f"El clip {slug} no expone calidades descargables")

    def alto(q: dict) -> int:
        try:
            return int(q.get("quality") or 0)
        except ValueError:
            return 0

    mejor = max(calidades, key=alto)
    token = clip.get("playbackAccessToken") or {}
    if not token.get("signature") or not token.get("value"):
        raise TwitchError(f"El clip {slug} no devolvió token de reproducción")

    url = (f"{mejor['sourceURL']}?sig={token['signature']}"
           f"&token={quote(token['value'])}")
    return url, {"quality": mejor.get("quality"), "fps": mejor.get("frameRate")}


def download_clip_file(url_or_slug: str, dest: Path) -> Path:
    """Descarga el MP4 del clip a `dest`."""
    url, _ = clip_download_url(url_or_slug)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    with requests.get(url, stream=True, timeout=120) as r:
        if r.status_code != 200:
            raise TwitchError(f"La descarga del clip devolvió {r.status_code}")
        with tmp.open("wb") as fh:
            for trozo in r.iter_content(1 << 20):
                if trozo:
                    fh.write(trozo)

    tmp.replace(dest)
    return dest


def is_configured() -> bool:
    return config.twitch_credentials() is not None

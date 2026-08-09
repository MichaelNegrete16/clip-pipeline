"""Lógica compartida de ingesta y estadísticas. La usan tanto la CLI como el panel web."""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as db_mod
import kick_client
import twitch_client
from score import views_per_day

# Ambos clientes exponen get_channel(slug) e iter_clips(slug, sort, time_range, max_pages).
CLIENTS = {"kick": kick_client, "twitch": twitch_client}

THRESHOLDS = (1000, 300, 100, 50)

# Acepta URL completa, dominio+slug o slug pelado.
_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(kick\.com|twitch\.tv)/(?:videos/)?([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")

PLATFORM_BY_HOST = {"kick.com": "kick", "twitch.tv": "twitch"}


def parse_sources(text: str) -> list[tuple[str, str]]:
    """Extrae (plataforma, slug) de texto pegado: URLs, slugs, separados como sea.

    >>> parse_sources("https://kick.com/lacobraaa/clips, westcol")
    [('kick', 'lacobraaa'), ('kick', 'westcol')]
    """
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for raw in re.split(r"[\s,;]+", text.strip()):
        if not raw:
            continue
        m = _URL_RE.search(raw)
        if m:
            platform = PLATFORM_BY_HOST[m.group(1).lower()]
            slug = m.group(2).lower()
            # kick.com/lacobraaa/clips -> el sufijo no es el slug
            if slug in {"clips", "videos", "about", "chat"}:
                continue
        elif _SLUG_RE.match(raw):
            platform, slug = "kick", raw.lower()
        else:
            continue

        if (platform, slug) not in seen:
            seen.add((platform, slug))
            found.append((platform, slug))
    return found


def ingest_source(conn: sqlite3.Connection, platform: str, slug: str,
                  pages_all: int = 25, pages_month: int = 8,
                  windows: tuple[str, ...] | None = None) -> dict:
    """Descarga y guarda los clips de un streamer. Devuelve el resultado resumido.

    Por defecto consulta tres ventanas: `all` fija el techo histórico del canal (sirve
    para evaluarlo), y `month` + `week` traen lo FRESCO, que es lo que se publica.
    El cron diario debería llamar con windows=("day","week"): más barato y sólo
    material reciente.
    """
    client = CLIENTS.get(platform)
    if client is None:
        return {"ok": False, "slug": slug, "platform": platform,
                "error": f"plataforma '{platform}' no soportada"}

    try:
        info = client.get_channel(slug)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "slug": slug, "platform": platform,
                "error": f"no se pudo resolver el canal: {exc}"}

    user = info.get("user") or {}
    display = user.get("username") or slug

    pages_by_window = {"all": pages_all, "month": pages_month,
                       "week": pages_month, "day": pages_month}
    wins = windows or ("all", "month", "week")

    clips: dict[str, dict] = {}
    try:
        for rng in wins:
            pages = pages_by_window.get(rng, pages_month)
            for c in client.iter_clips(slug, sort="view", time_range=rng, max_pages=pages):
                if platform == "kick":
                    c = kick_client.normalize(c)
                clips[c["id"]] = c
    except Exception as exc:  # noqa: BLE001
        if not clips:
            return {"ok": False, "slug": slug, "platform": platform, "error": str(exc)}

    source_id = db_mod.upsert_source(conn, platform, slug, display)

    for c in clips.values():
        views = c.get("views") or c.get("view_count") or 0
        conn.execute(
            """
            INSERT INTO clip_candidates (
                source_id, platform_clip_id, title, views, likes, duration_s, created_at,
                video_url, embed_url, clip_page_url, thumbnail_url, category,
                creator_username, is_mature, views_per_day, vod_offset, livestream_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (source_id, platform_clip_id) DO UPDATE SET
                views         = excluded.views,
                likes         = excluded.likes,
                views_per_day = excluded.views_per_day,
                -- COALESCE: si una corrida vieja los dejó vacíos, se rellenan;
                -- si vienen nulos ahora, no se pisa lo que ya había.
                vod_offset    = COALESCE(excluded.vod_offset, clip_candidates.vod_offset),
                livestream_id = COALESCE(excluded.livestream_id, clip_candidates.livestream_id),
                clip_page_url = COALESCE(excluded.clip_page_url, clip_candidates.clip_page_url),
                last_seen_at  = datetime('now')
            """,
            (
                source_id, c["id"], c.get("title"), views, c.get("likes") or 0,
                c.get("duration") or 0, c.get("created_at"), c.get("video_url"),
                c.get("embed_url"), c.get("clip_page_url"),
                c.get("thumbnail_url"), (c.get("category") or {}).get("name"),
                (c.get("creator") or {}).get("username"), int(bool(c.get("is_mature"))),
                round(views_per_day(views, c.get("created_at")), 2),
                c.get("vod_offset"), c.get("livestream_id"),
            ),
        )
        row = conn.execute(
            "SELECT id FROM clip_candidates WHERE source_id=? AND platform_clip_id=?",
            (source_id, c["id"]),
        ).fetchone()
        conn.execute("INSERT INTO view_snapshots (clip_id, views) VALUES (?,?)",
                     (row["id"], views))

    conn.commit()
    return {"ok": True, "slug": slug, "platform": platform, "display_name": display,
            "source_id": source_id, "clips": len(clips)}


def source_stats(conn: sqlite3.Connection, source_id: int,
                 min_d: int = 8, max_d: int = 90) -> dict:
    """Estadísticas de decisión: ¿este streamer aporta clips buenos de forma sostenida?"""
    rows = conn.execute(
        "SELECT views, duration_s, created_at, title FROM clip_candidates WHERE source_id=?",
        (source_id,),
    ).fetchall()

    src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if not rows:
        return {"id": source_id, "slug": src["slug"], "platform": src["platform"],
                "display_name": src["display_name"], "total_clips": 0, "peak": 0,
                "month_top": 0, "per_month": {t: 0 for t in THRESHOLDS},
                "enabled": bool(src["enabled"]), "has_consent": bool(src["has_consent"])}

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    usable = [r for r in rows if min_d <= r["duration_s"] <= max_d]

    recent = []
    for r in usable:
        try:
            dt = datetime.fromisoformat((r["created_at"] or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= cutoff:
            recent.append(r)

    return {
        "id": source_id,
        "slug": src["slug"],
        "platform": src["platform"],
        "display_name": src["display_name"],
        "enabled": bool(src["enabled"]),
        "has_consent": bool(src["has_consent"]),
        "total_clips": len(rows),
        "usable_clips": len(usable),
        "peak": max((r["views"] for r in rows), default=0),
        "month_top": max((r["views"] for r in recent), default=0),
        "per_month": {t: len([r for r in recent if r["views"] >= t]) for t in THRESHOLDS},
    }


def all_source_stats(conn: sqlite3.Connection) -> list[dict]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM sources ORDER BY added_at").fetchall()]
    return [source_stats(conn, i) for i in ids]

"""Scoring y filtros de elegibilidad para clips.

Las vistas están fuertemente sesgadas (el top de un canal puede tener 100x la mediana),
así que normalizamos en escala logarítmica. Si no, el clip #1 histórico gana siempre y
nunca sube nada nuevo.

Se combinan dos señales:
  - popularidad absoluta (views)
  - velocidad (views por día desde su publicación)

La velocidad evita que un clip viejo con muchas vistas acumuladas tape a uno reciente
que está explotando ahora.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

# Duración objetivo para Shorts. YouTube admite hasta 180s, pero por debajo de 60s
# la retención completa es mucho más probable.
MIN_DURATION_S = 8
MAX_DURATION_S = 90

MIN_VIEWS = 1000

W_VIEWS = 0.6
W_VELOCITY = 0.4


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_since(created_at: str | None) -> float:
    dt = _parse_dt(created_at)
    if dt is None:
        return 1.0
    delta = datetime.now(timezone.utc) - dt
    return max(delta.total_seconds() / 86400.0, 1.0)


def views_per_day(views: int, created_at: str | None) -> float:
    return views / days_since(created_at)


def check_eligibility(clip: dict) -> tuple[bool, str | None]:
    """Filtros duros. Devuelve (elegible, motivo_de_rechazo)."""
    duration = clip.get("duration") or 0
    views = clip.get("views") or clip.get("view_count") or 0

    if duration < MIN_DURATION_S:
        return False, f"muy corto ({duration}s)"
    if duration > MAX_DURATION_S:
        return False, f"muy largo ({duration}s)"
    if views < MIN_VIEWS:
        return False, f"pocas vistas ({views})"
    if clip.get("is_mature"):
        return False, "marcado como contenido adulto"
    if (clip.get("privacy") or "public") != "public":
        return False, "clip no público"
    return True, None


def compute_scores(clips: list[dict]) -> list[dict]:
    """Añade views_per_day, eligible, reject_reason y score (0-100) a cada clip."""
    if not clips:
        return []

    enriched = []
    for clip in clips:
        views = clip.get("views") or clip.get("view_count") or 0
        vpd = views_per_day(views, clip.get("created_at"))
        eligible, reason = check_eligibility(clip)
        enriched.append({**clip, "_views": views, "_vpd": vpd, "_eligible": eligible, "_reason": reason})

    # Normalizamos sólo contra los elegibles: incluir la cola de clips basura
    # comprimiría el rango útil y todos los buenos quedarían con score parecido.
    pool = [c for c in enriched if c["_eligible"]] or enriched

    def norm(values: list[float], value: float) -> float:
        logs = [math.log1p(max(v, 0)) for v in values]
        lo, hi = min(logs), max(logs)
        if hi - lo < 1e-9:
            return 1.0
        return (math.log1p(max(value, 0)) - lo) / (hi - lo)

    all_views = [c["_views"] for c in pool]
    all_vpd = [c["_vpd"] for c in pool]

    for clip in enriched:
        if clip["_eligible"]:
            s = W_VIEWS * norm(all_views, clip["_views"]) + W_VELOCITY * norm(all_vpd, clip["_vpd"])
            clip["_score"] = round(s * 100, 2)
        else:
            clip["_score"] = 0.0

    return enriched

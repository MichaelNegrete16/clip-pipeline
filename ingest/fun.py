"""Detección de clips divertidos, no sólo vistos.

Las vistas miden que algo se vio, no que fuera bueno. Aquí se combinan señales que sí
apuntan a "esto valió la pena":

1. DENSIDAD DE CLIPEO (la más fuerte). Si varias personas distintas clipearon el mismo
   instante del directo, ahí pasó algo. Nadie clipea un momento aburrido. Se agrupan
   los clips por (directo, ventana de tiempo) usando vod_offset.

2. PISTAS EN EL TÍTULO. Quien clipea escribe en caliente: "jajaja", "se muere de risa",
   "no puede parar". Señal débil y ruidosa, pero gratis.

3. VELOCIDAD DE VISTAS, que ya se calcula en score.py.

Lo que NO cubre esto: la gracia real está en el audio y el chat. La versión buena de
este detector lee el chat en vivo y mide picos de risa; esto es la aproximación que se
puede hacer con los datos que ya tenemos guardados.
"""

from __future__ import annotations

import math
import re
import sqlite3

# Ventana para considerar que dos clips capturan "el mismo momento".
CLUSTER_WINDOW_S = 90

# Palabras que quien clipea escribe cuando algo le hizo gracia.
FUN_HINTS = [
    "jaja", "jeje", "jiji", "lmao", "lol", "risa", "riendo", "muere de risa",
    "no puede", "se caga", "llorando", "epico", "épico", "brutal", "crack",
    "wtf", "que hace", "qué hace", "increible", "increíble", "fail", "insano",
    "momento", "reaccion", "reacción", "asustado", "susto", "grito", "cara de",
]
# Pistas de contenido informativo/noticioso, para el modo "informativo".
INFO_HINTS = [
    "explica", "explicando", "opina", "opinión", "analiza", "análisis", "noticia",
    "confirma", "anuncia", "revela", "cuenta", "dice que", "responde",
]

_HINT_RE = re.compile("|".join(re.escape(h) for h in FUN_HINTS), re.IGNORECASE)
_INFO_RE = re.compile("|".join(re.escape(h) for h in INFO_HINTS), re.IGNORECASE)


def title_hints(title: str | None) -> tuple[int, int]:
    """(pistas de gracia, pistas de info) encontradas en el título."""
    if not title:
        return 0, 0
    return len(_HINT_RE.findall(title)), len(_INFO_RE.findall(title))


def compute_clusters(conn: sqlite3.Connection, source_id: int | None = None) -> int:
    """Cuenta cuántos clips capturan el mismo momento y lo guarda en hype_clips.

    Devuelve cuántos clips quedaron marcados como momento caliente (>1 clip).
    """
    where = "WHERE livestream_id IS NOT NULL AND vod_offset IS NOT NULL"
    params: list = []
    if source_id:
        where += " AND source_id = ?"
        params.append(source_id)

    rows = conn.execute(
        f"SELECT id, source_id, livestream_id, vod_offset FROM clip_candidates {where} "
        f"ORDER BY source_id, livestream_id, vod_offset", params).fetchall()

    # Agrupamos por directo y recorremos en orden: los clips cercanos en el tiempo
    # forman un mismo racimo.
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["source_id"], r["livestream_id"]), []).append(r)

    hot = 0
    for items in groups.values():
        cluster: list = []
        for r in items:
            if cluster and (r["vod_offset"] - cluster[-1]["vod_offset"]) > CLUSTER_WINDOW_S:
                hot += _flush(conn, cluster)
                cluster = []
            cluster.append(r)
        hot += _flush(conn, cluster)

    conn.commit()
    return hot


def _flush(conn: sqlite3.Connection, cluster: list) -> int:
    if not cluster:
        return 0
    n = len(cluster)
    # La clave usa el inicio del racimo, no el offset de cada clip: así todos los del
    # mismo momento comparten identificador aunque estén desplazados entre sí.
    head = cluster[0]
    key = f"{head['source_id']}:{head['livestream_id']}:{head['vod_offset']}"
    conn.executemany(
        "UPDATE clip_candidates SET hype_clips = ?, cluster_key = ? WHERE id = ?",
        [(n, key, r["id"]) for r in cluster])
    return n if n > 1 else 0


def compute_fun_scores(conn: sqlite3.Connection, source_id: int | None = None) -> dict:
    """Calcula fun_score (0-100) combinando densidad, pistas de título y velocidad."""
    where = "WHERE 1=1"
    params: list = []
    if source_id:
        where += " AND source_id = ?"
        params.append(source_id)

    rows = conn.execute(
        f"SELECT id, title, views, views_per_day, hype_clips FROM clip_candidates {where}",
        params).fetchall()
    if not rows:
        return {"scored": 0, "hot": 0, "chat_confirmed": 0}

    max_vpd = max((r["views_per_day"] or 0) for r in rows) or 1

    # Clips que el chat confirmó: hubo pico de risa justo cuando se creó el clip.
    confirmados = {r["clip_id"]: r["ratio"] for r in conn.execute(
        "SELECT clip_id, MAX(ratio) ratio FROM chat_peaks "
        "WHERE clip_id IS NOT NULL GROUP BY clip_id")}

    updates, hot = [], 0
    for r in rows:
        hype = r["hype_clips"] or 1
        fun, info = title_hints(r["title"])
        ratio = confirmados.get(r["id"], 0)

        # Densidad: log para que 6 clips no valga 6x lo que 3.
        s_hype = min(math.log1p(hype - 1) / math.log(6), 1.0)
        s_hint = min(fun * 0.35, 1.0)
        s_vel = min(math.log1p(r["views_per_day"] or 0) / math.log1p(max_vpd), 1.0)
        # Confirmación del chat: la señal más directa que existe de que hubo gracia.
        s_chat = min(math.log1p(ratio) / math.log(12), 1.0) if ratio else 0.0

        if s_chat:
            score = 100 * (0.40 * s_chat + 0.30 * s_hype + 0.10 * s_hint + 0.20 * s_vel)
        else:
            score = 100 * (0.50 * s_hype + 0.20 * s_hint + 0.30 * s_vel)

        updates.append((round(score, 2), r["id"]))
        if hype > 1:
            hot += 1

    conn.executemany("UPDATE clip_candidates SET fun_score = ? WHERE id = ?", updates)
    conn.commit()
    return {"scored": len(updates), "hot": hot, "chat_confirmed": len(confirmados)}


# Cuánto después de un pico de chat se suele crear el clip. La gente reacciona, se ríe,
# y recién entonces le da a clipear: casi nunca es instantáneo.
PEAK_LOOKAHEAD_S = 300
PEAK_LOOKBEHIND_S = 45


def correlate_peaks(conn: sqlite3.Connection) -> int:
    """Enlaza cada pico de chat con el clip que se creó en ese momento.

    Es la señal más fuerte de todas: no es que alguien clipeara algo, es que el chat
    entero se rió en ese instante Y alguien lo clipeó.
    """
    peaks = conn.execute(
        "SELECT id, source_id, at_utc FROM chat_peaks WHERE clip_id IS NULL").fetchall()

    enlazados = 0
    for p in peaks:
        row = conn.execute(
            """SELECT id FROM clip_candidates
               WHERE source_id = ?
                 AND created_at >= datetime(?, ?)
                 AND created_at <= datetime(?, ?)
               ORDER BY ABS(strftime('%s', created_at) - strftime('%s', ?)) ASC
               LIMIT 1""",
            (p["source_id"], p["at_utc"], f"-{PEAK_LOOKBEHIND_S} seconds",
             p["at_utc"], f"+{PEAK_LOOKAHEAD_S} seconds", p["at_utc"])).fetchone()
        if row:
            conn.execute("UPDATE chat_peaks SET clip_id = ? WHERE id = ?",
                         (row["id"], p["id"]))
            enlazados += 1

    conn.commit()
    return enlazados


def refresh(conn: sqlite3.Connection, source_id: int | None = None) -> dict:
    linked = correlate_peaks(conn)
    clusters = compute_clusters(conn, source_id)
    res = compute_fun_scores(conn, source_id)
    return {"clustered": clusters, "peaks_linked": linked, **res}

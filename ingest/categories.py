"""Agrupación de categorías en familias manejables.

Twitch usa una categoría POR JUEGO, así que hay una cola larguísima de categorías con
cinco clips cada una ("Fears to Fathom", "Burglin' Gnomes", "Shift at Midnight"...).
Filtrar por ahí no sirve para nada.

La regla es al revés de lo que parece: se enumeran las categorías que NO son juegos —
que son pocas y estables — y todo lo demás cae en Gaming. Mantener una lista de juegos
sería imposible, salen decenas cada semana.

Dos grupos existen por razones de negocio, no de contenido:
  - CASINO: YouTube restringe fuerte el contenido de apuestas y limita anunciantes.
  - SUBIDO DE TONO: el equivalente de "Pools, Hot Tubs & Bikinis", que desmonetiza.
Conviene poder excluirlos de un canal con un clic.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

CHARLA = "Charla"
GAMING = "Gaming"
DEPORTES = "Deportes"
MUSICA = "Música"
EVENTOS = "Eventos"
CREATIVO = "Creativo"
CASINO = "Casino"
SUBIDO = "Subido de tono"
OTROS = "Otros"

GROUP_ORDER = [CHARLA, GAMING, DEPORTES, EVENTOS, MUSICA, CREATIVO, CASINO, SUBIDO, OTROS]

# Coincidencia exacta sobre el nombre normalizado.
EXACT = {
    "just chatting": CHARLA,
    "irl": CHARLA,
    "talk shows & podcasts": CHARLA,
    "talk shows and podcasts": CHARLA,
    "podcasts": CHARLA,
    "politics": CHARLA,
    "travel & outdoors": CHARLA,
    "food & drink": CHARLA,
    "beauty & body art": CHARLA,
    "asmr": CHARLA,
    "sleeping": CHARLA,

    "sports": DEPORTES,
    "football": DEPORTES,
    "soccer": DEPORTES,
    "fifa": DEPORTES,
    "basketball": DEPORTES,
    "boxing": DEPORTES,
    "mma": DEPORTES,
    "fitness & health": DEPORTES,

    "music": MUSICA,
    "dj": MUSICA,
    "music & performing arts": MUSICA,
    "karaoke": MUSICA,

    "special events": EVENTOS,
    "the game awards": EVENTOS,
    "esports": EVENTOS,

    "art": CREATIVO,
    "makers & crafting": CREATIVO,
    "software and game development": CREATIVO,
    "science & technology": CREATIVO,
    "animals, aquariums, and zoos": CREATIVO,
    "games + demos": CREATIVO,

    "slots": CASINO,
    "virtual casino": CASINO,
    "poker": CASINO,
    "sports betting": CASINO,

    "pools, hot tubs & bikinis": SUBIDO,
    "pools, hot tubs and bikinis": SUBIDO,
}

# Patrones sobre el nombre normalizado, para lo que no cae por nombre exacto.
# El orden importa: gana el primero que coincida.
PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bvelada\b|\bveladadelano\b"), EVENTOS),
    (re.compile(r"\bcasino\b|\bslot|\bapuesta|\bbetting\b|\bruleta\b"), CASINO),
    (re.compile(r"\bhot tub|\bbikini"), SUBIDO),
    # Fútbol, sea competición real o videojuego. Van juntos a propósito: para un canal
    # de fútbol es más útil tenerlos en la misma familia que un EA FC perdido entre
    # Minecraft y Fortnite.
    (re.compile(r"\bea sports fc\b|\bfc \d{2}\b|\bfifa\b|\befootball\b|"
                r"\bpro evolution soccer\b|\bpes \d|\bfootball manager\b|"
                r"\brocket league\b"), DEPORTES),
    (re.compile(r"\bkings league\b|\bqueens league\b|\bmundial|\bcopa\b|"
                r"\bchampions\b|\blaliga\b|\bla liga\b|\bpremier league\b|"
                r"\bnba\b|\bnfl\b|\bufc\b|\bformula ?1\b|\bf1\b"), DEPORTES),
    (re.compile(r"\bpodcast|\bcharla|\bentrevista|\breaccion"), CHARLA),
]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text)


def group_for(category: str | None) -> str:
    """Familia a la que pertenece una categoría. Sin categoría -> Otros."""
    if not category or not category.strip():
        return OTROS

    plano = _norm(category)
    if plano in EXACT:
        return EXACT[plano]
    for patron, grupo in PATTERNS:
        if patron.search(plano):
            return grupo
    # Todo lo que no se reconoce es un juego: enumerar juegos sería imposible.
    return GAMING


def refresh_groups(conn: sqlite3.Connection) -> dict:
    """Recalcula category_group en todos los clips. Barato: se hace en memoria."""
    filas = conn.execute(
        "SELECT DISTINCT category FROM clip_candidates").fetchall()
    mapeo = {r["category"]: group_for(r["category"]) for r in filas}

    conn.executemany(
        "UPDATE clip_candidates SET category_group = ? "
        "WHERE category IS ? OR category = ?",
        [(g, c, c) for c, g in mapeo.items()])
    conn.commit()

    resumen = {r["category_group"]: r["n"] for r in conn.execute(
        "SELECT category_group, COUNT(*) n FROM clip_candidates "
        "GROUP BY category_group")}
    return {"categorias": len(mapeo), "por_grupo": resumen}


def split_slots(mix: dict[str, float], total: int) -> dict[str, int]:
    """Reparte `total` cupos entre las familias según sus pesos.

    Usa el método de restos mayores: reparte la parte entera y los cupos sobrantes van
    a quien tenga la fracción más grande. Con 2 cupos y pesos 2:1 sale {2:1, 1:1} en
    vez de dejar una familia en cero, que es lo que pasaría redondeando a secas.
    """
    activos = {k: float(v) for k, v in (mix or {}).items() if float(v or 0) > 0}
    if not activos or total <= 0:
        return {}

    suma = sum(activos.values())
    exactos = {k: total * v / suma for k, v in activos.items()}
    cupos = {k: int(v) for k, v in exactos.items()}

    faltan = total - sum(cupos.values())
    if faltan > 0:
        restos = sorted(activos, key=lambda k: (exactos[k] - cupos[k], activos[k]),
                        reverse=True)
        for k in restos[:faltan]:
            cupos[k] += 1
    return {k: v for k, v in cupos.items() if v > 0}


def stats(conn: sqlite3.Connection, *, days: int = 30, min_views: int = 300,
          source_ids: list[int] | None = None) -> list[dict]:
    """Por familia: cuánto produce y qué streamers mandan en ella.

    Esto responde la pregunta útil: si quiero un canal de Gaming, ¿a quién sigo?
    """
    where = ["c.created_at >= datetime('now', ?)"]
    params: list = [f"-{int(days)} days"]
    if source_ids:
        where.append(f"c.source_id IN ({','.join('?' * len(source_ids))})")
        params.extend(source_ids)
    filtro = " AND ".join(where)

    grupos = conn.execute(
        f"""SELECT COALESCE(c.category_group, ?) AS grupo,
                   COUNT(*) AS clips,
                   SUM(CASE WHEN c.views >= ? AND c.duration_s BETWEEN 8 AND 90
                            THEN 1 ELSE 0 END) AS buenos,
                   COUNT(DISTINCT c.source_id) AS streamers,
                   MAX(c.views) AS pico
            FROM clip_candidates c
            WHERE {filtro}
            GROUP BY grupo ORDER BY buenos DESC""",
        (OTROS, min_views, *params)).fetchall()

    salida = []
    for g in grupos:
        top = [dict(r) for r in conn.execute(
            f"""SELECT s.slug, s.platform, COUNT(*) AS buenos, MAX(c.views) AS pico
                FROM clip_candidates c JOIN sources s ON s.id = c.source_id
                WHERE COALESCE(c.category_group, ?) = ?
                  AND c.views >= ? AND c.duration_s BETWEEN 8 AND 90
                  AND {filtro}
                GROUP BY c.source_id ORDER BY buenos DESC, pico DESC LIMIT 6""",
            (OTROS, g["grupo"], min_views, *params))]

        detalle = [dict(r) for r in conn.execute(
            f"""SELECT c.category, COUNT(*) AS buenos
                FROM clip_candidates c
                WHERE COALESCE(c.category_group, ?) = ?
                  AND c.views >= ? AND c.duration_s BETWEEN 8 AND 90
                  AND c.category IS NOT NULL AND c.category <> ''
                  AND {filtro}
                GROUP BY c.category ORDER BY buenos DESC LIMIT 8""",
            (OTROS, g["grupo"], min_views, *params))]

        salida.append({
            "grupo": g["grupo"], "clips": g["clips"], "buenos": g["buenos"],
            "streamers": g["streamers"], "pico": g["pico"],
            "por_dia": round((g["buenos"] or 0) / days, 2),
            "top_streamers": top, "categorias": detalle,
        })
    return salida

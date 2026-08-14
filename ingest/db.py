"""Esquema SQLite del pipeline.

Modelo de "herramientas": las fuentes (streamers de Kick/Twitch) son un pool COMPARTIDO,
y cada canal de YouTube destino es un `profile` con su propia configuración: qué fuentes
lo alimentan, qué categorías, qué umbral de vistas, su marca de agua, su horario y su
blacklist. Un mismo clip puede alimentar varios profiles, o reservarse a uno solo.

    sources ──< profile_sources >── profiles ──< profile_queue >── clip_candidates
                                       │
                                       ├── youtube_accounts (tokens OAuth)
                                       └── blacklist_terms  (censura por bleep)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pipeline.db"

SCHEMA = """
-- ── FUENTES ────────────────────────────────────────────────────────────────────
-- Streamers de los que extraemos clips. Pool global, reutilizable entre profiles.
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,                 -- 'kick' | 'twitch'
    slug          TEXT NOT NULL,
    display_name  TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    has_consent   INTEGER NOT NULL DEFAULT 0,    -- permiso del streamer confirmado
    consent_note  TEXT,                          -- dónde consta el acuerdo
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (platform, slug)
);

-- ── PROFILES (canales de YouTube destino) ──────────────────────────────────────
-- Cada uno es una "herramienta": ClipsAfk, FutbolClips, etc. Config independiente.
CREATE TABLE IF NOT EXISTS profiles (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    slug              TEXT NOT NULL UNIQUE,      -- 'clipsafk'
    display_name      TEXT NOT NULL,             -- 'ClipsAfk'
    enabled           INTEGER NOT NULL DEFAULT 1,

    -- Selección
    min_views         INTEGER NOT NULL DEFAULT 100,
    min_duration_s    INTEGER NOT NULL DEFAULT 8,
    max_duration_s    INTEGER NOT NULL DEFAULT 90,
    categories        TEXT,                      -- JSON array; NULL = todas
    exclude_mature    INTEGER NOT NULL DEFAULT 1,

    -- Publicación
    uploads_per_day   INTEGER NOT NULL DEFAULT 2,
    publish_times     TEXT NOT NULL DEFAULT '["09:00","18:00"]',  -- JSON array HH:MM
    timezone          TEXT NOT NULL DEFAULT 'America/Bogota',
    require_approval  INTEGER NOT NULL DEFAULT 1,  -- revisión humana antes de subir

    -- Marca / render
    watermark_path    TEXT,
    title_template    TEXT DEFAULT '{title} | {source}',
    tags              TEXT,                      -- JSON array
    censor_enabled    INTEGER NOT NULL DEFAULT 1,

    -- Formato de salida. No hay API de Shorts: YouTube marca como Short lo que sea
    -- vertical (9:16) y dure <= 3 min. Así que el formato se decide al renderizar.
    output_format     TEXT NOT NULL DEFAULT 'short',   -- 'short' | 'video' | 'both'
    -- 'crop' recorta al centro y llena la pantalla (ideal facecam, pierde los bordes).
    -- 'blur' mete el video entero sobre fondo borroso (no pierde nada de imagen).
    vertical_style    TEXT NOT NULL DEFAULT 'blur',    -- 'blur' | 'crop'
    -- Modo 'both': además de los shorts diarios, arma un recopilatorio horizontal.
    compilation_clips INTEGER NOT NULL DEFAULT 10,
    compilation_day   TEXT DEFAULT 'sunday',

    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Routing M:N: qué fuentes alimentan qué profile.
CREATE TABLE IF NOT EXISTS profile_sources (
    profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    source_id   INTEGER NOT NULL REFERENCES sources(id)  ON DELETE CASCADE,
    weight      REAL NOT NULL DEFAULT 1.0,       -- prioriza fuentes dentro del profile
    PRIMARY KEY (profile_id, source_id)
);

-- ── CLIPS ──────────────────────────────────────────────────────────────────────
-- Descubiertos una sola vez por fuente; el fan-out a profiles va en profile_queue.
CREATE TABLE IF NOT EXISTS clip_candidates (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         INTEGER NOT NULL REFERENCES sources(id),
    platform_clip_id  TEXT NOT NULL,
    title             TEXT,
    views             INTEGER NOT NULL DEFAULT 0,
    likes             INTEGER NOT NULL DEFAULT 0,
    duration_s        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT,
    video_url         TEXT,                      -- Kick: HLS .m3u8 | Twitch: NULL (usa yt-dlp)
    embed_url         TEXT,                      -- Twitch: reproductor oficial en iframe
    clip_page_url     TEXT,                      -- URL pública del clip; entrada de yt-dlp
    thumbnail_url     TEXT,
    category          TEXT,
    creator_username  TEXT,
    is_mature         INTEGER NOT NULL DEFAULT 0,
    views_per_day     REAL NOT NULL DEFAULT 0,
    first_seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source_id, platform_clip_id)
);

CREATE INDEX IF NOT EXISTS idx_clips_source ON clip_candidates(source_id, views DESC);

-- Historial de vistas: permite medir velocidad real entre corridas del cron
-- en vez de asumir crecimiento lineal desde la fecha de creación.
CREATE TABLE IF NOT EXISTS view_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id      INTEGER NOT NULL REFERENCES clip_candidates(id) ON DELETE CASCADE,
    views        INTEGER NOT NULL,
    taken_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshots_clip ON view_snapshots(clip_id, taken_at);

-- ── COLA POR PROFILE ───────────────────────────────────────────────────────────
-- Un clip puede estar en la cola de varios profiles con score distinto.
CREATE TABLE IF NOT EXISTS profile_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id    INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    clip_id       INTEGER NOT NULL REFERENCES clip_candidates(id) ON DELETE CASCADE,
    score         REAL NOT NULL DEFAULT 0,
    eligible      INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT,
    status        TEXT NOT NULL DEFAULT 'new',   -- new|approved|rejected|rendered|uploaded|failed
    decided_at    TEXT,
    decided_by    TEXT,
    queued_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (profile_id, clip_id)
);
CREATE INDEX IF NOT EXISTS idx_queue_pending ON profile_queue(profile_id, status, score DESC);

-- ── CENSURA ────────────────────────────────────────────────────────────────────
-- Términos a silenciar/pitar. profile_id NULL = regla global para todos.
CREATE TABLE IF NOT EXISTS blacklist_terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id  INTEGER REFERENCES profiles(id) ON DELETE CASCADE,
    term        TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'bleep',  -- 'bleep' silencia | 'reject' descarta el clip
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_blacklist_profile ON blacklist_terms(profile_id);

-- ── RECOPILATORIOS (formato largo 16:9) ────────────────────────────────────────
-- No caben en profile_queue porque agrupan N clips en una sola pieza. Los datos de
-- subida viven aquí mismo en vez de en `uploads`, que está atada a la cola de shorts.
CREATE TABLE IF NOT EXISTS compilations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id    INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    title         TEXT,
    subtitle      TEXT,
    description   TEXT,
    tags          TEXT,
    chapters      TEXT,                      -- texto listo para la descripción
    output_path   TEXT,
    duration_s    REAL,
    clips_count   INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'draft',  -- draft|rendering|ready|uploaded|failed
    progress      TEXT,
    error         TEXT,
    yt_video_id   TEXT,
    privacy       TEXT,
    published_at  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    rendered_at   TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS compilation_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    compilation_id INTEGER NOT NULL REFERENCES compilations(id) ON DELETE CASCADE,
    clip_id        INTEGER NOT NULL REFERENCES clip_candidates(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL DEFAULT 0,
    hook           TEXT,
    UNIQUE (compilation_id, clip_id)
);
CREATE INDEX IF NOT EXISTS idx_comp_items ON compilation_items(compilation_id, position);

-- ── MONITOREO EN VIVO ──────────────────────────────────────────────────────────
-- El chat es un medidor de gracia en tiempo real: cuando algo pega, la gente escribe
-- JAJAJA de golpe. Guardamos esos picos con su marca de tiempo para después
-- correlacionarlos con los clips que la gente creó en ese mismo instante.
CREATE TABLE IF NOT EXISTS live_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    stream_id   TEXT,
    title       TEXT,
    category    TEXT,
    viewers     INTEGER,
    started_at  TEXT,
    seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at    TEXT,
    UNIQUE (source_id, stream_id)
);

CREATE TABLE IF NOT EXISTS chat_peaks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    session_id  INTEGER REFERENCES live_sessions(id) ON DELETE SET NULL,
    at_utc      TEXT NOT NULL,
    window_s    INTEGER NOT NULL DEFAULT 10,
    laugh       INTEGER NOT NULL DEFAULT 0,
    hype        INTEGER NOT NULL DEFAULT 0,
    messages    INTEGER NOT NULL DEFAULT 0,
    baseline    REAL NOT NULL DEFAULT 0,
    ratio       REAL NOT NULL DEFAULT 0,
    samples     TEXT,
    clip_id     INTEGER REFERENCES clip_candidates(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_peaks_source ON chat_peaks(source_id, at_utc DESC);

-- ── RENDER Y SUBIDA ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS renders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id      INTEGER NOT NULL REFERENCES profile_queue(id) ON DELETE CASCADE,
    output_path   TEXT,
    duration_s    REAL,
    bleeps        INTEGER NOT NULL DEFAULT 0,    -- cuántos términos se censuraron
    transcript    TEXT,                          -- JSON de Whisper, para subtítulos
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT,
    rendered_at   TEXT
);

CREATE TABLE IF NOT EXISTS youtube_accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id     INTEGER NOT NULL UNIQUE REFERENCES profiles(id) ON DELETE CASCADE,
    channel_title  TEXT,
    yt_channel_id  TEXT,
    refresh_token  TEXT,                         -- en DB, no en .env: escala a N canales
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS uploads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id       INTEGER NOT NULL REFERENCES profile_queue(id),
    yt_video_id    TEXT,
    title          TEXT,
    tags           TEXT,
    privacy        TEXT,
    published_at   TEXT,
    -- Métricas del loop de aprendizaje (YouTube Analytics API)
    views          INTEGER,
    avg_view_pct   REAL,                         -- retención media
    likes          INTEGER,
    comments       INTEGER,
    subs_gained    INTEGER,
    metrics_at     TEXT
);
"""


# Columnas añadidas después de la primera versión. CREATE TABLE IF NOT EXISTS no las
# agrega a una tabla que ya existe, así que las aplicamos a mano al abrir la conexión.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("clip_candidates", "embed_url", "TEXT"),
    ("clip_candidates", "clip_page_url", "TEXT"),
    ("profiles", "output_format", "TEXT NOT NULL DEFAULT 'short'"),
    ("profiles", "vertical_style", "TEXT NOT NULL DEFAULT 'blur'"),
    ("profiles", "cta_enabled", "INTEGER NOT NULL DEFAULT 1"),
    # Separación mínima entre PUBLICACIONES del mismo canal. Publicar varios videos
    # seguidos desde un canal chico parece automatizado y YouTube corta su
    # distribución: pasó con 3 de los 6 primeros, subidos con 21 segundos de diferencia.
    ("profiles", "min_publish_gap_min", "INTEGER NOT NULL DEFAULT 45"),
    # Mezcla de familias deseada, como pesos: {"Gaming":2,"Charla":1} = 2 de cada 3.
    # Sin esto la selección va por ranking global y una familia se come el cupo.
    ("profiles", "category_mix", "TEXT"),
    # Piso de gracia para los cupos por categoría. Sin él, una familia flaca mete
    # cualquier cosa con tal de llenar su hueco: con Deportes en 18 clips disponibles
    # entró uno de gracia 15 que sólo había clipeado una persona.
    ("profiles", "min_fun_score", "REAL NOT NULL DEFAULT 45"),
    # Idiomas de los que este canal toma clips. Los que no sean español pasan por
    # traducción y subtítulos al renderizar.
    ("profiles", "languages", "TEXT DEFAULT '[\"es\"]'"),
    # Idioma de la fuente. Twitch lo da en broadcaster_language; Kick sólo cuando el
    # canal está en vivo, así que ahí queda como respaldo lo que detecte Whisper.
    ("sources", "language", "TEXT"),
    ("sources", "language_origin", "TEXT"),   # api | detectado | manual
    # Familias que nunca se publican, pase lo que pase con su puntaje.
    ("profiles", "category_exclude", "TEXT DEFAULT '[\"Casino\",\"Subido de tono\"]'"),
    # Momento real de la llamada a la API, distinto de published_at cuando se programa.
    ("uploads", "uploaded_at", "TEXT"),
    # Título/descripción/tags propuestos por el bot, editables antes de subir.
    ("profile_queue", "title", "TEXT"),
    ("profile_queue", "description", "TEXT"),
    ("profile_queue", "tags", "TEXT"),
    ("profile_queue", "kind", "TEXT DEFAULT 'short'"),
    # Texto que se pinta SOBRE el video. Distinto del título de YouTube: aquí va la
    # frase pelada y corta, sin el canal ni #Shorts.
    ("profile_queue", "hook", "TEXT"),
    ("profile_queue", "scheduled_for", "TEXT"),
    ("profiles", "compilation_clips", "INTEGER NOT NULL DEFAULT 10"),
    ("profiles", "compilation_day", "TEXT DEFAULT 'sunday'"),
    ("uploads", "kind", "TEXT"),                 # 'short' | 'video'
    ("youtube_accounts", "token_expires_at", "TEXT"),
    ("youtube_accounts", "scopes", "TEXT"),
    ("youtube_accounts", "avatar_url", "TEXT"),
    # Posición del clip dentro del directo. Si varias personas clipean el mismo
    # instante, ese momento valió la pena: es la señal de "esto estuvo bueno".
    ("clip_candidates", "vod_offset", "INTEGER"),
    ("clip_candidates", "livestream_id", "TEXT"),
    ("clip_candidates", "hype_clips", "INTEGER NOT NULL DEFAULT 1"),
    # Identifica el racimo: todos los clips del mismo momento comparten clave.
    # Sirve para publicar UNO por momento y no repetir el mismo chiste 15 veces.
    ("clip_candidates", "cluster_key", "TEXT"),
    # Twitch usa una categoría por juego; agruparlas en familias es lo que hace
    # utilizable el filtro. `parent_category` es la agrupación nativa de Kick.
    ("clip_candidates", "category_group", "TEXT"),
    ("clip_candidates", "parent_category", "TEXT"),
    # Sin esto no hay forma de distinguir un render vivo de uno que murió con el
    # proceso: la fila se quedaba en 'rendering' y bloqueaba los reintentos.
    ("renders", "updated_at", "TEXT"),
    ("clip_candidates", "fun_score", "REAL NOT NULL DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, coltype in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.commit()


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def upsert_source(conn: sqlite3.Connection, platform: str, slug: str,
                  display_name: str | None = None) -> int:
    conn.execute(
        "INSERT INTO sources (platform, slug, display_name) VALUES (?,?,?) "
        "ON CONFLICT (platform, slug) DO UPDATE SET "
        "display_name = COALESCE(excluded.display_name, sources.display_name)",
        (platform, slug, display_name),
    )
    return conn.execute(
        "SELECT id FROM sources WHERE platform=? AND slug=?", (platform, slug)
    ).fetchone()["id"]


def upsert_profile(conn: sqlite3.Connection, slug: str, display_name: str, **cfg) -> int:
    conn.execute(
        "INSERT INTO profiles (slug, display_name) VALUES (?,?) "
        "ON CONFLICT (slug) DO UPDATE SET display_name = excluded.display_name",
        (slug, display_name),
    )
    pid = conn.execute("SELECT id FROM profiles WHERE slug=?", (slug,)).fetchone()["id"]

    allowed = {
        "min_views", "min_duration_s", "max_duration_s", "categories", "exclude_mature",
        "uploads_per_day", "publish_times", "timezone", "require_approval",
        "watermark_path", "title_template", "tags", "censor_enabled", "enabled",
    }
    updates = {k: v for k, v in cfg.items() if k in allowed}
    for key in ("categories", "publish_times", "tags"):
        if isinstance(updates.get(key), (list, dict)):
            updates[key] = json.dumps(updates[key], ensure_ascii=False)
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE profiles SET {sets} WHERE id = ?", (*updates.values(), pid))
    return pid


def link_source(conn: sqlite3.Connection, profile_id: int, source_id: int,
                weight: float = 1.0) -> None:
    conn.execute(
        "INSERT INTO profile_sources (profile_id, source_id, weight) VALUES (?,?,?) "
        "ON CONFLICT (profile_id, source_id) DO UPDATE SET weight = excluded.weight",
        (profile_id, source_id, weight),
    )

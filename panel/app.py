"""Panel local del pipeline de clips.

    python panel/app.py     ->  http://127.0.0.1:8000

Expone la misma lógica que la CLI para que el panel definitivo en Next.js pueda
consumir esta API tal cual.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "render"))

import db as db_mod            # noqa: E402
import pipeline as pipe        # noqa: E402
import categories              # noqa: E402
import fun                     # noqa: E402
import titles                  # noqa: E402
import youtube_client as yt    # noqa: E402
import branding                # noqa: E402
import jobs                    # noqa: E402
import storage                 # noqa: E402

app = FastAPI(title="Clip Pipeline")
STATIC = Path(__file__).resolve().parent / "static"
# hls.js se sirve local: sin dependencia de CDN, el panel funciona aunque no haya red.
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Los MP4 renderizados se sirven para poder verlos en el panel antes de subirlos.
MEDIA_DIR = ROOT / "media"
(MEDIA_DIR / "out").mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

# Los renders que quedaron a medias en el proceso anterior no pueden seguir vivos: su
# pool de hilos se fue con él. Si no se marcan como fallidos aquí, enqueue() los ve
# como "en curso" y se niega a reintentar, y el botón Renderizar deja de funcionar.
_zombis = jobs.reap_stale(startup=True)
if _zombis:
    print(f"  {_zombis} render(s) interrumpidos del arranque anterior, marcados para reintentar")

DEFAULT_PROFILE = ("sin-asignar", "Sin asignar")


def get_conn():
    conn = db_mod.connect()
    db_mod.upsert_profile(conn, *DEFAULT_PROFILE)
    return conn


# ── Modelos ────────────────────────────────────────────────────────────────────
class AddSources(BaseModel):
    text: str
    pages_all: int = 15
    pages_month: int = 5


class Decision(BaseModel):
    status: str          # approved | rejected
    profile_slug: str = DEFAULT_PROFILE[0]


class SourcePatch(BaseModel):
    enabled: bool | None = None
    has_consent: bool | None = None


class ProfileCreate(BaseModel):
    display_name: str
    slug: str | None = None


class ProfilePatch(BaseModel):
    display_name: str | None = None
    enabled: bool | None = None
    min_views: int | None = None
    min_duration_s: int | None = None
    max_duration_s: int | None = None
    uploads_per_day: int | None = None
    publish_times: list[str] | None = None
    timezone: str | None = None
    require_approval: bool | None = None
    watermark_path: str | None = None
    title_template: str | None = None
    tags: list[str] | None = None
    censor_enabled: bool | None = None
    cta_enabled: bool | None = None           # botones de suscribirse/compartir
    output_format: str | None = None          # short | video | both
    vertical_style: str | None = None         # blur | crop
    compilation_clips: int | None = None
    compilation_day: str | None = None


class SourceLink(BaseModel):
    source_id: int
    linked: bool
    weight: float = 1.0


class BlacklistAdd(BaseModel):
    term: str
    severity: str = "bleep"      # bleep | reject
    notes: str | None = None


# ── Rutas ──────────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/sources")
def list_sources():
    conn = get_conn()
    try:
        stats = pipe.all_source_stats(conn)
    finally:
        conn.close()

    agg = {t: sum(s["per_month"].get(t, 0) for s in stats if s["enabled"])
           for t in pipe.THRESHOLDS}
    return {"sources": stats, "aggregate": agg,
            "per_day": {t: round(agg[t] / 30, 2) for t in pipe.THRESHOLDS}}


@app.post("/api/sources")
def add_sources(body: AddSources):
    parsed = pipe.parse_sources(body.text)
    if not parsed:
        raise HTTPException(400, "No se reconoció ningún canal en el texto pegado")

    conn = get_conn()
    results = []
    try:
        for platform, slug in parsed:
            res = pipe.ingest_source(conn, platform, slug,
                                     pages_all=body.pages_all, pages_month=body.pages_month)
            if res["ok"]:
                res["stats"] = pipe.source_stats(conn, res["source_id"])
            results.append(res)
    finally:
        conn.close()
    return {"results": results}


@app.patch("/api/sources/{source_id}")
def patch_source(source_id: int, body: SourcePatch):
    conn = get_conn()
    try:
        if body.enabled is not None:
            conn.execute("UPDATE sources SET enabled=? WHERE id=?", (int(body.enabled), source_id))
        if body.has_consent is not None:
            conn.execute("UPDATE sources SET has_consent=? WHERE id=?",
                         (int(body.has_consent), source_id))
        conn.commit()
        return pipe.source_stats(conn, source_id)
    finally:
        conn.close()


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM clip_candidates WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/clips")
def list_clips(source_id: int | None = None, min_views: int = 0, max_duration: int = 90,
               min_duration: int = 8, window: str = "week", status: str = "all",
               sort: str = "views", min_age_hours: int = 12, limit: int = 200,
               profile_slug: str = DEFAULT_PROFILE[0], category_group: str | None = None):
    """Lista candidatos. Por defecto, frescos (última semana) y ya madurados.

    `min_age_hours` existe porque un clip recién publicado tiene 0 vistas: si no se le
    da tiempo de acumular, cualquier ranking por vistas lo entierra injustamente.
    """
    where = ["c.duration_s BETWEEN ? AND ?", "c.views >= ?"]
    params: list = [min_duration, max_duration, min_views]

    if source_id:
        where.append("c.source_id = ?")
        params.append(source_id)
    if category_group:
        where.append("COALESCE(c.category_group, 'Otros') = ?")
        params.append(category_group)

    days = {"day": 1, "week": 7, "month": 30}.get(window)
    if days:
        where.append(f"c.created_at >= datetime('now', '-{days} days')")
    if min_age_hours > 0:
        where.append(f"c.created_at <= datetime('now', '-{int(min_age_hours)} hours')")

    order = {"views": "c.views DESC", "recent": "c.created_at DESC",
             "velocity": "c.views_per_day DESC"}.get(sort, "c.views DESC")

    conn = get_conn()
    try:
        prow = conn.execute("SELECT id FROM profiles WHERE slug=?", (profile_slug,)).fetchone()
        if not prow:
            raise HTTPException(404, f"profile '{profile_slug}' no existe")
        pid = prow["id"]

        # Un profile real sólo ve los clips de las fuentes que tiene asignadas.
        if profile_slug != DEFAULT_PROFILE[0]:
            linked = [r["source_id"] for r in conn.execute(
                "SELECT source_id FROM profile_sources WHERE profile_id=?", (pid,))]
            if linked:
                where.append(f"c.source_id IN ({','.join('?' * len(linked))})")
                params.extend(linked)

        if status != "all":
            where.append("COALESCE(q.status,'new') = ?")
            params.append(status)

        rows = conn.execute(
            f"""
            SELECT c.*, s.slug AS source_slug, s.platform,
                   COALESCE(q.status,'new') AS status
            FROM clip_candidates c
            JOIN sources s ON s.id = c.source_id
            LEFT JOIN profile_queue q ON q.clip_id = c.id AND q.profile_id = {pid}
            WHERE {' AND '.join(where)}
            ORDER BY {order} LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return {"clips": [dict(r) for r in rows], "count": len(rows)}
    finally:
        conn.close()


@app.post("/api/clips/{clip_id}/decide")
def decide(clip_id: int, body: Decision):
    if body.status not in {"approved", "rejected", "new"}:
        raise HTTPException(400, "status inválido")

    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM profiles WHERE slug=?",
                           (body.profile_slug,)).fetchone()
        if not row:
            raise HTTPException(404, f"profile '{body.profile_slug}' no existe")
        conn.execute(
            "INSERT INTO profile_queue (profile_id, clip_id, status, decided_at, decided_by) "
            "VALUES (?,?,?,datetime('now'),'panel') "
            "ON CONFLICT (profile_id, clip_id) DO UPDATE SET "
            "status=excluded.status, decided_at=datetime('now'), decided_by='panel'",
            (row["id"], clip_id, body.status),
        )
        conn.commit()
        return {"ok": True, "clip_id": clip_id, "status": body.status}
    finally:
        conn.close()


# ── PROFILES (canales de YouTube destino) ──────────────────────────────────────
JSON_FIELDS = ("publish_times", "tags", "categories")


def _profile_row(conn, row) -> dict:
    """Serializa un profile con sus fuentes, blacklist y estado de la cola."""
    p = dict(row)
    for f in JSON_FIELDS:
        if p.get(f):
            try:
                p[f] = json.loads(p[f])
            except (json.JSONDecodeError, TypeError):
                p[f] = []
        else:
            p[f] = []

    p["sources"] = [dict(r) for r in conn.execute(
        "SELECT s.id, s.slug, s.platform, ps.weight FROM profile_sources ps "
        "JOIN sources s ON s.id = ps.source_id WHERE ps.profile_id = ? ORDER BY s.slug",
        (p["id"],))]

    p["blacklist"] = [dict(r) for r in conn.execute(
        "SELECT id, term, severity, notes FROM blacklist_terms "
        "WHERE profile_id = ? OR profile_id IS NULL ORDER BY term", (p["id"],))]

    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM profile_queue WHERE profile_id = ? GROUP BY status",
        (p["id"],))}
    p["queue"] = {k: counts.get(k, 0)
                  for k in ("new", "approved", "rejected", "rendered", "uploaded", "failed")}

    yt = conn.execute("SELECT channel_title, yt_channel_id, refresh_token IS NOT NULL AS connected "
                      "FROM youtube_accounts WHERE profile_id = ?", (p["id"],)).fetchone()
    p["youtube"] = ({"connected": bool(yt["connected"]), "channel_title": yt["channel_title"],
                     "yt_channel_id": yt["yt_channel_id"]} if yt
                    else {"connected": False, "channel_title": None, "yt_channel_id": None})
    return p


@app.get("/api/profiles")
def list_profiles():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM profiles ORDER BY created_at").fetchall()
        return {"profiles": [_profile_row(conn, r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/profiles")
def create_profile(body: ProfileCreate):
    slug = (body.slug or body.display_name).lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    if not slug:
        raise HTTPException(400, "Nombre inválido")

    conn = get_conn()
    try:
        pid = db_mod.upsert_profile(conn, slug, body.display_name.strip())
        conn.commit()
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (pid,)).fetchone()
        return _profile_row(conn, row)
    finally:
        conn.close()


@app.patch("/api/profiles/{profile_id}")
def patch_profile(profile_id: int, body: ProfilePatch):
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(400, "Nada que actualizar")

    for f in JSON_FIELDS:
        if isinstance(updates.get(f), list):
            updates[f] = json.dumps(updates[f], ensure_ascii=False)
    for f in ("enabled", "require_approval", "censor_enabled", "cta_enabled"):
        if f in updates:
            updates[f] = int(bool(updates[f]))

    conn = get_conn()
    try:
        sets = ", ".join(f"{k} = ?" for k in updates)
        cur = conn.execute(f"UPDATE profiles SET {sets} WHERE id = ?",
                           (*updates.values(), profile_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Profile no encontrado")
        conn.commit()
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return _profile_row(conn, row)
    finally:
        conn.close()


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT slug FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if row and row["slug"] == DEFAULT_PROFILE[0]:
            raise HTTPException(400, "No se puede borrar el profile por defecto")
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/profiles/{profile_id}/sources")
def link_profile_source(profile_id: int, body: SourceLink):
    conn = get_conn()
    try:
        if body.linked:
            db_mod.link_source(conn, profile_id, body.source_id, body.weight)
        else:
            conn.execute("DELETE FROM profile_sources WHERE profile_id=? AND source_id=?",
                         (profile_id, body.source_id))
        conn.commit()
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return _profile_row(conn, row)
    finally:
        conn.close()


@app.post("/api/profiles/{profile_id}/blacklist")
def add_blacklist(profile_id: int, body: BlacklistAdd):
    term = body.term.strip().lower()
    if not term:
        raise HTTPException(400, "Término vacío")
    if body.severity not in {"bleep", "reject"}:
        raise HTTPException(400, "severity debe ser 'bleep' o 'reject'")

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO blacklist_terms (profile_id, term, severity, notes) VALUES (?,?,?,?)",
            (profile_id, term, body.severity, body.notes))
        conn.commit()
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return _profile_row(conn, row)
    finally:
        conn.close()


@app.delete("/api/blacklist/{term_id}")
def del_blacklist(term_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM blacklist_terms WHERE id = ?", (term_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ── LOS DE HOY: selección automática + render + preview ────────────────────────
class QueuePatch(BaseModel):
    title: str | None = None
    hook: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    status: str | None = None


@app.post("/api/profiles/{profile_id}/propose")
def propose_today(profile_id: int, min_age_hours: int = 12, window_days: int = 7):
    """Elige los mejores clips frescos para hoy según la config del canal.

    Excluye lo que ya pasó por la cola de este canal: no se repite un clip que ya
    subiste, descartaste o está en proceso.
    """
    conn = get_conn()
    try:
        prof = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not prof:
            raise HTTPException(404, "Canal no encontrado")

        linked = [r["source_id"] for r in conn.execute(
            "SELECT source_id FROM profile_sources WHERE profile_id=?", (profile_id,))]
        if not linked:
            raise HTTPException(400, "Este canal no tiene fuentes asignadas todavía")

        # Refrescamos las señales de gracia antes de elegir, para no proponer con
        # racimos desactualizados.
        fun.refresh(conn)

        marks = ",".join("?" * len(linked))
        # ROW_NUMBER por racimo: si 15 personas clipearon el mismo momento, se publica
        # UNO solo (el mejor del racimo) en vez de repetir el chiste 15 veces.
        # Los clips sin racimo se tratan cada uno como su propio grupo.
        rows = conn.execute(
            f"""WITH ranked AS (
                    SELECT c.*, s.slug AS source_slug, s.platform,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(c.cluster_key, 'solo:' || c.id)
                               ORDER BY c.views DESC, c.duration_s ASC
                           ) AS rn
                    FROM clip_candidates c
                    JOIN sources s ON s.id = c.source_id
                    WHERE c.source_id IN ({marks})
                      AND c.views >= ?
                      AND c.duration_s BETWEEN ? AND ?
                      AND c.created_at >= datetime('now', ?)
                      AND c.created_at <= datetime('now', ?)
                      AND c.is_mature = 0
                      AND c.id NOT IN (
                          SELECT clip_id FROM profile_queue WHERE profile_id = ?)
                )
                SELECT * FROM ranked WHERE rn = 1
                ORDER BY fun_score DESC, views DESC
                LIMIT ?""",
            (*linked, prof["min_views"], prof["min_duration_s"], prof["max_duration_s"],
             f"-{int(window_days)} days", f"-{int(min_age_hours)} hours",
             profile_id, prof["uploads_per_day"]),
        ).fetchall()

        tpl = prof["title_template"]
        ptags = json.loads(prof["tags"]) if prof["tags"] else []
        is_short = prof["output_format"] in ("short", "both")

        created = []
        for r in rows:
            clip = dict(r)
            cur = conn.execute(
                "INSERT INTO profile_queue (profile_id, clip_id, score, eligible, status, "
                "title, description, tags, kind, hook) VALUES (?,?,?,1,'new',?,?,?,?,?)",
                (profile_id, clip["id"], clip["views"],
                 titles.build_title(clip, tpl, is_short=is_short),
                 titles.build_description(clip, dict(prof), is_short=is_short),
                 json.dumps(titles.build_tags(clip, ptags), ensure_ascii=False),
                 "short" if is_short else "video",
                 titles.build_hook(clip)),
            )
            created.append(cur.lastrowid)
        conn.commit()

        return {"created": len(created), "requested": prof["uploads_per_day"],
                "queue_ids": created}
    finally:
        conn.close()


@app.get("/api/profiles/{profile_id}/today")
def today_lineup(profile_id: int):
    """La tanda actual del canal: lo propuesto, renderizado o subido y aún no publicado."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT q.id AS queue_id, q.status, q.title, q.description, q.tags, q.kind,
                      q.hook, q.queued_at, c.id AS clip_id, c.title AS clip_title, c.views,
                      c.duration_s, c.thumbnail_url, c.embed_url, c.video_url,
                      c.clip_page_url, c.created_at, c.fun_score, c.hype_clips,
                      s.slug AS source_slug, s.platform,
                      r.status AS render_status, r.output_path, r.error AS render_error,
                      u.yt_video_id, u.privacy AS yt_privacy, u.published_at AS yt_published_at
               FROM profile_queue q
               JOIN clip_candidates c ON c.id = q.clip_id
               JOIN sources s ON s.id = c.source_id
               LEFT JOIN renders r ON r.id = (
                   SELECT id FROM renders WHERE queue_id = q.id ORDER BY id DESC LIMIT 1)
               LEFT JOIN uploads u ON u.id = (
                   SELECT id FROM uploads WHERE queue_id = q.id ORDER BY id DESC LIMIT 1)
               WHERE q.profile_id = ?
                 AND q.status IN ('new','approved','rendered','failed','uploaded')
               ORDER BY q.queued_at DESC, c.views DESC""",
            (profile_id,)).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"]) if d["tags"] else []
            items.append(d)
        return {"items": items}
    finally:
        conn.close()


@app.patch("/api/queue/{queue_id}")
def patch_queue(queue_id: int, body: QueuePatch):
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(400, "Nada que actualizar")
    if isinstance(updates.get("tags"), list):
        updates["tags"] = json.dumps(updates["tags"], ensure_ascii=False)

    conn = get_conn()
    try:
        sets = ", ".join(f"{k} = ?" for k in updates)
        cur = conn.execute(f"UPDATE profile_queue SET {sets} WHERE id = ?",
                           (*updates.values(), queue_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "No existe ese elemento en la cola")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/queue/{queue_id}")
def delete_queue(queue_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM profile_queue WHERE id = ?", (queue_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/queue/{queue_id}/render")
def start_render(queue_id: int):
    conn = get_conn()
    try:
        if not conn.execute("SELECT 1 FROM profile_queue WHERE id=?", (queue_id,)).fetchone():
            raise HTTPException(404, "No existe ese elemento en la cola")
    finally:
        conn.close()
    return {"render_id": jobs.enqueue(queue_id), "status": "pending"}


@app.get("/api/queue/{queue_id}/render")
def render_status(queue_id: int):
    st = jobs.status(queue_id)
    if not st:
        return {"status": "none"}
    return st


# ── SUBIDA A YOUTUBE ───────────────────────────────────────────────────────────
class UploadReq(BaseModel):
    privacy: str = "private"      # private | unlisted | public
    schedule: bool = False        # programar en el próximo horario del canal


def next_publish_slot(profile: dict) -> tuple[str, str] | None:
    """Próximo horario de publicación del canal, en UTC ISO y en hora local legible.

    YouTube exige publishAt en UTC; la config del canal está en su zona horaria.
    """
    try:
        times = json.loads(profile["publish_times"]) if profile["publish_times"] else []
    except (json.JSONDecodeError, TypeError):
        times = []
    if not times:
        return None

    try:
        tz = ZoneInfo(profile["timezone"] or "America/Bogota")
    except Exception:  # noqa: BLE001 - zona inválida en config
        tz = ZoneInfo("America/Bogota")

    now = datetime.now(tz)
    slots: list[datetime] = []
    for day_offset in (0, 1):
        for t in times:
            try:
                hh, mm = (int(x) for x in str(t).split(":")[:2])
            except ValueError:
                continue
            cand = (now + timedelta(days=day_offset)).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
            if cand > now + timedelta(minutes=5):   # margen: YouTube rechaza el pasado
                slots.append(cand)
    if not slots:
        return None

    nxt = min(slots)
    return (nxt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            nxt.strftime("%Y-%m-%d %H:%M"))


@app.get("/api/queue/{queue_id}/upload")
def upload_preview(queue_id: int):
    """Qué pasaría al subir: sirve para mostrarlo en el panel antes de confirmar."""
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT q.*, p.publish_times, p.timezone, p.uploads_per_day,
                      y.refresh_token IS NOT NULL AS connected, y.channel_title
               FROM profile_queue q JOIN profiles p ON p.id = q.profile_id
               LEFT JOIN youtube_accounts y ON y.profile_id = q.profile_id
               WHERE q.id = ?""", (queue_id,)).fetchone()
        if not row:
            raise HTTPException(404, "No existe ese elemento en la cola")

        slot = next_publish_slot(dict(row))
        return {"connected": bool(row["connected"]), "channel_title": row["channel_title"],
                "next_slot_utc": slot[0] if slot else None,
                "next_slot_local": slot[1] if slot else None,
                "timezone": row["timezone"]}
    finally:
        conn.close()


@app.post("/api/queue/{queue_id}/upload")
def upload_to_youtube(queue_id: int, body: UploadReq):
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT q.*, p.publish_times, p.timezone, y.refresh_token, y.yt_channel_id
               FROM profile_queue q JOIN profiles p ON p.id = q.profile_id
               LEFT JOIN youtube_accounts y ON y.profile_id = q.profile_id
               WHERE q.id = ?""", (queue_id,)).fetchone()
        if not row:
            raise HTTPException(404, "No existe ese elemento en la cola")
        if not row["refresh_token"]:
            raise HTTPException(400, "Este canal no tiene YouTube conectado")

        rend = conn.execute(
            "SELECT output_path FROM renders WHERE queue_id = ? AND status = 'done' "
            "ORDER BY id DESC LIMIT 1", (queue_id,)).fetchone()
        if not rend or not rend["output_path"]:
            raise HTTPException(400, "Primero hay que renderizar este clip")

        path = ROOT / rend["output_path"]
        if not path.exists():
            raise HTTPException(400, f"No se encuentra el archivo renderizado: {path.name}")

        publish_at = None
        if body.schedule:
            slot = next_publish_slot(dict(row))
            if not slot:
                raise HTTPException(400, "El canal no tiene horarios de publicación válidos")
            publish_at = slot[0]

        try:
            tokens = yt.refresh_access_token(row["refresh_token"])
            tags = json.loads(row["tags"]) if row["tags"] else []
            result = yt.upload_video(
                tokens["access_token"], path,
                title=row["title"] or "Clip",
                description=row["description"] or "",
                tags=tags,
                privacy=body.privacy,
                publish_at=publish_at,
            )
        except yt.YouTubeError as exc:
            raise HTTPException(400, str(exc))

        conn.execute(
            "INSERT INTO uploads (queue_id, yt_video_id, title, tags, privacy, "
            "published_at, kind) VALUES (?,?,?,?,?,?,?)",
            (queue_id, result["id"], result["title"], row["tags"], result["privacy"],
             publish_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             row["kind"] or "short"))
        conn.execute("UPDATE profile_queue SET status = 'uploaded' WHERE id = ?", (queue_id,))
        conn.commit()

        # La copia buena ya vive en YouTube: liberar el disco local.
        freed = storage.cleanup_after_upload(queue_id)

        return {"ok": True, **result, "publish_at": publish_at,
                "freed_mb": round(freed / 1e6, 1),
                "note": ("Subido como privado. Mientras el proyecto no pase la auditoría "
                         "de YouTube, las subidas quedan forzadas a privado aunque pidas "
                         "público.") if result["privacy"] != body.privacy else None}
    finally:
        conn.close()


# ── CATEGORÍAS ─────────────────────────────────────────────────────────────────
@app.get("/api/categories")
def list_categories(days: int = 30, min_views: int = 300, profile_slug: str | None = None):
    """Familias de categorías con lo que produce cada una y quién manda en ella."""
    conn = get_conn()
    try:
        source_ids = None
        if profile_slug and profile_slug != DEFAULT_PROFILE[0]:
            row = conn.execute("SELECT id FROM profiles WHERE slug=?",
                               (profile_slug,)).fetchone()
            if row:
                source_ids = [r["source_id"] for r in conn.execute(
                    "SELECT source_id FROM profile_sources WHERE profile_id=?",
                    (row["id"],))] or None

        return {"groups": categories.stats(conn, days=days, min_views=min_views,
                                           source_ids=source_ids),
                "order": categories.GROUP_ORDER, "days": days, "min_views": min_views}
    finally:
        conn.close()


@app.post("/api/categories/refresh")
def refresh_categories():
    """Reclasifica todos los clips. Se corre tras cambiar las reglas de agrupación."""
    conn = get_conn()
    try:
        return categories.refresh_groups(conn)
    finally:
        conn.close()


# ── RECOPILATORIOS ─────────────────────────────────────────────────────────────
class CompilationPatch(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    description: str | None = None
    tags: list[str] | None = None


@app.post("/api/profiles/{profile_id}/compilation")
def create_compilation(profile_id: int, window_days: int = 7, min_age_hours: int = 12):
    """Arma un recopilatorio con los mejores clips del periodo, uno por momento."""
    conn = get_conn()
    try:
        prof = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not prof:
            raise HTTPException(404, "Canal no encontrado")

        linked = [r["source_id"] for r in conn.execute(
            "SELECT source_id FROM profile_sources WHERE profile_id=?", (profile_id,))]
        if not linked:
            raise HTTPException(400, "Este canal no tiene fuentes asignadas")

        fun.refresh(conn)
        n = max(3, prof["compilation_clips"] or 10)
        marks = ",".join("?" * len(linked))
        # Igual que en los shorts: uno por racimo, para no repetir el mismo momento.
        rows = conn.execute(
            f"""WITH ranked AS (
                    SELECT c.*, s.slug AS source_slug,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(c.cluster_key, 'solo:' || c.id)
                               ORDER BY c.views DESC) AS rn
                    FROM clip_candidates c JOIN sources s ON s.id = c.source_id
                    WHERE c.source_id IN ({marks})
                      AND c.views >= ? AND c.duration_s BETWEEN ? AND ?
                      AND c.created_at >= datetime('now', ?)
                      AND c.created_at <= datetime('now', ?)
                      AND c.is_mature = 0
                )
                SELECT * FROM ranked WHERE rn = 1
                ORDER BY fun_score DESC, views DESC LIMIT ?""",
            (*linked, prof["min_views"], prof["min_duration_s"], prof["max_duration_s"],
             f"-{int(window_days)} days", f"-{int(min_age_hours)} hours", n)).fetchall()

        if len(rows) < 3:
            raise HTTPException(400, f"Sólo hay {len(rows)} clips que cumplan la config; "
                                     "hacen falta al menos 3 para un recopilatorio")

        titulo = f"Lo mejor de {prof['display_name']}"
        cur = conn.execute(
            "INSERT INTO compilations (profile_id, title, subtitle, tags, clips_count, "
            "status) VALUES (?,?,?,?,?,'draft')",
            (profile_id, titulo, f"Los {len(rows)} mejores momentos",
             prof["tags"], len(rows)))
        comp_id = cur.lastrowid

        for pos, r in enumerate(rows):
            clip = dict(r)
            conn.execute(
                "INSERT INTO compilation_items (compilation_id, clip_id, position, hook) "
                "VALUES (?,?,?,?)",
                (comp_id, clip["id"], pos, titles.build_hook(clip)))
        conn.commit()
        return {"id": comp_id, "clips": len(rows), "title": titulo}
    finally:
        conn.close()


@app.get("/api/profiles/{profile_id}/compilations")
def list_compilations(profile_id: int):
    conn = get_conn()
    try:
        out = []
        for r in conn.execute(
                "SELECT * FROM compilations WHERE profile_id=? ORDER BY id DESC LIMIT 20",
                (profile_id,)):
            d = dict(r)
            d["items"] = [dict(x) for x in conn.execute(
                """SELECT i.position, i.hook, c.title, c.views, c.duration_s,
                          c.thumbnail_url, s.slug AS source_slug, s.platform
                   FROM compilation_items i
                   JOIN clip_candidates c ON c.id = i.clip_id
                   JOIN sources s ON s.id = c.source_id
                   WHERE i.compilation_id = ? ORDER BY i.position""", (d["id"],))]
            out.append(d)
        return {"compilations": out}
    finally:
        conn.close()


@app.patch("/api/compilations/{comp_id}")
def patch_compilation(comp_id: int, body: CompilationPatch):
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if isinstance(updates.get("tags"), list):
        updates["tags"] = json.dumps(updates["tags"], ensure_ascii=False)
    if not updates:
        raise HTTPException(400, "Nada que actualizar")
    conn = get_conn()
    try:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE compilations SET {sets} WHERE id = ?",
                     (*updates.values(), comp_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/compilations/{comp_id}")
def delete_compilation(comp_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT output_path FROM compilations WHERE id=?",
                           (comp_id,)).fetchone()
        if row and row["output_path"]:
            storage.drop_render(row["output_path"])
        conn.execute("DELETE FROM compilations WHERE id = ?", (comp_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/compilations/{comp_id}/render")
def render_compilation(comp_id: int):
    conn = get_conn()
    try:
        if not conn.execute("SELECT 1 FROM compilations WHERE id=?", (comp_id,)).fetchone():
            raise HTTPException(404, "No existe ese recopilatorio")
    finally:
        conn.close()
    jobs.enqueue_compilation(comp_id)
    return {"ok": True, "status": "rendering"}


@app.post("/api/compilations/{comp_id}/upload")
def upload_compilation(comp_id: int, body: UploadReq):
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT c.*, y.refresh_token FROM compilations c
               LEFT JOIN youtube_accounts y ON y.profile_id = c.profile_id
               WHERE c.id = ?""", (comp_id,)).fetchone()
        if not row:
            raise HTTPException(404, "No existe ese recopilatorio")
        if not row["refresh_token"]:
            raise HTTPException(400, "Este canal no tiene YouTube conectado")
        if row["status"] != "ready" or not row["output_path"]:
            raise HTTPException(400, "Primero hay que renderizarlo")

        path = ROOT / row["output_path"]
        if not path.exists():
            raise HTTPException(400, f"No se encuentra el archivo: {path.name}")

        # Los capítulos van al principio de la descripción: YouTube exige que el
        # primero sea 00:00 y los detecta sólo si están en la descripción.
        desc = "\n\n".join(x for x in (row["chapters"], row["description"]) if x)

        try:
            tokens = yt.refresh_access_token(row["refresh_token"])
            tags = json.loads(row["tags"]) if row["tags"] else []
            result = yt.upload_video(
                tokens["access_token"], path,
                title=row["title"] or "Recopilatorio",
                description=desc, tags=tags, privacy=body.privacy)
        except yt.YouTubeError as exc:
            raise HTTPException(400, str(exc))

        conn.execute(
            "UPDATE compilations SET status='uploaded', yt_video_id=?, privacy=?, "
            "published_at=datetime('now') WHERE id=?",
            (result["id"], result["privacy"], comp_id))
        conn.commit()
        liberado = storage.drop_render(row["output_path"])
        conn.execute("UPDATE compilations SET output_path=NULL WHERE id=?", (comp_id,))
        conn.commit()
        return {"ok": True, **result, "freed_mb": round(liberado / 1e6, 1)}
    finally:
        conn.close()


# ── CONEXIÓN CON YOUTUBE (OAuth) ───────────────────────────────────────────────
# Debe coincidir EXACTAMENTE con el URI autorizado en Google Cloud. Google trata
# localhost y 127.0.0.1 como distintos, así que el panel se abre en localhost.
REDIRECT_URI = "http://localhost:8000/oauth/callback"


@app.get("/api/youtube/status")
def youtube_status():
    return {"configured": yt.is_configured(), "redirect_uri": REDIRECT_URI,
            "scopes": yt.SCOPES}


@app.get("/api/profiles/{profile_id}/youtube/auth")
def youtube_auth(profile_id: int):
    if not yt.is_configured():
        raise HTTPException(400, "Faltan YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET en el .env")
    return {"url": yt.auth_url(REDIRECT_URI, state=str(profile_id))}


@app.get("/oauth/callback")
def oauth_callback(code: str | None = None, state: str | None = None,
                   error: str | None = None):
    """Recibe el retorno de Google, canjea el código y guarda el refresh_token."""
    if error:
        return HTMLResponse(_callback_page(f"Google devolvió un error: {error}", ok=False))
    if not code or not state:
        return HTMLResponse(_callback_page("Faltó el código o el state.", ok=False))

    try:
        profile_id = int(state)
        tokens = yt.exchange_code(code, REDIRECT_URI)
        access = tokens["access_token"]
        refresh = tokens.get("refresh_token")
        if not refresh:
            return HTMLResponse(_callback_page(
                "Google no devolvió refresh_token. Revoca el acceso de la app en "
                "myaccount.google.com/permissions y vuelve a conectar.", ok=False))

        channel = yt.get_my_channel(access)

        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO youtube_accounts (profile_id, channel_title, yt_channel_id, "
                "refresh_token, scopes, avatar_url, updated_at) "
                "VALUES (?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT (profile_id) DO UPDATE SET channel_title=excluded.channel_title, "
                "yt_channel_id=excluded.yt_channel_id, refresh_token=excluded.refresh_token, "
                "scopes=excluded.scopes, avatar_url=excluded.avatar_url, "
                "updated_at=datetime('now')",
                (profile_id, channel["title"], channel["id"], refresh, " ".join(yt.SCOPES),
                 channel.get("avatar_url")))
            conn.commit()
        finally:
            conn.close()

        return HTMLResponse(_callback_page(
            f"Canal <b>{channel['title']}</b> conectado. "
            f"{channel['subscribers']:,} suscriptores · {channel['videos']:,} videos.", ok=True))
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(_callback_page(str(exc), ok=False))


def _callback_page(msg: str, ok: bool) -> str:
    color = "#53d769" if ok else "#f2545b"
    return f"""<!doctype html><meta charset="utf-8">
<body style="background:#0d1017;color:#e6ebf2;font:15px/1.6 system-ui;padding:60px;text-align:center">
  <div style="max-width:560px;margin:auto">
    <h2 style="color:{color}">{'Conectado' if ok else 'No se pudo conectar'}</h2>
    <p>{msg}</p>
    <p><a href="http://localhost:8000/" style="color:#4a9eff">Volver al panel</a></p>
  </div></body>"""


@app.post("/api/profiles/{profile_id}/watermark/from-youtube")
def watermark_from_youtube(profile_id: int, with_label: bool = True):
    """Usa el avatar del canal conectado como marca de agua.

    Así la marca viaja con el archivo: si alguien descarga el Short y lo resube, tu
    logo sigue ahí.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT p.slug, p.display_name, y.refresh_token, y.avatar_url, y.channel_title "
            "FROM profiles p LEFT JOIN youtube_accounts y ON y.profile_id = p.id "
            "WHERE p.id = ?", (profile_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Canal no encontrado")
        if not row["refresh_token"]:
            raise HTTPException(400, "Este canal no tiene YouTube conectado")

        avatar = row["avatar_url"]
        title = row["channel_title"]
        if not avatar:
            # Se conectó antes de que guardáramos el avatar: pedirlo ahora.
            try:
                tokens = yt.refresh_access_token(row["refresh_token"])
                channel = yt.get_my_channel(tokens["access_token"])
            except yt.YouTubeError as exc:
                raise HTTPException(400, str(exc))
            avatar = channel.get("avatar_url")
            title = channel.get("title")
            conn.execute("UPDATE youtube_accounts SET avatar_url=?, channel_title=? "
                         "WHERE profile_id=?", (avatar, title, profile_id))
        if not avatar:
            raise HTTPException(400, "El canal no tiene imagen de perfil en YouTube")

        try:
            rel = branding.watermark_for_profile(
                row["slug"], avatar, label=title if with_label else None)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"No se pudo generar la marca: {exc}")

        conn.execute("UPDATE profiles SET watermark_path=? WHERE id=?", (rel, profile_id))
        conn.commit()
        return {"ok": True, "watermark_path": rel, "channel_title": title}
    finally:
        conn.close()


# ── BOT EN VIVO ────────────────────────────────────────────────────────────────
@app.get("/api/live")
def live_status(hours: int = 24, limit: int = 40):
    """Directos activos y picos de risa detectados por el bot de chat."""
    conn = get_conn()
    try:
        sesiones = [dict(r) for r in conn.execute(
            """SELECT l.*, s.slug, s.platform FROM live_sessions l
               JOIN sources s ON s.id = l.source_id
               WHERE l.ended_at IS NULL AND l.seen_at >= datetime('now','-10 minutes')
               ORDER BY l.viewers DESC""")]

        picos = []
        for r in conn.execute(
            f"""SELECT p.*, s.slug, s.platform, c.title AS clip_title, c.views AS clip_views
                FROM chat_peaks p
                JOIN sources s ON s.id = p.source_id
                LEFT JOIN clip_candidates c ON c.id = p.clip_id
                WHERE p.at_utc >= datetime('now','-{int(hours)} hours')
                ORDER BY p.at_utc DESC LIMIT ?""", (limit,)):
            d = dict(r)
            try:
                d["samples"] = json.loads(d["samples"]) if d["samples"] else []
            except (json.JSONDecodeError, TypeError):
                d["samples"] = []
            picos.append(d)

        total = conn.execute(
            f"SELECT COUNT(*) n, SUM(clip_id IS NOT NULL) enlazados FROM chat_peaks "
            f"WHERE at_utc >= datetime('now','-{int(hours)} hours')").fetchone()

        return {"live": sesiones, "peaks": picos,
                "totals": {"peaks": total["n"] or 0, "linked": total["enlazados"] or 0},
                "twitch_configured": yt is not None and True}
    finally:
        conn.close()


@app.post("/api/live/correlate")
def live_correlate():
    """Reenlaza picos con clips y recalcula la gracia."""
    conn = get_conn()
    try:
        return fun.refresh(conn)
    finally:
        conn.close()


# ── ESPACIO EN DISCO ───────────────────────────────────────────────────────────
@app.get("/api/storage")
def storage_usage():
    return storage.usage()


@app.post("/api/storage/sweep")
def storage_sweep(aggressive: bool = False):
    return storage.sweep(aggressive=aggressive)


@app.delete("/api/profiles/{profile_id}/youtube")
def youtube_disconnect(profile_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM youtube_accounts WHERE profile_id = ?", (profile_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/profiles/{profile_id}/youtube/refresh")
def youtube_refresh(profile_id: int):
    """Prueba el refresh_token y devuelve datos frescos del canal."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT refresh_token FROM youtube_accounts WHERE profile_id=?",
                           (profile_id,)).fetchone()
        if not row or not row["refresh_token"]:
            raise HTTPException(404, "Este canal no tiene YouTube conectado")
        tokens = yt.refresh_access_token(row["refresh_token"])
        channel = yt.get_my_channel(tokens["access_token"])
        conn.execute("UPDATE youtube_accounts SET channel_title=?, yt_channel_id=?, "
                     "updated_at=datetime('now') WHERE profile_id=?",
                     (channel["title"], channel["id"], profile_id))
        conn.commit()
        return {"ok": True, "channel": channel}
    except yt.YouTubeError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@app.post("/api/profiles/{profile_id}/metrics/sync")
def sync_metrics(profile_id: int, days: int = 90):
    """Trae las métricas de YouTube y las guarda contra cada video subido.

    `averageViewPercentage` es la que de verdad manda en Shorts: mide retención, que es
    lo que mueve el algoritmo. Las vistas son la consecuencia, no la causa.

    YouTube tarda 24-48 h en procesar los datos de un video recién subido, así que un
    resultado vacío justo después de subir es normal, no un fallo.
    """
    conn = get_conn()
    try:
        acc = conn.execute(
            "SELECT refresh_token, yt_channel_id FROM youtube_accounts WHERE profile_id=?",
            (profile_id,)).fetchone()
        if not acc or not acc["refresh_token"]:
            raise HTTPException(400, "Este canal no tiene YouTube conectado")

        subidos = [r["yt_video_id"] for r in conn.execute(
            """SELECT u.yt_video_id FROM uploads u
               JOIN profile_queue q ON q.id = u.queue_id
               WHERE q.profile_id = ? AND u.yt_video_id IS NOT NULL""", (profile_id,))]
        comps = [r["yt_video_id"] for r in conn.execute(
            "SELECT yt_video_id FROM compilations WHERE profile_id=? AND yt_video_id IS NOT NULL",
            (profile_id,))]

        try:
            tokens = yt.refresh_access_token(acc["refresh_token"])
            hoy = datetime.now(timezone.utc).date()
            filas = yt.video_metrics(
                tokens["access_token"], acc["yt_channel_id"],
                (hoy - timedelta(days=days)).isoformat(), hoy.isoformat(),
                video_ids=(subidos + comps) or None)
        except yt.YouTubeError as exc:
            raise HTTPException(400, str(exc))

        actualizados = 0
        for f in filas:
            vid = f.get("video")
            if not vid:
                continue
            cur = conn.execute(
                """UPDATE uploads SET views=?, avg_view_pct=?, subs_gained=?, likes=?,
                       comments=?, metrics_at=datetime('now')
                   WHERE yt_video_id=?""",
                (f.get("views"), f.get("averageViewPercentage"),
                 f.get("subscribersGained"), f.get("likes"), f.get("comments"), vid))
            actualizados += cur.rowcount
        conn.commit()

        return {"ok": True, "videos_conocidos": len(subidos) + len(comps),
                "filas_recibidas": len(filas), "actualizados": actualizados,
                "nota": ("YouTube todavía no procesó estos videos. Suele tardar 24-48 h "
                         "desde la subida.") if filas == [] and (subidos or comps) else None}
    finally:
        conn.close()


@app.get("/api/profiles/{profile_id}/metrics")
def profile_metrics(profile_id: int):
    """Métricas del profile.

    `pipeline` sale de nuestra base y es real desde ya. `youtube` queda en null hasta
    que se conecte la cuenta por OAuth y empiecen a subirse videos: no inventamos datos.
    """
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Profile no encontrado")
        prof = _profile_row(conn, row)

        uploads = [dict(r) for r in conn.execute(
            """SELECT u.*, c.title AS clip_title, s.slug AS source_slug
               FROM uploads u
               JOIN profile_queue q ON q.id = u.queue_id
               JOIN clip_candidates c ON c.id = q.clip_id
               JOIN sources s ON s.id = c.source_id
               WHERE q.profile_id = ? ORDER BY u.published_at DESC LIMIT 50""",
            (profile_id,))]

        agg = conn.execute(
            """SELECT COUNT(*) n, SUM(u.views) views, AVG(u.avg_view_pct) ret,
                      SUM(u.subs_gained) subs, SUM(u.likes) likes
               FROM uploads u JOIN profile_queue q ON q.id = u.queue_id
               WHERE q.profile_id = ?""", (profile_id,)).fetchone()

        # Capacidad: ¿las fuentes asignadas dan para el ritmo configurado?
        src_ids = [s["id"] for s in prof["sources"]]
        per_month = 0
        if src_ids:
            marks = ",".join("?" * len(src_ids))
            per_month = conn.execute(
                f"""SELECT COUNT(*) n FROM clip_candidates
                    WHERE source_id IN ({marks}) AND views >= ?
                      AND duration_s BETWEEN ? AND ?
                      AND created_at >= datetime('now','-30 days')""",
                (*src_ids, row["min_views"], row["min_duration_s"], row["max_duration_s"]),
            ).fetchone()["n"]

        return {
            "profile": prof,
            "capacity": {
                "clips_per_month": per_month,
                "sustainable_per_day": round(per_month / 30, 2),
                "target_per_day": row["uploads_per_day"],
                "ok": (per_month / 30) >= row["uploads_per_day"],
            },
            "pipeline": prof["queue"],
            "quota": yt.quota_budget(row["uploads_per_day"]),
            "youtube": {
                "connected": prof["youtube"]["connected"],
                "uploads": agg["n"] or 0,
                "views": agg["views"],
                "avg_retention": agg["ret"],
                "subs_gained": agg["subs"],
                "likes": agg["likes"],
            },
            "recent_uploads": uploads,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import uvicorn
    print("\n  Panel:  http://127.0.0.1:8000\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")

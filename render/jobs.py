"""Trabajos de render en segundo plano.

Renderizar tarda ~30s por clip. Si se hiciera dentro del request HTTP el panel se
quedaría colgado, así que los trabajos van a un pool y el estado se persiste en la
tabla `renders`: el panel pregunta por él cada par de segundos.
"""

from __future__ import annotations

import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "render"))

import compilation         # noqa: E402
import db as db_mod        # noqa: E402
import renderer            # noqa: E402
import storage             # noqa: E402
import transcribe          # noqa: E402

MEDIA = ROOT / "media"

# 2 workers: el render satura CPU y encolar de a más sólo hace que todos vayan lentos.
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="render")


def _load_job(conn, queue_id: int) -> dict | None:
    row = conn.execute(
        """SELECT q.id AS queue_id, q.profile_id, q.hook, c.*,
                  s.slug AS source_slug, s.platform,
                  p.watermark_path, p.output_format, p.vertical_style, p.cta_enabled,
                  p.censor_enabled
           FROM profile_queue q
           JOIN clip_candidates c ON c.id = q.clip_id
           JOIN sources s ON s.id = c.source_id
           JOIN profiles p ON p.id = q.profile_id
           WHERE q.id = ?""", (queue_id,)).fetchone()
    return dict(row) if row else None


IN_PROGRESS = ("pending", "downloading", "transcribing", "rendering")

# Si una fila lleva este tiempo sin latir, su proceso murió: nada tarda tanto sin
# actualizar estado, ni transcribiendo un clip largo.
STALE_MINUTES = 20


def _set_status(conn, render_id: int, status: str, **fields) -> None:
    sets = ["status = ?", "updated_at = datetime('now')"]
    vals: list = [status]
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        vals.append(v)
    conn.execute(f"UPDATE renders SET {', '.join(sets)} WHERE id = ?", (*vals, render_id))
    conn.commit()


def reap_stale(startup: bool = False) -> int:
    """Marca como fallidos los renders cuyo proceso ya no existe.

    En el arranque son TODOS los que estén en curso: el pool de hilos se perdió con el
    proceso anterior, así que ninguno puede seguir vivo. En caliente, sólo los que
    llevan mucho sin dar señales.
    """
    conn = db_mod.connect()
    try:
        marks = ",".join("?" * len(IN_PROGRESS))
        if startup:
            cond, params = "", list(IN_PROGRESS)
        else:
            cond = f"AND COALESCE(updated_at, '1970-01-01') <= datetime('now', '-{STALE_MINUTES} minutes')"
            params = list(IN_PROGRESS)

        filas = conn.execute(
            f"SELECT id, queue_id FROM renders WHERE status IN ({marks}) {cond}",
            params).fetchall()
        if not filas:
            return 0

        motivo = ("Interrumpido al reiniciar el servidor" if startup
                  else f"Sin señales por más de {STALE_MINUTES} minutos")
        ids = [f["id"] for f in filas]
        conn.executemany("UPDATE renders SET status='failed', error=?, "
                         "updated_at=datetime('now') WHERE id=?",
                         [(motivo, i) for i in ids])
        # La cola vuelve a un estado desde el que se puede reintentar.
        conn.executemany("UPDATE profile_queue SET status='new' WHERE id=? "
                         "AND status IN ('rendered','failed')",
                         [(f["queue_id"],) for f in filas])
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def _work(queue_id: int, render_id: int) -> None:
    conn = db_mod.connect()
    clip_id: int | None = None
    try:
        job = _load_job(conn, queue_id)
        if not job:
            _set_status(conn, render_id, "failed", error="No se encontró el clip en la cola")
            return

        clip_id = job["id"]
        _set_status(conn, render_id, "downloading")
        raw = renderer.download_clip(job, MEDIA / "raw")

        audio_graph, transcript_json, bleeps = None, None, 0
        if job.get("censor_enabled"):
            terms = [dict(r) for r in conn.execute(
                "SELECT term, severity FROM blacklist_terms "
                "WHERE profile_id = ? OR profile_id IS NULL", (job["profile_id"],))]
            if terms:
                _set_status(conn, render_id, "transcribing")
                wav = transcribe.extract_audio(raw, MEDIA / "raw" / f"a_{job['id']}.wav")
                try:
                    result = transcribe.transcribe(wav)
                    ranges, rejects = transcribe.find_blacklisted(result["words"], terms)

                    if rejects:
                        # severity 'reject': el clip no se publica, ni pitado.
                        motivo = ", ".join(sorted(set(rejects)))
                        _set_status(conn, render_id, "failed",
                                    error=f"Descartado por la blacklist: {motivo}")
                        conn.execute(
                            "UPDATE profile_queue SET status='rejected', reject_reason=? "
                            "WHERE id=?", (f"blacklist: {motivo}", queue_id))
                        conn.commit()
                        return

                    bleeps = len(ranges)
                    audio_graph = transcribe.censor_audio_graph(ranges)
                    # El texto se censura igual que el audio: pitar la voz y dejar la
                    # palabra escrita en el subtítulo no censura nada.
                    for s in result["segments"]:
                        s["text"] = transcribe.censor_text(s["text"], terms)
                    transcript_json = json.dumps(
                        {"segments": result["segments"], "bleeped": ranges},
                        ensure_ascii=False)
                finally:
                    storage._unlink(wav)

        _set_status(conn, render_id, "rendering")
        style = job.get("vertical_style") or "blur"
        wm = job.get("watermark_path")
        wm_path = Path(wm) if wm else None
        if wm_path and not wm_path.is_absolute():
            wm_path = ROOT / wm_path

        out = MEDIA / "out" / f"short_{job['id']}_p{job['profile_id']}.mp4"
        renderer.render_short(
            raw, out, style=style,
            watermark=wm_path if wm_path and wm_path.exists() else None,
            hook=job.get("hook"),
            cta=bool(job.get("cta_enabled", 1)),
            audio_graph=audio_graph)

        info = renderer.probe(out)
        _set_status(conn, render_id, "done",
                    output_path=str(out.relative_to(ROOT)).replace("\\", "/"),
                    duration_s=info["duration"],
                    bleeps=bleeps,
                    transcript=transcript_json,
                    rendered_at=None)
        conn.execute("UPDATE renders SET rendered_at = datetime('now') WHERE id = ?",
                     (render_id,))
        conn.execute("UPDATE profile_queue SET status = 'rendered' WHERE id = ?", (queue_id,))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        detail = f"{exc}\n{traceback.format_exc(limit=3)}"
        try:
            _set_status(conn, render_id, "failed", error=detail[:1500])
            conn.execute("UPDATE profile_queue SET status = 'failed' WHERE id = ?", (queue_id,))
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        # El original se borra SIEMPRE, salga bien o mal el render. Antes sólo se
        # limpiaba en el camino feliz, así que cada render fallido dejaba ~20 MB
        # tirados para siempre. Si hay que re-renderizar se vuelve a descargar en
        # segundos; no vale la pena conservarlo.
        if clip_id is not None:
            storage.drop_raw(clip_id)
            storage._unlink(MEDIA / "raw" / f"a_{clip_id}.wav")
        conn.close()


def enqueue(queue_id: int) -> int:
    """Encola un render y devuelve el id de la fila en `renders`."""
    reap_stale()          # limpia zombis antes de decidir si ya hay uno en curso

    conn = db_mod.connect()
    try:
        prev = conn.execute(
            "SELECT id, status FROM renders WHERE queue_id = ? ORDER BY id DESC LIMIT 1",
            (queue_id,)).fetchone()
        # Tras el barrido, lo que siga "en curso" sí está vivo de verdad.
        if prev and prev["status"] in IN_PROGRESS:
            return prev["id"]

        cur = conn.execute(
            "INSERT INTO renders (queue_id, status, updated_at) "
            "VALUES (?, 'pending', datetime('now'))", (queue_id,))
        render_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    _pool.submit(_work, queue_id, render_id)
    return render_id


# ── RECOPILATORIOS ─────────────────────────────────────────────────────────────
def _comp_set(conn, comp_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = datetime('now')"
    conn.execute(f"UPDATE compilations SET {sets} WHERE id = ?",
                 (*fields.values(), comp_id))
    conn.commit()


def _comp_work(comp_id: int) -> None:
    conn = db_mod.connect()
    clips: list[dict] = []
    try:
        comp = conn.execute("SELECT * FROM compilations WHERE id = ?", (comp_id,)).fetchone()
        if not comp:
            return

        prof = conn.execute("SELECT * FROM profiles WHERE id = ?",
                            (comp["profile_id"],)).fetchone()
        clips = [dict(r) for r in conn.execute(
            """SELECT c.*, s.slug AS source_slug, s.platform, i.hook
               FROM compilation_items i
               JOIN clip_candidates c ON c.id = i.clip_id
               JOIN sources s ON s.id = c.source_id
               WHERE i.compilation_id = ? ORDER BY i.position""", (comp_id,))]
        if not clips:
            _comp_set(conn, comp_id, status="failed", error="El recopilatorio no tiene clips")
            return

        wm = prof["watermark_path"]
        wm_path = ROOT / wm if wm and not Path(wm).is_absolute() else (Path(wm) if wm else None)

        _comp_set(conn, comp_id, status="rendering", progress="Iniciando", error=None)
        out = MEDIA / "out" / f"comp_{comp_id}.mp4"

        res = compilation.build(
            clips,
            title=comp["title"] or prof["display_name"],
            subtitle=comp["subtitle"] or "",
            logo=wm_path if wm_path and wm_path.exists() else None,
            watermark=wm_path if wm_path and wm_path.exists() else None,
            out=out,
            on_progress=lambda m: _comp_set(conn, comp_id, progress=m),
        )

        _comp_set(conn, comp_id, status="ready",
                  output_path=str(out.relative_to(ROOT)).replace("\\", "/"),
                  duration_s=res["duration"], chapters=res["chapters_text"],
                  progress=None, rendered_at=None)
        conn.execute("UPDATE compilations SET rendered_at = datetime('now') WHERE id = ?",
                     (comp_id,))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            _comp_set(conn, comp_id, status="failed",
                      error=f"{exc}\n{traceback.format_exc(limit=3)}"[:1500], progress=None)
        except Exception:  # noqa: BLE001
            pass
    finally:
        # Los segmentos normalizados pesan tanto como el resultado final; sin esto
        # cada recopilatorio dejaría el doble de disco ocupado.
        compilation.cleanup_work()
        for c in clips:
            storage.drop_raw(c["id"])
        conn.close()


def enqueue_compilation(comp_id: int) -> None:
    conn = db_mod.connect()
    try:
        row = conn.execute("SELECT status FROM compilations WHERE id = ?",
                           (comp_id,)).fetchone()
        if row and row["status"] == "rendering":
            return
        _comp_set(conn, comp_id, status="rendering", progress="En cola")
    finally:
        conn.close()
    _pool.submit(_comp_work, comp_id)


def status(queue_id: int) -> dict | None:
    conn = db_mod.connect()
    try:
        row = conn.execute(
            "SELECT * FROM renders WHERE queue_id = ? ORDER BY id DESC LIMIT 1",
            (queue_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

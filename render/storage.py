"""Control de espacio en disco.

Cada clip deja dos archivos: la descarga original (~15-25 MB) y el render final
(~5-17 MB). A 2 videos diarios son ~1,5 GB al mes si no se limpia nada.

Política:
  - El ORIGINAL se borra apenas termina el render: ya cumplió su función. Si hace falta
    re-renderizar (por ejemplo para cambiar de estilo), se vuelve a descargar en segundos.
  - El RENDER se conserva hasta que el video se sube, porque hay que poder revisarlo antes.
    Tras subirlo se borra: la copia buena ya vive en YouTube.
  - Un barrido periódico limpia lo que quedó huérfano por renders fallidos o cancelados.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))

import db as db_mod  # noqa: E402

MEDIA = ROOT / "media"
RAW = MEDIA / "raw"
OUT = MEDIA / "out"


def _size(folder: Path) -> tuple[int, int]:
    if not folder.exists():
        return 0, 0
    files = [f for f in folder.glob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def usage() -> dict:
    raw_n, raw_b = _size(RAW)
    out_n, out_b = _size(OUT)
    return {
        "raw": {"files": raw_n, "bytes": raw_b},
        "out": {"files": out_n, "bytes": out_b},
        "total_bytes": raw_b + out_b,
        "total_mb": round((raw_b + out_b) / 1e6, 1),
    }


def _unlink(path: Path) -> int:
    """Borra un archivo y devuelve los bytes liberados."""
    try:
        if path.exists() and path.is_file():
            size = path.stat().st_size
            path.unlink()
            return size
    except OSError:
        pass
    return 0


def drop_raw(clip_id: int) -> int:
    """Borra la descarga original de un clip."""
    return _unlink(RAW / f"raw_{clip_id}.mp4")


def drop_render(output_path: str | None) -> int:
    if not output_path:
        return 0
    return _unlink(ROOT / output_path)


def cleanup_after_upload(queue_id: int) -> int:
    """Tras subir a YouTube, el render local ya no aporta nada."""
    conn = db_mod.connect()
    freed = 0
    try:
        rows = conn.execute(
            "SELECT r.output_path, q.clip_id FROM renders r "
            "JOIN profile_queue q ON q.id = r.queue_id WHERE r.queue_id = ?",
            (queue_id,)).fetchall()
        for r in rows:
            freed += drop_render(r["output_path"])
            freed += drop_raw(r["clip_id"])
        conn.execute(
            "UPDATE renders SET output_path = NULL WHERE queue_id = ?", (queue_id,))
        conn.commit()
    finally:
        conn.close()
    return freed


def sweep(raw_age_hours: float = 2, uploaded_age_days: int = 2,
          aggressive: bool = False) -> dict:
    """Barrido de mantenimiento.

    Modo normal: borra TODO lo de raw/ que pase de `raw_age_hours` (originales y audios
    de transcripción por igual) y los renders de videos ya subidos. Respeta los renders
    pendientes de revisar, que es lo único irrecuperable sin volver a procesar.

    Modo agresivo: borra además los renders que aún no se han subido. Para cuando lo
    que quieres es vaciar el disco y te da igual volver a renderizar.

    Devuelve también lo que NO borró y por qué: un "0 MB liberados" sin explicación se
    lee como que el botón está roto.
    """
    conn = db_mod.connect()
    freed = 0
    removed: list[str] = []
    skipped: list[dict] = []
    try:
        # 1) raw/ completo, no sólo raw_*.mp4: ahí caen también los .wav que deja la
        #    transcripción cuando un render falla a mitad.
        cutoff = time.time() - raw_age_hours * 3600
        for f in RAW.glob("*"):
            if not f.is_file():
                continue
            if aggressive or f.stat().st_mtime < cutoff:
                b = _unlink(f)
                if b:
                    freed += b
                    removed.append(f.name)
            else:
                edad = (time.time() - f.stat().st_mtime) / 3600
                skipped.append({"file": f.name, "mb": round(f.stat().st_size / 1e6, 1),
                                "reason": f"recién creado ({edad:.1f} h de {raw_age_hours} h)"})

        # 2) Renders de lo ya subido: la copia buena vive en YouTube.
        subidos = conn.execute(
            """SELECT r.output_path FROM renders r
               JOIN uploads u ON u.queue_id = r.queue_id
               WHERE r.output_path IS NOT NULL
                 AND u.published_at <= datetime('now', ?)""",
            (f"-{int(uploaded_age_days)} days",)).fetchall()
        for r in subidos:
            b = drop_render(r["output_path"])
            if b:
                freed += b
                removed.append(Path(r["output_path"]).name)
        conn.execute(
            """UPDATE renders SET output_path = NULL WHERE queue_id IN (
                   SELECT u.queue_id FROM uploads u
                   WHERE u.published_at <= datetime('now', ?))""",
            (f"-{int(uploaded_age_days)} days",))

        # 3) out/: en modo agresivo se va todo; si no, sólo los huérfanos sin fila en
        #    la base (sobras de pruebas por CLI) con más de una hora.
        conocidos = {Path(r["output_path"]).name for r in conn.execute(
            "SELECT output_path FROM renders WHERE output_path IS NOT NULL")}
        for f in OUT.glob("*"):
            if not f.is_file():
                continue
            huerfano = f.name not in conocidos
            if aggressive or (huerfano and f.stat().st_mtime < time.time() - 3600):
                b = _unlink(f)
                if b:
                    freed += b
                    removed.append(f.name)
            elif not huerfano:
                skipped.append({"file": f.name, "mb": round(f.stat().st_size / 1e6, 1),
                                "reason": "render sin subir todavía"})

        if aggressive:
            conn.execute("UPDATE renders SET output_path = NULL")
            conn.execute("UPDATE profile_queue SET status='new' WHERE status='rendered'")

        conn.commit()
    finally:
        conn.close()

    return {"freed_bytes": freed, "freed_mb": round(freed / 1e6, 1),
            "removed": removed, "skipped": skipped,
            "aggressive": aggressive, "usage": usage()}


if __name__ == "__main__":
    u = usage()
    print(f"originales : {u['raw']['files']:>3} archivos · {u['raw']['bytes'] / 1e6:.1f} MB")
    print(f"renders    : {u['out']['files']:>3} archivos · {u['out']['bytes'] / 1e6:.1f} MB")
    print(f"TOTAL      : {u['total_mb']} MB")
    if "--sweep" in sys.argv:
        res = sweep()
        print(f"\nliberados  : {res['freed_mb']} MB ({len(res['removed'])} archivos)")
        print(f"quedan     : {res['usage']['total_mb']} MB")

"""Ingesta de clips por CLI, con reporte de la distribución de vistas.

Uso:
    python ingest/run.py lacobraaa
    python ingest/run.py "https://kick.com/westcol/clips" brunenger zeling
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import db as db_mod
import pipeline as pipe


def report(conn, source_id: int) -> None:
    st = pipe.source_stats(conn, source_id)
    rows = conn.execute(
        "SELECT title, views, duration_s, views_per_day, created_at "
        "FROM clip_candidates WHERE source_id=? ORDER BY views DESC", (source_id,)
    ).fetchall()
    if not rows:
        print(f"  {st['slug']}: sin clips.")
        return

    views = [r["views"] for r in rows]
    bar = "=" * 72
    print(f"\n{bar}\n  {st['platform'].upper()} / {st['slug']}\n{bar}")
    print(f"  Clips guardados     : {len(rows):,}")
    print(f"  Pico histórico      : {st['peak']:,}")
    print(f"  Top del último mes  : {st['month_top']:,}")
    print(f"  Mediana de vistas   : {int(statistics.median(views)):,}")

    print(f"\n  PRODUCCIÓN DEL ÚLTIMO MES (clips de 8-90s)")
    for t in pipe.THRESHOLDS:
        n = st["per_month"][t]
        print(f"    >= {t:>5,} vistas : {n:>4} clips/mes  ->  {n / 30:.2f} videos/día")

    print(f"\n  TOP 10 POR VISTAS")
    print(f"    {'VISTAS':>9} {'V/DÍA':>8} {'DUR':>5}  {'FECHA':>10}  TÍTULO")
    for r in rows[:10]:
        print(f"    {r['views']:>9,} {r['views_per_day']:>8,.0f} {r['duration_s']:>4}s  "
              f"{(r['created_at'] or '')[:10]:>10}  {(r['title'] or '')[:36]}")
    print(f"{bar}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingesta clips de canales de Kick")
    ap.add_argument("targets", nargs="+", help="slugs o URLs de canales")
    ap.add_argument("--pages-all", type=int, default=25)
    ap.add_argument("--pages-month", type=int, default=8)
    args = ap.parse_args()

    parsed = pipe.parse_sources(" ".join(args.targets))
    if not parsed:
        ap.error("no se reconoció ningún canal")

    conn = db_mod.connect()
    try:
        for platform, slug in parsed:
            print(f"Descargando {platform}/{slug}...")
            res = pipe.ingest_source(conn, platform, slug,
                                     pages_all=args.pages_all, pages_month=args.pages_month)
            if not res["ok"]:
                print(f"  ERROR {slug}: {res['error']}")
                continue
            report(conn, res["source_id"])
    finally:
        conn.close()
    print(f"\nGuardado en {db_mod.DB_PATH}\n")


if __name__ == "__main__":
    main()

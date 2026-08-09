"""Analiza la viabilidad del canal: ¿alcanza el ritmo de clips buenos para 2 videos/día?

El backlog histórico engaña. Lo que decide si el canal aguanta la cadencia es cuántos
clips elegibles produce POR MES de forma sostenida.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import db as db_mod

MIN_D, MAX_D = 8, 90


def main() -> None:
    conn = db_mod.connect()
    rows = conn.execute(
        "SELECT c.*, s.slug FROM clip_candidates c JOIN sources s ON s.id = c.source_id"
    ).fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    bar = "=" * 72
    print(f"\n{bar}\n  VIABILIDAD DEL CANAL — ¿aguanta 2 videos/día?\n{bar}")

    # --- Sensibilidad al umbral de vistas -------------------------------------
    print("\n  SENSIBILIDAD AL UMBRAL DE VISTAS (duración 8-90s)")
    print(f"    {'umbral':>10} {'elegibles':>10} {'días a 2/día':>14}")
    for thr in (5000, 2000, 1000, 500, 300, 200, 100, 50):
        n = len([r for r in rows if r["views"] >= thr and MIN_D <= r["duration_s"] <= MAX_D])
        print(f"    {thr:>10,} {n:>10} {n / 2:>13.0f}")

    # --- Producción por mes ----------------------------------------------------
    per_month: dict[str, list] = defaultdict(list)
    for r in rows:
        if not r["created_at"]:
            continue
        try:
            dt = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        per_month[dt.strftime("%Y-%m")].append(r)

    print("\n  PRODUCCIÓN MENSUAL DE CLIPS (últimos 18 meses)")
    print(f"    {'mes':>9} {'total':>7} {'>=1k v':>8} {'>=300 v':>9} {'>=100 v':>9}")
    recent_months = sorted(per_month.keys(), reverse=True)[:18]
    tot_1k = tot_300 = tot_100 = 0
    for m in recent_months:
        items = per_month[m]
        ok = [r for r in items if MIN_D <= r["duration_s"] <= MAX_D]
        a = len([r for r in ok if r["views"] >= 1000])
        b = len([r for r in ok if r["views"] >= 300])
        c = len([r for r in ok if r["views"] >= 100])
        tot_1k += a; tot_300 += b; tot_100 += c
        print(f"    {m:>9} {len(items):>7} {a:>8} {b:>9} {c:>9}")

    n = len(recent_months) or 1
    print(f"\n  PROMEDIO MENSUAL SOSTENIBLE ({n} meses)")
    for label, total in ((">=1k vistas", tot_1k), (">=300 vistas", tot_300), (">=100 vistas", tot_100)):
        avg = total / n
        print(f"    {label:<14} {avg:5.1f} clips/mes  ->  {avg / 30:.2f} videos/día sostenibles")

    # --- Antigüedad del top ----------------------------------------------------
    top = sorted([r for r in rows if MIN_D <= r["duration_s"] <= MAX_D],
                 key=lambda r: -r["views"])[:10]
    print("\n  ANTIGÜEDAD DEL TOP 10 UTILIZABLE")
    print(f"    {'vistas':>9} {'antigüedad':>12}  título")
    for r in top:
        try:
            dt = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            age = f"{(now - dt).days} días"
        except (ValueError, AttributeError):
            age = "?"
        print(f"    {r['views']:>9,} {age:>12}  {(r['title'] or '')[:42]}")

    print(f"\n{bar}\n")


if __name__ == "__main__":
    main()

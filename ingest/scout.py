"""Evalúa canales candidatos para la whitelist.

Un canal sirve si produce clips buenos DE FORMA SOSTENIDA, no si tuvo un viral en 2024.

Método: se consulta `sort=view&time=month`, que devuelve los clips del último mes ya
ordenados por vistas. Contar cuántos superan cada umbral da directamente la producción
mensual del canal. Paginamos sólo hasta caer bajo el umbral mínimo.

NO se usa `sort=date`: devuelve los clips más recientes, que aún no acumularon vistas
(un clip de hoy tiene 0 views) y da lecturas falsas de cero.

Uso:
    python ingest/scout.py westcol brunenger zeling
    python ingest/scout.py --file canales.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import kick_client
from score import MAX_DURATION_S, MIN_DURATION_S

THRESHOLDS = (1000, 300, 100, 50)
WEEKS_PER_MONTH = 4.33


def _collect(slug: str, time_range: str, max_pages: int) -> list[dict] | None:
    """Clips del rango ordenados por vistas, cortando cuando ya no aportan."""
    out: list[dict] = []
    try:
        for clip in kick_client.iter_clips(slug, sort="view", time_range=time_range,
                                           max_pages=max_pages):
            out.append(clip)
            # Vienen ordenados desc; bajo el umbral mínimo ya no hay nada que contar.
            if (clip.get("views") or 0) < min(THRESHOLDS):
                break
    except Exception:
        return None
    return out


def evaluate(slug: str, max_pages: int) -> dict | None:
    try:
        info = kick_client.get_channel(slug)
    except Exception:
        return None

    monthly = _collect(slug, "month", max_pages)
    if monthly is None:
        return None
    weekly = _collect(slug, "week", max_pages) or []

    def count(clips: list[dict], thr: int) -> int:
        return len([c for c in clips
                    if (c.get("views") or 0) >= thr
                    and MIN_DURATION_S <= (c.get("duration") or 0) <= MAX_DURATION_S])

    followers = (info.get("followers_count") or info.get("followersCount") or 0)
    best_all = _collect(slug, "all", 1) or []
    peak = max([(c.get("views") or 0) for c in best_all], default=0)

    return {
        "slug": slug,
        "followers": followers,
        "peak_alltime": peak,
        "month": {t: count(monthly, t) for t in THRESHOLDS},
        # Proyección desde la semana: detecta canales que repuntaron hace poco.
        "week_proj": {t: count(weekly, t) * WEEKS_PER_MONTH for t in THRESHOLDS},
        "month_top": max([(c.get("views") or 0) for c in monthly], default=0),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Evalúa canales de Kick para la whitelist")
    ap.add_argument("slugs", nargs="*", help="slugs de canales de Kick")
    ap.add_argument("--file", help="archivo con un slug por línea")
    ap.add_argument("--pages", type=int, default=8, help="máx. páginas por consulta")
    ap.add_argument("--target", type=float, default=2.0, help="videos/día objetivo")
    ap.add_argument("--threshold", type=int, default=300, choices=THRESHOLDS,
                    help="umbral de vistas que consideras 'clip bueno'")
    args = ap.parse_args()

    slugs = list(args.slugs)
    if args.file:
        slugs += [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines()
                  if l.strip() and not l.startswith("#")]
    if not slugs:
        ap.error("indica al menos un slug o --file")

    print(f"Evaluando {len(slugs)} canales (sort=view, ventana: mes)...\n")
    results, dead = [], []
    for slug in slugs:
        print(f"  -> {slug:<20}", end="", flush=True)
        r = evaluate(slug, args.pages)
        if r is None:
            dead.append(slug)
            print("[no resuelve]")
        else:
            results.append(r)
            print(f"top del mes: {r['month_top']:,} vistas")

    thr = args.threshold
    results.sort(key=lambda r: -r["month"][thr])

    bar = "=" * 86
    print(f"\n{bar}\n  RANKING — clips útiles por mes ({MIN_DURATION_S}-{MAX_DURATION_S}s)\n{bar}")
    print(f"  {'canal':<20} {'pico hist.':>11} {'top mes':>9} "
          f"{'>=1k':>6} {'>=300':>7} {'>=100':>7} {'>=50':>6} {'vid/día':>8}")
    print(f"  {'-' * 82}")

    tot = {t: 0 for t in THRESHOLDS}
    for r in results:
        m = r["month"]
        for t in THRESHOLDS:
            tot[t] += m[t]
        print(f"  {r['slug']:<20} {r['peak_alltime']:>11,} {r['month_top']:>9,} "
              f"{m[1000]:>6} {m[300]:>7} {m[100]:>7} {m[50]:>6} {m[thr] / 30:>8.2f}")

    print(f"  {'-' * 82}")
    print(f"  {'AGREGADO':<20} {'':>11} {'':>9} "
          f"{tot[1000]:>6} {tot[300]:>7} {tot[100]:>7} {tot[50]:>6} {tot[thr] / 30:>8.2f}")

    have = tot[thr] / 30
    print(f"\n  Umbral de calidad: >={thr} vistas   |   Objetivo: {args.target} videos/día")
    if have >= args.target:
        print(f"  OK: la whitelist sostiene {have:.2f} videos/día.")
    else:
        print(f"  INSUFICIENTE: sostiene {have:.2f} de {args.target} videos/día.")
        aportan = [r for r in results if r["month"][thr] > 0]
        if aportan:
            media = sum(r["month"][thr] for r in aportan) / len(aportan) / 30
            print(f"  Faltan ~{(args.target - have) / media:.0f} canales del calibre de los que sí aportan.")
        print(f"  Con umbral >=100 sostendría {tot[100] / 30:.2f} videos/día.")

    if dead:
        print(f"\n  No resueltos: {', '.join(dead)}")
    print()


if __name__ == "__main__":
    main()

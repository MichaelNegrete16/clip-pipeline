"""Ingesta masiva de fuentes desde un archivo de lista.

    python ingest/seed.py seeds/streamers_es.txt

Al final imprime el ranking por producción real del último mes. Ojo con el sesgo de
"famoso": un canal enorme puede tener casi cero clips útiles hoy — westcol tiene un pico
histórico de 351k vistas y su mejor clip del último mes no llega a 600. Lo que importa
es lo que produce AHORA, y para eso está la tabla final.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import db as db_mod
import pipeline as pipe


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingesta masiva de fuentes")
    ap.add_argument("file", help="archivo con URLs/slugs, uno por línea")
    ap.add_argument("--pages-all", type=int, default=8)
    ap.add_argument("--pages-month", type=int, default=4)
    args = ap.parse_args()

    text = Path(args.file).read_text(encoding="utf-8")
    targets = pipe.parse_sources(
        " ".join(l for l in text.splitlines() if l.strip() and not l.startswith("#")))
    if not targets:
        sys.exit("No se reconoció ningún canal en el archivo")

    print(f"Ingestando {len(targets)} canales...\n")
    conn = db_mod.connect()
    ok, failed = [], []
    try:
        for i, (platform, slug) in enumerate(targets, 1):
            print(f"  [{i:>2}/{len(targets)}] {platform}/{slug:<20}", end="", flush=True)
            res = pipe.ingest_source(conn, platform, slug,
                                     pages_all=args.pages_all, pages_month=args.pages_month,
                                     windows=("all", "month", "week"))
            if res["ok"]:
                st = pipe.source_stats(conn, res["source_id"])
                ok.append(st)
                print(f"{res['clips']:>5} clips · top mes {st['month_top']:>7,}")
            else:
                failed.append((f"{platform}/{slug}", res["error"][:60]))
                print("  --")

        print(f"\n{'=' * 86}")
        print("  RANKING POR PRODUCCIÓN REAL DEL ÚLTIMO MES")
        print(f"{'=' * 86}")
        print(f"  {'canal':<24} {'plat':<7} {'pico hist.':>11} {'top mes':>9} "
              f"{'>=1k':>6} {'>=300':>7} {'>=100':>7}")
        print(f"  {'-' * 82}")

        ok.sort(key=lambda s: -s["per_month"][300])
        tot = {t: 0 for t in pipe.THRESHOLDS}
        for s in ok:
            for t in pipe.THRESHOLDS:
                tot[t] += s["per_month"][t]
            print(f"  {s['slug']:<24} {s['platform']:<7} {s['peak']:>11,} "
                  f"{s['month_top']:>9,} {s['per_month'][1000]:>6} "
                  f"{s['per_month'][300]:>7} {s['per_month'][100]:>7}")

        print(f"  {'-' * 82}")
        print(f"  {'AGREGADO':<32} {'':>11} {'':>9} "
              f"{tot[1000]:>6} {tot[300]:>7} {tot[100]:>7}")
        print(f"\n  Videos/día sostenibles:  {tot[1000]/30:>5.2f} (umbral 1k) | "
              f"{tot[300]/30:>5.2f} (umbral 300) | {tot[100]/30:>5.2f} (umbral 100)")

        aportan = len([s for s in ok if s["per_month"][300] > 0])
        print(f"  Canales que aportan algo: {aportan} de {len(ok)} resueltos")

        if failed:
            print(f"\n  No resueltos ({len(failed)}):")
            for name, err in failed:
                print(f"    {name:<28} {err}")
        print()
    finally:
        conn.close()


if __name__ == "__main__":
    main()

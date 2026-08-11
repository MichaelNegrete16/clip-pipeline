"""Descarga y render de clips: 9:16 vertical con marca de agua.

Descarga con yt-dlp (habla HLS de Kick y clips de Twitch por igual) y transforma con
ffmpeg. Remotion entra después, para lo que aporte de verdad: subtítulos animados,
intro/outro y overlays con movimiento. Para un crop y una marca de agua estática,
ffmpeg hace lo mismo en segundos y sin costo de render.

Uso:
    python render/renderer.py <clip_id> [--style blur|crop] [--watermark media/logo.png]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config  # noqa: E402
import db as db_mod  # noqa: E402
import twitch_client  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overlays  # noqa: E402

MEDIA = ROOT / "media"
SHORT_W, SHORT_H = 1080, 1920      # 9:16 vertical
VIDEO_W, VIDEO_H = 1920, 1080      # 16:9 horizontal


class RenderError(RuntimeError):
    pass


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise RenderError(f"{what} falló:\n  " + "\n  ".join(tail))


def download_clip(clip: dict, dest_dir: Path) -> Path:
    """Baja el clip. Twitch va por su GQL; Kick por su HLS con yt-dlp."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    source_url = clip.get("clip_page_url") or clip.get("video_url")
    if not source_url:
        raise RenderError(f"El clip {clip['id']} no tiene URL descargable")

    out = dest_dir / f"raw_{clip['id']}.mp4"
    if out.exists():
        return out

    # Twitch: descarga propia. El extractor de yt-dlp se rompe cada vez que Twitch
    # toca su GraphQL, y esperar el parche deja el pipeline parado. Si nuestra vía
    # fallara, se cae al camino de yt-dlp de todas formas.
    if clip.get("platform") == "twitch":
        try:
            return twitch_client.download_clip_file(source_url, out)
        except Exception as exc:  # noqa: BLE001
            print(f"      descarga directa falló ({exc}); probando con yt-dlp")

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--quiet", "--no-warnings",
        "--ffmpeg-location", str(Path(config.ffmpeg()).parent),
        "--merge-output-format", "mp4",
        "-o", str(out),
        source_url,
    ]
    _run(cmd, "Descarga con yt-dlp")
    if not out.exists():
        raise RenderError(f"yt-dlp terminó sin error pero no generó {out}")
    return out


def probe(path: Path) -> dict:
    """Dimensiones y duración reales del archivo."""
    proc = subprocess.run(
        [config.ffprobe(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,duration",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True)
    info: dict = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            if v not in ("N/A", ""):
                info.setdefault(k, v)
    return {"width": int(info.get("width", 0)), "height": int(info.get("height", 0)),
            "duration": float(info.get("duration", 0) or 0)}


# Relación de aspecto del video en primer plano dentro del modo 'blur'.
# Un 16:9 (1.78) a todo el ancho ocupa sólo el 32% de la altura del Short: se ve más
# desenfoque que contenido. Recortando los lados hasta 1.45 sube al ~39% y la
# composición deja de verse vacía, perdiendo apenas un 19% del ancho original.
BLUR_FG_ASPECT = 1.45

# El bloque de video va algo por encima del centro: deja aire arriba para el enganche
# y abajo para los botones, que van al 76% de altura.
BLUR_FG_RISE = 70


def _vertical_filter(style: str) -> str:
    """Filtro de conversión a 9:16.

    'crop' recorta al centro: llena la pantalla, ideal para facecam donde la cara ya
    está centrada, pero pierde los bordes.
    'blur' mete el video sobre un fondo borroso: conserva casi toda la imagen, que es
    lo seguro cuando no sabes dónde está la acción (gameplay, varias ventanas).
    """
    if style == "crop":
        # force_original_aspect_ratio=increase escala hasta CUBRIR el lienzo y luego
        # recorta al centro. Escalar por el ancho (scale=1080:-2) daría 1080x608 en un
        # 16:9 y el recorte de 1920 de alto sería imposible.
        return (f"scale={SHORT_W}:{SHORT_H}:force_original_aspect_ratio=increase,"
                f"crop={SHORT_W}:{SHORT_H},setsar=1")

    # min(): si el material ya es más estrecho que el objetivo, no se recorta nada.
    fg_crop = rf"crop=min(iw\,ih*{BLUR_FG_ASPECT}):ih"
    return (
        f"split=2[bg][fg];"
        f"[bg]scale={SHORT_W}:{SHORT_H}:force_original_aspect_ratio=increase,"
        f"crop={SHORT_W}:{SHORT_H},boxblur=28:3,eq=brightness=-0.06[bgb];"
        f"[fg]{fg_crop},scale={SHORT_W}:-2[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2-{BLUR_FG_RISE},setsar=1"
    )


def render_short(src: Path, out: Path, *, style: str = "blur",
                 watermark: Path | None = None, wm_scale: float = 0.18,
                 wm_margin: int = 42, wm_opacity: float = 0.85,
                 hook: str | None = None, cta: bool = True,
                 cta_from: float = 1.5, audio_graph: str | None = None) -> Path:
    """Convierte a Short vertical 1080x1920 con marca de agua, enganche y botones.

    Los overlays se generan como PNG del tamaño exacto del lienzo, así que se pegan en
    0:0 y cada uno ya trae su posición dibujada dentro (zona segura de Shorts).
    `cta_from` retrasa un poco los botones: aparecer en el frame 0 se siente a anuncio.
    """
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [config.ffmpeg(), "-y", "-i", str(src)]
    parts = [f"[0:v]{_vertical_filter(style)}[base]"]
    last, idx = "base", 1

    if watermark and Path(watermark).exists():
        cmd += ["-i", str(watermark)]
        # La marca se escala respecto al ancho del lienzo, no a su tamaño original,
        # para que se vea igual con cualquier PNG que le pongas.
        parts.append(f"[{idx}:v]scale={int(SHORT_W * wm_scale)}:-1,"
                     f"format=rgba,colorchannelmixer=aa={wm_opacity}[wm]")
        parts.append(f"[{last}][wm]overlay=W-w-{wm_margin}:{wm_margin}[v{idx}]")
        last, idx = f"v{idx}", idx + 1

    hook_png = overlays.make_hook(hook) if hook else None
    if hook_png:
        cmd += ["-i", str(hook_png)]
        parts.append(f"[{last}][{idx}:v]overlay=0:0[v{idx}]")
        last, idx = f"v{idx}", idx + 1

    if cta:
        cmd += ["-i", str(overlays.make_cta())]
        # enable: los botones entran después del arranque, no desde el frame 0.
        parts.append(f"[{last}][{idx}:v]overlay=0:0:enable='gte(t,{cta_from})'[v{idx}]")
        last, idx = f"v{idx}", idx + 1

    # El grafo de censura viaja en el MISMO filter_complex que el video: ffmpeg no
    # admite -filter_complex y -af a la vez sobre la misma entrada.
    if audio_graph:
        parts.append(audio_graph)

    if idx > 1 or audio_graph:
        amap = ["-map", "[aout]"] if audio_graph else ["-map", "0:a?"]
        cmd += ["-filter_complex", ";".join(parts), "-map", f"[{last}]", *amap]
    else:
        cmd += ["-vf", _vertical_filter(style), "-map", "0:v", "-map", "0:a?"]

    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-r", "30", "-g", "60",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out),
    ]
    _run(cmd, "Render vertical")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Descarga y renderiza un clip a Short 9:16")
    ap.add_argument("clip_id", type=int)
    ap.add_argument("--style", default="blur", choices=["blur", "crop"])
    ap.add_argument("--watermark", default=None)
    ap.add_argument("--hook", default=None, help="texto de enganche arriba")
    ap.add_argument("--no-cta", action="store_true", help="sin botones de suscribirse")
    args = ap.parse_args()

    conn = db_mod.connect()
    row = conn.execute(
        "SELECT c.*, s.slug AS source_slug, s.platform FROM clip_candidates c "
        "JOIN sources s ON s.id = c.source_id WHERE c.id = ?", (args.clip_id,)).fetchone()
    conn.close()
    if not row:
        sys.exit(f"No existe el clip id={args.clip_id}")

    clip = dict(row)
    print(f"Clip: [{clip['platform']}] {clip['source_slug']} — {clip['title']}")
    print(f"      {clip['views']:,} vistas · {clip['duration_s']}s")

    print("Descargando...")
    raw = download_clip(clip, MEDIA / "raw")
    info = probe(raw)
    print(f"      {raw.name} · {info['width']}x{info['height']} · {info['duration']:.1f}s "
          f"· {raw.stat().st_size / 1e6:.1f} MB")

    wm = Path(args.watermark) if args.watermark else None
    out = MEDIA / "out" / f"short_{clip['id']}.mp4"
    extras = []
    if wm and wm.exists():
        extras.append("marca de agua")
    if args.hook:
        extras.append("enganche")
    if not args.no_cta:
        extras.append("botones")
    print(f"Renderizando 9:16 (estilo {args.style}"
          f"{' + ' + ', '.join(extras) if extras else ''})...")
    render_short(raw, out, style=args.style, watermark=wm,
                 hook=args.hook, cta=not args.no_cta)

    fin = probe(out)
    print(f"LISTO: {out}")
    print(f"      {fin['width']}x{fin['height']} · {fin['duration']:.1f}s "
          f"· {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()

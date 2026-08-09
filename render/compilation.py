"""Armado de recopilatorios horizontales para la sección de videos de YouTube.

Pegar clips de fuentes distintas no es concatenar archivos: vienen en 1920x1080 y
2560x1440, a 30 y 60 fps, con audio a distintos niveles y códecs. Si se concatenan tal
cual, ffmpeg produce basura o directamente falla.

El proceso es en tres pasos:

  1. NORMALIZAR cada clip por separado -> 1920x1080, 30 fps, AAC 48 kHz estéreo, y sobre
     todo `loudnorm`: sin eso el espectador va subiendo y bajando el volumen todo el
     video, porque cada streamer graba a un nivel distinto. Es el detalle que más
     separa un recopilatorio decente de uno amateur.
  2. INTRO con portada de marca.
  3. CONCATENAR con el demuxer y `-c copy`. Como todos los segmentos ya comparten
     parámetros exactos, no hay que recodificar: es instantáneo y sin pérdida.

De paso se calculan los capítulos de YouTube, que salen gratis porque ya conocemos la
duración de cada tramo.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config    # noqa: E402
import overlays  # noqa: E402
import renderer  # noqa: E402

MEDIA = ROOT / "media"
WORK = MEDIA / "comp"

VW, VH, FPS = 1920, 1080, 30
INTRO_S = 4.0
LOWER_THIRD_S = 5.0        # cuánto se muestra el nombre del streamer en cada clip

# loudnorm a -16 LUFS: es el objetivo que usa YouTube, así que el reproductor no
# vuelve a tocar el volumen al publicar.
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


class CompilationError(RuntimeError):
    pass


def _run(cmd: list[str], what: str) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if p.returncode != 0:
        cola = (p.stderr or "").strip().splitlines()[-6:]
        raise CompilationError(f"{what} falló:\n  " + "\n  ".join(cola))


def make_intro(title: str, subtitle: str, logo: Path | None, out: Path) -> Path:
    """Segmento de intro a partir de una portada estática, con audio en silencio.

    El audio silencioso NO es opcional: si un segmento no tiene pista de audio, el
    concat descuadra el resto del video.
    """
    card = overlays.make_title_card(title, subtitle, logo, WORK / "intro_card.png")
    frames = int(INTRO_S * FPS)
    cmd = [
        config.ffmpeg(), "-y",
        # -framerate fija cuántos frames entran; sin esto entran 25/s y zoompan los
        # multiplica. El parámetro `d` de zoompan son frames de SALIDA por cada frame
        # de ENTRADA: con d=1 la relación es uno a uno y la duración sale exacta.
        "-loop", "1", "-framerate", str(FPS), "-t", str(INTRO_S), "-i", str(card),
        "-f", "lavfi", "-t", str(INTRO_S),
        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        # El zoom lentísimo evita que la intro se sienta una imagen congelada.
        "-vf", f"scale={int(VW * 1.08)}:-2,"
               f"zoompan=z='min(1+0.0006*in,1.06)':d=1:s={VW}x{VH}:fps={FPS},"
               f"trim=end_frame={frames},fade=in:0:12,setsar=1",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        str(out),
    ]
    _run(cmd, "Render de la intro")
    return out


def normalize_clip(src: Path, out: Path, *, streamer: str = "", platform: str = "",
                   watermark: Path | None = None) -> Path:
    """Deja un clip listo para concatenar: mismo formato, mismo volumen, con rótulo."""
    inputs = [config.ffmpeg(), "-y", "-i", str(src)]
    partes = [
        f"[0:v]scale={VW}:{VH}:force_original_aspect_ratio=decrease,"
        f"pad={VW}:{VH}:(ow-iw)/2:(oh-ih)/2:color=black,fps={FPS},setsar=1[base]"
    ]
    ultimo, idx = "base", 1

    if streamer:
        rotulo = overlays.make_lower_third(streamer, platform,
                                           WORK / f"lt_{streamer}.png")
        inputs += ["-i", str(rotulo)]
        # Sólo los primeros segundos: dejarlo todo el clip cansa la vista.
        partes.append(f"[{ultimo}][{idx}:v]overlay=0:0:"
                      f"enable='lt(t,{LOWER_THIRD_S})'[v{idx}]")
        ultimo, idx = f"v{idx}", idx + 1

    if watermark and Path(watermark).exists():
        inputs += ["-i", str(watermark)]
        partes.append(f"[{idx}:v]scale={int(VW*0.09)}:-1,format=rgba,"
                      f"colorchannelmixer=aa=0.8[wm]")
        partes.append(f"[{ultimo}][wm]overlay=W-w-36:36[v{idx}]")
        ultimo, idx = f"v{idx}", idx + 1

    # anull garantiza pista de audio aunque el clip venga mudo.
    partes.append(f"[0:a]{LOUDNORM},aresample=48000[a]")

    cmd = inputs + [
        "-filter_complex", ";".join(partes),
        "-map", f"[{ultimo}]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", str(FPS), "-g", str(FPS * 2),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        str(out),
    ]
    _run(cmd, f"Normalización de {src.name}")
    return out


def concat(segments: list[Path], out: Path) -> Path:
    """Une los segmentos sin recodificar: todos comparten ya los mismos parámetros."""
    lista = WORK / "concat.txt"
    lista.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in segments), encoding="utf-8")

    cmd = [config.ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(lista),
           "-c", "copy", "-movflags", "+faststart", str(out)]
    _run(cmd, "Concatenación")
    return out


def format_chapters(entries: list[dict]) -> str:
    """Capítulos para la descripción de YouTube.

    Reglas de YouTube: el primero debe ser 00:00, hacen falta al menos 3 y cada uno
    debe durar 10 segundos o más. Si no se cumplen, YouTube los ignora en silencio.
    """
    if len(entries) < 3:
        return ""

    lineas = []
    for e in entries:
        s = int(e["start"])
        marca = f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 \
            else f"{s // 60}:{s % 60:02d}"
        lineas.append(f"{marca} {e['label']}")
    return "\n".join(lineas)


def build(clips: list[dict], *, title: str, subtitle: str = "",
          logo: Path | None = None, watermark: Path | None = None,
          out: Path | None = None, on_progress=None) -> dict:
    """Arma el recopilatorio completo. `clips` son filas con video ya descargable."""
    WORK.mkdir(parents=True, exist_ok=True)
    out = out or (MEDIA / "out" / "compilacion.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    segmentos: list[Path] = []
    capitulos: list[dict] = []
    t = 0.0

    def avisar(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    avisar("Armando la intro")
    intro = make_intro(title, subtitle, logo, WORK / "seg_000_intro.mp4")
    segmentos.append(intro)
    capitulos.append({"start": 0.0, "label": "Intro"})
    t += renderer.probe(intro)["duration"]

    for i, clip in enumerate(clips, 1):
        avisar(f"Clip {i} de {len(clips)}: descargando")
        raw = renderer.download_clip(clip, MEDIA / "raw")

        avisar(f"Clip {i} de {len(clips)}: normalizando audio y formato")
        seg = normalize_clip(raw, WORK / f"seg_{i:03d}.mp4",
                             streamer=clip.get("source_slug", ""),
                             platform=(clip.get("platform") or "").capitalize(),
                             watermark=watermark)
        segmentos.append(seg)

        etiqueta = (clip.get("hook") or clip.get("title") or "Momento").strip()
        capitulos.append({"start": t, "label": f"{clip.get('source_slug','')} — {etiqueta}"[:95]})
        t += renderer.probe(seg)["duration"]

    avisar("Uniendo todo")
    concat(segmentos, out)

    info = renderer.probe(out)
    return {
        "path": out,
        "duration": info["duration"],
        "clips": len(clips),
        "chapters": capitulos,
        "chapters_text": format_chapters(capitulos),
        "segments": [str(s) for s in segmentos],
    }


def cleanup_work() -> int:
    """Borra los segmentos intermedios: pesan tanto como el resultado final."""
    liberado = 0
    if WORK.exists():
        for f in WORK.glob("*"):
            if f.is_file():
                liberado += f.stat().st_size
                try:
                    f.unlink()
                except OSError:
                    pass
    return liberado

"""Genera la marca de agua a partir del avatar del canal de YouTube conectado.

La idea: que la marca viaje con el video. Si alguien descarga el Short y lo resube, tu
logo sigue ahí. El avatar de YouTube es cuadrado, así que lo recortamos en círculo (que
es como se ve en la plataforma) y le ponemos un borde y una sombra suave para que se
lea sobre cualquier fondo, claro u oscuro.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from PIL import Image, ImageDraw, ImageFilter

import overlays

ROOT = Path(__file__).resolve().parent.parent
LOGOS = ROOT / "media" / "logos"

SIZE = 320            # lado del avatar ya recortado
RING = 10             # grosor del borde blanco
PAD = 26              # espacio para la sombra


def _circle_mask(size: int) -> Image.Image:
    """Máscara circular suavizada (se dibuja 4x y se reduce, para bordes sin dientes)."""
    big = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    return big.resize((size, size), Image.LANCZOS)


def build_watermark(avatar_url: str, out_path: Path, *, label: str | None = None) -> Path:
    """Descarga el avatar y arma un PNG transparente listo para el overlay de ffmpeg."""
    resp = requests.get(avatar_url, timeout=30)
    resp.raise_for_status()
    avatar = Image.open(io.BytesIO(resp.content)).convert("RGBA")

    # Recorte cuadrado central antes de circular: los avatares no siempre vienen 1:1.
    side = min(avatar.size)
    left = (avatar.width - side) // 2
    top = (avatar.height - side) // 2
    avatar = avatar.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), Image.LANCZOS)

    mask = _circle_mask(SIZE)
    circular = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    circular.paste(avatar, (0, 0), mask)

    total = SIZE + RING * 2 + PAD * 2
    canvas = Image.new("RGBA", (total, total), (0, 0, 0, 0))

    # Sombra: da contraste cuando el video de fondo es claro.
    shadow = Image.new("RGBA", (total, total), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        (PAD - 2, PAD - 2, total - PAD + 2, total - PAD + 2), fill=(0, 0, 0, 130))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(9)))

    # Anillo blanco: separa el logo del fondo cuando el video es oscuro.
    ImageDraw.Draw(canvas).ellipse(
        (PAD, PAD, total - PAD - 1, total - PAD - 1), fill=(255, 255, 255, 235))

    canvas.alpha_composite(circular, (PAD + RING, PAD + RING))

    if label:
        canvas = _add_label(canvas, label)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return out_path


def _add_label(canvas: Image.Image, label: str) -> Image.Image:
    """Añade el nombre del canal debajo del círculo."""
    font = overlays._font(46, bold=True)

    tmp = ImageDraw.Draw(canvas)
    box = tmp.textbbox((0, 0), label, font=font, stroke_width=5)
    tw, th = box[2] - box[0], box[3] - box[1]

    width = max(canvas.width, tw + 40)
    out = Image.new("RGBA", (width, canvas.height + th + 26), (0, 0, 0, 0))
    out.alpha_composite(canvas, ((width - canvas.width) // 2, 0))

    ImageDraw.Draw(out).text(
        ((width - tw) // 2 - box[0], canvas.height + 8), label, font=font,
        fill=(255, 255, 255, 255), stroke_width=5, stroke_fill=(0, 0, 0, 200))
    return out


def watermark_for_profile(profile_slug: str, avatar_url: str,
                          label: str | None = None) -> str:
    """Genera la marca del canal y devuelve la ruta relativa para guardar en la BD."""
    out = LOGOS / f"{profile_slug}.png"
    build_watermark(avatar_url, out, label=label)
    return str(out.relative_to(ROOT)).replace("\\", "/")

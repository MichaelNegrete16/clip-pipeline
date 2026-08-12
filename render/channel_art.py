"""Genera el avatar y el banner del canal de YouTube.

El avatar se ve casi siempre a 32-48 píxeles en el feed de Shorts, así que manda una
regla por encima de todo: TRES LETRAS GRANDES Y MUCHO CONTRASTE. Cualquier detalle fino,
degradado suave o texto secundario desaparece a ese tamaño y sólo ensucia.

El banner tiene una zona segura de 1235x338 en el centro: es lo único que se ve en móvil.
Todo lo importante va ahí; los bordes sólo los ve quien entre desde televisor.

    python render/channel_art.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import overlays  # noqa: E402

OUT = ROOT / "media" / "canal"

# Los mismos colores del panel: la marca es una sola en todas partes.
AZUL = (91, 140, 255)
MORADO = (139, 91, 255)
FONDO = (10, 12, 17)
BLANCO = (255, 255, 255)


def _degradado(size: tuple[int, int], a: tuple, b: tuple) -> Image.Image:
    """Degradado diagonal de a -> b."""
    w, h = size
    base = Image.new("RGB", (w, h), a)
    capa = Image.new("RGB", (w, h), b)
    mascara = Image.new("L", (w, h))
    px = mascara.load()
    for y in range(h):
        for x in range(0, w, 4):          # de 4 en 4: el degradado no necesita más
            v = int(255 * (x / w * 0.65 + y / h * 0.35))
            for dx in range(4):
                if x + dx < w:
                    px[x + dx, y] = v
    base.paste(capa, (0, 0), mascara)
    return base


def avatar(texto: str = "AFK", out: Path | None = None, size: int = 800) -> Path:
    """Avatar cuadrado. YouTube lo recorta en círculo, así que el margen importa."""
    out = out or (OUT / "avatar.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    img = _degradado((size, size), AZUL, MORADO).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # Aro interior: da un borde visible cuando el avatar cae sobre fondo claro.
    m = int(size * 0.055)
    draw.ellipse((m, m, size - m, size - m), outline=(255, 255, 255, 60),
                 width=int(size * 0.014))

    # El texto ocupa el máximo posible dentro del círculo seguro.
    objetivo = int(size * 0.62)
    tam = int(size * 0.42)
    for tam in range(int(size * 0.5), int(size * 0.18), -6):
        f = overlays._font(tam, bold=True)
        caja = draw.textbbox((0, 0), texto, font=f)
        if caja[2] - caja[0] <= objetivo:
            break
    f = overlays._font(tam, bold=True)
    caja = draw.textbbox((0, 0), texto, font=f)
    tw, th = caja[2] - caja[0], caja[3] - caja[1]

    # Sombra proyectada: separa las letras del degradado sin bajar el contraste.
    sombra = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(sombra).text(((size - tw) / 2 - caja[0], (size - th) / 2 - caja[1] + 6),
                                texto, font=f, fill=(0, 0, 0, 130))
    img.alpha_composite(sombra.filter(ImageFilter.GaussianBlur(14)))

    draw.text(((size - tw) / 2 - caja[0], (size - th) / 2 - caja[1]), texto, font=f,
              fill=BLANCO)

    img.convert("RGB").save(out, quality=95)
    return out


def banner(nombre: str = "ClipsAfk",
           lema: str = "Los mejores momentos del streaming en español",
           pie: str = "2 clips nuevos cada día  ·  09:00 y 18:00",
           out: Path | None = None) -> Path:
    """Banner 2048x1152 con todo lo legible dentro de la zona segura central."""
    out = out or (OUT / "banner.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    W, H = 2048, 1152
    SW, SH = 1235, 338                      # zona segura que se ve en móvil

    img = Image.new("RGB", (W, H), FONDO)
    draw = ImageDraw.Draw(img)

    # Resplandor detrás del centro: da profundidad sin robar contraste al texto.
    glow = Image.new("RGB", (W, H), FONDO)
    ImageDraw.Draw(glow).ellipse((W // 2 - 760, H // 2 - 420, W // 2 + 760, H // 2 + 420),
                                 fill=(38, 56, 112))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(220)), 0.85)
    draw = ImageDraw.Draw(img)

    x0, y0 = (W - SW) // 2, (H - SH) // 2
    cx = W // 2

    f_nombre = overlays._font(112, bold=True)
    f_lema = overlays._font(46, bold=False)
    f_pie = overlays._font(38, bold=True)

    def centrado(texto, font, y, fill):
        caja = draw.textbbox((0, 0), texto, font=font)
        draw.text((cx - (caja[2] - caja[0]) / 2 - caja[0], y), texto, font=font, fill=fill)
        return caja[3] - caja[1]

    y = y0 + 34
    y += centrado(nombre, f_nombre, y, BLANCO) + 40
    y += centrado(lema, f_lema, y, (154, 166, 184)) + 44

    # El pie va en una cápsula: es la promesa concreta y tiene que destacar.
    caja = draw.textbbox((0, 0), pie, font=f_pie)
    pw, ph = caja[2] - caja[0], caja[3] - caja[1]
    px, py = cx - pw / 2 - 30, y - 12
    draw.rounded_rectangle((px, py, px + pw + 60, py + ph + 34), radius=(ph + 34) // 2,
                           fill=AZUL)
    draw.text((cx - pw / 2 - caja[0], y + 4), pie, font=f_pie, fill=(4, 16, 31))

    img.save(out, quality=95)
    return out


if __name__ == "__main__":
    a = avatar()
    b = banner()
    for f in (a, b):
        im = Image.open(f)
        print(f"  {f.relative_to(ROOT)}  {im.width}x{im.height}  "
              f"{f.stat().st_size / 1000:.0f} KB")

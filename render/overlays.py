"""Genera los overlays del Short: texto de enganche y botones de suscribirse/compartir.

Se dibujan con PIL en PNG transparente y luego ffmpeg los superpone. Hacerlo con
drawtext de ffmpeg sería un infierno de escapado (comillas, acentos, saltos de línea)
y no permite ajustar el texto a varias líneas con control fino.

ZONA SEGURA — importante: YouTube Shorts dibuja SU interfaz encima del video. Abajo van
el título, el canal y la descripción; a la derecha los botones de like y comentarios.
Todo lo que pongamos ahí queda tapado. Por eso:
    - el enganche arriba, al 8% de altura (no pegado al borde, que algunos móviles cortan)
    - los botones al 76% (levantados sobre la franja que ocupa YouTube)
    - nada más allá del 88% de ancho, para no chocar con la columna de botones
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "media" / "overlays"

W, H = 1080, 1920
# 13,5%: por debajo de la marca de agua de la esquina, que ocupa el borde superior.
SAFE_TOP = int(H * 0.135)
SAFE_CTA = int(H * 0.76)

RED = (255, 0, 45, 255)
DARK = (18, 18, 22, 225)


# Fuentes por sistema. Sin esto, en Linux PIL cae a load_default(), que es un mapa de
# bits diminuto: los enganches y rótulos saldrían ilegibles en la VPS.
FONT_CANDIDATES = {
    True: [   # negrita
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
    False: [  # regular
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
}

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    clave = (size, bold)
    if clave in _font_cache:
        return _font_cache[clave]

    for path in FONT_CANDIDATES[bold]:
        if Path(path).exists():
            try:
                f = _font_cache[clave] = ImageFont.truetype(path, size)
                return f
            except OSError:
                continue

    raise RuntimeError(
        "No se encontró ninguna fuente TrueType utilizable. En Linux instala:\n"
        "  sudo apt install fonts-dejavu-core\n"
        "o añade la ruta de tu fuente a FONT_CANDIDATES en render/overlays.py"
    )


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        probe = f"{cur} {w}".strip()
        if draw.textlength(probe, font=font) <= max_w or not cur:
            cur = probe
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make_hook(text: str, out: Path | None = None) -> Path | None:
    """Texto de enganche arriba: blanco, grande, con borde y sombra para leerse sobre todo."""
    text = (text or "").strip()
    if not text:
        return None

    out = out or (TMP / "hook.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Se reduce el tamaño hasta que quepa en 3 líneas: un enganche más largo que eso
    # no lo lee nadie en un Short.
    size, max_w = 82, int(W * 0.86)
    for size in range(82, 41, -6):
        font = _font(size)
        lines = _wrap(draw, text, font, max_w)
        if len(lines) <= 3:
            break
    font = _font(size)
    lines = _wrap(draw, text, font, max_w)[:3]

    line_h = int(size * 1.22)
    total_h = line_h * len(lines)
    y = SAFE_TOP

    # Sombra difusa detrás: da contraste sobre fondos claros sin tapar la imagen.
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (int(W * 0.04), y - 26, int(W * 0.96), y + total_h + 22),
        radius=28, fill=(0, 0, 0, 115))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))

    for i, line in enumerate(lines):
        tw = draw.textlength(line, font=font)
        draw.text(((W - tw) / 2, y + i * line_h), line, font=font,
                  fill=(255, 255, 255, 255), stroke_width=7, stroke_fill=(0, 0, 0, 235))

    img.save(out)
    return out


# ── FORMATO HORIZONTAL (recopilatorios) ────────────────────────────────────────
VW, VH = 1920, 1080


def make_title_card(title: str, subtitle: str = "", logo: Path | None = None,
                    out: Path | None = None) -> Path:
    """Portada de la intro del recopilatorio: fondo oscuro, logo, título y subtítulo."""
    out = out or (TMP / "intro_card.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGBA", (VW, VH), (13, 16, 23, 255))
    draw = ImageDraw.Draw(img)

    # Viñeta radial simple: dos rectángulos difuminados dan sensación de foco central.
    glow = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((VW // 2 - 620, VH // 2 - 420, VW // 2 + 620, VH // 2 + 420),
                                 fill=(40, 70, 130, 90))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(160)))

    y = 190
    if logo and Path(logo).exists():
        mark = Image.open(logo).convert("RGBA")
        scale = 300 / max(mark.size)
        mark = mark.resize((int(mark.width * scale), int(mark.height * scale)), Image.LANCZOS)
        img.alpha_composite(mark, ((VW - mark.width) // 2, y))
        y += mark.height + 46

    font = _font(96)
    lines = _wrap(draw, title, font, int(VW * 0.82))[:2]
    for i, line in enumerate(lines):
        tw = draw.textlength(line, font=font)
        draw.text(((VW - tw) / 2, y + i * 116), line, font=font,
                  fill=(255, 255, 255, 255), stroke_width=6, stroke_fill=(0, 0, 0, 210))
    y += len(lines) * 116 + 20

    if subtitle:
        sf = _font(52, bold=False)
        tw = draw.textlength(subtitle, font=sf)
        draw.text(((VW - tw) / 2, y), subtitle, font=sf, fill=(150, 165, 185, 255))

    img.convert("RGB").save(out)
    return out


def make_lower_third(name: str, platform: str = "", out: Path | None = None) -> Path:
    """Rótulo con el nombre del streamer, abajo a la izquierda.

    No es decoración: da crédito visible a la fuente en cada clip del recopilatorio.
    """
    out = out or (TMP / f"lower_{name.replace('/', '_')}.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font = _font(46)
    sub = _font(30, bold=False)
    text = name
    tw = int(draw.textlength(text, font=font))
    sw = int(draw.textlength(platform, font=sub)) if platform else 0

    # 118 de alto, no 96: con dos líneas (nombre a 46 px y plataforma a 30 px) más los
    # descendentes de la tipografía, la caja anterior recortaba la segunda línea.
    pad, h = 34, 118
    w = max(tw, sw) + pad * 2
    x, y = 70, VH - 200

    shadow = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((x - 4, y - 4, x + w + 4, y + h + 4),
                                             radius=16, fill=(0, 0, 0, 150))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))

    draw.rounded_rectangle((x, y, x + w, y + h), radius=14, fill=(18, 18, 22, 220))
    draw.rectangle((x, y, x + 7, y + h), fill=(255, 0, 45, 255))   # acento lateral
    draw.text((x + pad, y + (20 if platform else 32)), text, font=font,
              fill=(255, 255, 255, 255))
    if platform:
        draw.text((x + pad, y + 72), platform, font=sub, fill=(150, 165, 185, 255))

    img.save(out)
    return out


def _share_arrow(draw: ImageDraw.ImageDraw, cx: int, cy: int, s: int) -> None:
    """Flecha de compartir dibujada a mano: no dependemos de que la fuente traiga el glifo."""
    draw.line([(cx - s, cy + s // 2), (cx + s // 2, cy - s // 2)],
              fill=(255, 255, 255, 255), width=max(3, s // 5))
    draw.polygon([(cx + s, cy - s), (cx + s, cy), (cx, cy - s)],
                 fill=(255, 255, 255, 255))


def _bell(draw: ImageDraw.ImageDraw, cx: int, cy: int, s: int) -> None:
    """Campanita de notificaciones."""
    draw.pieslice((cx - s, cy - s, cx + s, cy + s), 180, 360, fill=(255, 255, 255, 255))
    draw.rectangle((cx - s, cy - 2, cx + s, cy + int(s * 0.45)), fill=(255, 255, 255, 255))
    draw.rectangle((cx - int(s * 1.25), cy + int(s * 0.45),
                    cx + int(s * 1.25), cy + int(s * 0.72)), fill=(255, 255, 255, 255))
    draw.ellipse((cx - int(s * 0.32), cy + int(s * 0.72),
                  cx + int(s * 0.32), cy + int(s * 1.3)), fill=(255, 255, 255, 255))


def make_cta(out: Path | None = None, *, subscribe_text: str = "SUSCRÍBETE",
             share_text: str = "COMPARTE") -> Path:
    """Botones de suscribirse y compartir, en la zona que YouTube no tapa."""
    out = out or (TMP / "cta.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font(44)

    pill_h, gap = 104, 26
    pad_x, icon_gap = 40, 20

    sub_w = int(draw.textlength(subscribe_text, font=font)) + pad_x * 2 + 70
    shr_w = int(draw.textlength(share_text, font=font)) + pad_x * 2 + 62
    total = sub_w + gap + shr_w
    x = (W - total) // 2
    y = SAFE_CTA

    # Sombra común: separa los botones del video de fondo.
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((x - 6, y - 6, x + total + 6, y + pill_h + 8),
                         radius=pill_h // 2, fill=(0, 0, 0, 150))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))

    # Suscribirse (rojo de YouTube)
    draw.rounded_rectangle((x, y, x + sub_w, y + pill_h), radius=pill_h // 2, fill=RED)
    _bell(draw, x + pad_x + 6, y + pill_h // 2 - 6, 20)
    draw.text((x + pad_x + 62, y + (pill_h - 52) // 2), subscribe_text, font=font,
              fill=(255, 255, 255, 255))

    # Compartir (oscuro translúcido)
    x2 = x + sub_w + gap
    draw.rounded_rectangle((x2, y, x2 + shr_w, y + pill_h), radius=pill_h // 2, fill=DARK)
    _share_arrow(draw, x2 + pad_x + 4, y + pill_h // 2, 20)
    draw.text((x2 + pad_x + 54, y + (pill_h - 52) // 2), share_text, font=font,
              fill=(255, 255, 255, 255))

    img.save(out)
    return out

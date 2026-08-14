"""Subtítulos quemados en el video, a partir de los segmentos de Whisper.

Se genera un archivo ASS y se deja que ffmpeg lo renderice, en vez de componer un PNG
por segmento. Con overlays de imagen harían falta tantas entradas como frases y el
grafo de filtros se vuelve inmanejable; ASS resuelve tipografía, borde y posición en
una sola pasada.

COLOCACIÓN: en un Short, la franja de abajo la tapa la interfaz de YouTube y la de
arriba lleva el enganche. Los subtítulos van al ~62% de altura, entre ambos.
"""

from __future__ import annotations

import re
from pathlib import Path

# Lienzo de referencia. ASS escala solo si el video tiene otro tamaño.
W, H = 1080, 1920

# Distancia desde abajo hasta la BASE del texto.
#
# En el estilo 'blur' el video ocupa la banda central (de 517 a 1262 px) y debajo queda
# franja borrosa vacía hasta los botones de CTA, que empiezan al 76% (1459 px). Poner
# ahí los subtítulos los hace mucho más legibles que encima del gameplay, que suele ser
# un caos de colores. 1920 - 1425 = 495 deja la base del texto en ese hueco, con sitio
# para dos líneas sin tocar ni el video ni los botones.
MARGEN_V = 495
MAX_CHARS = 30          # por línea; más ancho que esto no se lee de un vistazo
MAX_LINEAS = 2


def _tiempo(segundos: float) -> str:
    """Formato de tiempo de ASS: h:mm:ss.cc"""
    segundos = max(0.0, segundos)
    h = int(segundos // 3600)
    m = int((segundos % 3600) // 60)
    s = segundos % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _partir(texto: str) -> str:
    """Reparte el texto en como mucho dos líneas equilibradas."""
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) <= MAX_CHARS:
        return texto

    palabras = texto.split()
    lineas, actual = [], ""
    for p in palabras:
        if len(actual) + len(p) + 1 <= MAX_CHARS or not actual:
            actual = f"{actual} {p}".strip()
        else:
            lineas.append(actual)
            actual = p
    if actual:
        lineas.append(actual)

    if len(lineas) > MAX_LINEAS:
        # Si no cabe en dos líneas, se recorta: un subtítulo de tres líneas en un
        # Short tapa el video y nadie lo lee entero.
        lineas = lineas[:MAX_LINEAS]
        lineas[-1] = lineas[-1].rstrip(" ,.") + "…"
    return "\\N".join(lineas)


def _escapar(texto: str) -> str:
    return texto.replace("{", "(").replace("}", ")").replace("\n", " ")


def construir_ass(segmentos: list[dict], out: Path, *, tam: int = 58,
                  margen_v: int = MARGEN_V) -> Path:
    """Escribe el archivo ASS con los segmentos ya traducidos."""
    out.parent.mkdir(parents=True, exist_ok=True)

    cabecera = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,DejaVu Sans,{tam},&H00FFFFFF,&H00FFFFFF,&H00000000,&HB0000000,-1,0,0,0,100,100,0,0,1,5,3,2,60,60,{margen_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lineas = []
    for s in segmentos:
        texto = (s.get("text") or "").strip()
        if not texto:
            continue
        ini, fin = float(s.get("start", 0)), float(s.get("end", 0))
        if fin <= ini:
            continue
        lineas.append(
            f"Dialogue: 0,{_tiempo(ini)},{_tiempo(fin)},Base,,0,0,0,,"
            f"{_escapar(_partir(texto))}")

    out.write_text(cabecera + "\n".join(lineas) + "\n", encoding="utf-8")
    return out


def filtro_ffmpeg(ruta_ass: Path) -> str:
    """Fragmento de filtro para ffmpeg.

    En Windows la ruta absoluta ROMPE el filtro por los dos puntos de la unidad, y
    escaparlos como `C\\:` tampoco sirve: probado, falla igual con "Invalid argument".
    La única forma fiable es pasar una ruta relativa al directorio de trabajo.
    """
    import os

    try:
        rel = os.path.relpath(ruta_ass.resolve(), os.getcwd())
        if not rel.startswith(".."):
            return f"subtitles='{Path(rel).as_posix()}'"
    except ValueError:
        pass   # unidades distintas en Windows: no hay ruta relativa posible

    # Último recurso: copiar el archivo junto al directorio de trabajo.
    import shutil
    destino = Path(os.getcwd()) / f"_subs_{ruta_ass.stem}.ass"
    shutil.copyfile(ruta_ass, destino)
    return f"subtitles='{destino.name}'"

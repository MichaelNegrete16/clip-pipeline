"""Transcripción con Whisper y detección de palabras de la blacklist.

Se usa faster-whisper porque da timestamps POR PALABRA, que es justo lo que hace falta:
para pitar una grosería hay que saber en qué segundo exacto empieza y termina. Un
transcriptor que sólo dé frases no sirve — pitarías la frase entera.

La normalización antes de comparar es lo que hace que esto funcione de verdad:
"HIJUEPUTA", "hijueputa" y "hijuepúta" deben caer todas en la misma regla.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))

import config  # noqa: E402

_model = None
MODEL_SIZE = "small"      # equilibrio razonable para español en CPU

# Margen alrededor de la palabra: Whisper corta justo, y sin colchón se escucha el
# principio de la grosería antes de que entre el pitido.
PAD_BEFORE = 0.06
PAD_AFTER = 0.10


def get_model(size: str | None = None):
    """Carga el modelo una sola vez (tarda unos segundos la primera vez)."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(size or MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


# La ñ se protege durante la descomposición: en NFD se parte en "n" + tilde combinante,
# y al quitar los acentos quedaría como "n". Eso convertiría "año" en "ano", que es una
# palabra distinta y además está en muchas blacklists: pitaría cada "año" del video.
_ENYE = "\x00"


def normalize(text: str) -> str:
    """Minúsculas, sin acentos y sin puntuación, para comparar contra la blacklist."""
    text = text.lower().replace("ñ", _ENYE)
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9ñ]", "", text.replace(_ENYE, "ñ"))


def transcribe(audio_path: Path, *, language: str = "es", size: str | None = None) -> dict:
    """Devuelve {'text', 'words': [{'word','start','end'}], 'segments': [...]}"""
    model = get_model(size)
    segments, info = model.transcribe(
        str(audio_path), language=language, word_timestamps=True,
        vad_filter=True,                       # descarta silencios: más rápido y preciso
        vad_parameters={"min_silence_duration_ms": 400},
    )

    words, segs, full = [], [], []
    for seg in segments:
        segs.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
        full.append(seg.text.strip())
        for w in (seg.words or []):
            words.append({"word": w.word.strip(), "start": w.start, "end": w.end})

    return {"text": " ".join(full), "words": words, "segments": segs,
            "language": info.language, "duration": info.duration}


def find_blacklisted(words: list[dict], terms: list[dict]) -> tuple[list[dict], list[str]]:
    """Localiza las palabras a censurar.

    terms: [{'term': 'hijueputa', 'severity': 'bleep'|'reject'}]
    Devuelve (rangos_a_pitar, motivos_de_rechazo).
    """
    bleep_set, reject_set = {}, {}
    for t in terms:
        key = normalize(t["term"])
        if not key:
            continue
        (reject_set if t.get("severity") == "reject" else bleep_set)[key] = t["term"]

    ranges, rejects = [], []
    for w in words:
        key = normalize(w["word"])
        if not key:
            continue

        if key in reject_set:
            rejects.append(reject_set[key])
            continue

        # Coincidencia exacta, o el término contenido en la palabra: cubre "hijueputas"
        # o "malparidooo" sin necesidad de listar cada variante.
        hit = bleep_set.get(key) or next(
            (orig for k, orig in bleep_set.items() if len(k) >= 4 and k in key), None)
        if hit:
            ranges.append({"start": max(0.0, w["start"] - PAD_BEFORE),
                           "end": w["end"] + PAD_AFTER,
                           "word": w["word"], "term": hit})

    return _merge(ranges), rejects


def _merge(ranges: list[dict]) -> list[dict]:
    """Une rangos solapados: dos groserías seguidas deben ser un solo pitido."""
    if not ranges:
        return []
    ranges.sort(key=lambda r: r["start"])
    out = [ranges[0]]
    for r in ranges[1:]:
        if r["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], r["end"])
            out[-1]["word"] += " " + r["word"]
        else:
            out.append(r)
    return out


def censor_audio_graph(ranges: list[dict], *, mode: str = "bleep", tone_hz: int = 1000,
                       src: str = "0:a", out: str = "aout") -> str | None:
    """Fragmento de filter_complex que silencia o pita los rangos indicados.

    mode 'mute'  -> baja el volumen a cero (más discreto)
    mode 'bleep' -> silencia la voz y mezcla un tono encima, el pitido clásico

    El tono NO puede hacerse con `volume=enable=...`: cuando `enable` es falso el filtro
    deja pasar la señal intacta, así que el pitido sonaría durante todo el video. Hay
    que usar una expresión de volumen evaluada por frame que valga 0 fuera de los rangos.
    """
    if not ranges:
        return None

    cond = "+".join(f"between(t,{r['start']:.3f},{r['end']:.3f})" for r in ranges)
    silenced = f"[{src}]volume=enable='{cond}':volume=0"

    if mode == "mute":
        return f"{silenced}[{out}]"

    end = max(r["end"] for r in ranges) + 1
    return (
        f"{silenced}[clean];"
        f"sine=frequency={tone_hz}:sample_rate=48000:duration={end:.2f},"
        f"volume=volume='if({cond},0.28,0)':eval=frame[tone];"
        f"[clean][tone]amix=inputs=2:duration=first:normalize=0[{out}]"
    )


def censor_text(text: str, terms: list[dict]) -> str:
    """Enmascara las palabras en el texto de los subtítulos.

    Si se pita el audio pero el subtítulo escribe la palabra, la censura no sirvió
    de nada. Se aplica la misma máscara a las dos capas.
    """
    keys = [normalize(t["term"]) for t in terms if normalize(t["term"])]
    if not keys:
        return text

    def mask(m: re.Match) -> str:
        w = m.group(0)
        k = normalize(w)
        if any(k == key or (len(key) >= 4 and key in k) for key in keys):
            return w[0] + "*" * (len(w) - 1) if len(w) > 1 else "*"
        return w

    return re.sub(r"\b[\wáéíóúñÁÉÍÓÚÑ]+\b", mask, text)


def extract_audio(video: Path, out: Path) -> Path:
    """Extrae el audio a WAV mono 16 kHz, que es lo que Whisper espera."""
    import subprocess
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [config.ffmpeg(), "-y", "-i", str(video), "-vn",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
        capture_output=True, check=True)
    return out

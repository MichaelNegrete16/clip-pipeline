"""Detección de picos de risa en el chat.

La idea: el chat es un medidor de gracia en tiempo real. Cuando algo tiene gracia, la
gente escribe JAJAJA, KEKW, xd, 💀 — y lo hace de golpe. El pico no está en el volumen
absoluto de risa (un canal de 50k espectadores siempre tiene más que uno de 500), sino
en el SALTO respecto a su propio ritmo normal. Por eso todo se mide contra la línea base
del propio canal, no contra un umbral fijo.

Se cuenta por ventanas de 10 segundos y se compara contra la mediana de los últimos
minutos. La mediana y no el promedio: un solo pico previo inflaría el promedio y
escondería los siguientes.
"""

from __future__ import annotations

import re
import statistics
import time
import unicodedata
from collections import defaultdict, deque

WINDOW_S = 10                 # tamaño de cada cubeta
BASELINE_WINDOWS = 18         # 3 minutos de referencia
MIN_BASELINE_WINDOWS = 6      # antes de esto no hay suficiente historia para decidir
SPIKE_FACTOR = 2.8            # cuántas veces la base cuenta como pico
MIN_LAUGH_ABS = 8             # piso absoluto: evita disparos en chats muertos
COOLDOWN_S = 45               # no marcar el mismo momento dos veces

# Risa escrita. El patrón de repetición cubre jaja/jeje/jiji/jsjs y sus mezclas.
_LAUGH_RE = re.compile(r"\b(?:[ajeisho]{2,}|(?:ja|je|ji|js|ah|eh){2,})\b", re.IGNORECASE)
_XD_RE = re.compile(r"\bx+d+\b", re.IGNORECASE)

# Emotes de risa: valen más que un "xd" suelto porque cuestan más de escribir.
LAUGH_EMOTES = {
    "kekw": 3, "kekl": 3, "lulw": 3, "lul": 2, "omegalul": 3, "pepelaugh": 3,
    "icant": 2, "kekwait": 2, "lmao": 2, "lmfao": 2, "rofl": 2, "kek": 2,
    "jajaja": 2, "pausechamp": 1,
}
LAUGH_CHARS = {"😂": 3, "🤣": 3, "💀": 2, "😭": 1}

# Sorpresa/hype: no es lo mismo que gracia, se mide aparte para no mezclar señales.
HYPE_TOKENS = {"pog", "pogchamp", "poggers", "omg", "wtf", "eeee", "uff", "nooo",
               "vamos", "letsgo", "gg", "wow", "increible", "brutal"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def score_message(msg: str) -> tuple[int, int]:
    """(puntos de risa, puntos de hype) de un mensaje."""
    plain = _norm(msg)
    laugh = hype = 0

    for ch, w in LAUGH_CHARS.items():
        if ch in msg:
            laugh += w * min(msg.count(ch), 3)

    tokens = plain.split()
    for t in tokens[:20]:                       # los mensajes larguísimos no puntúan más
        t = t.strip(".,!?;:")
        if not t:
            continue
        if t in LAUGH_EMOTES:
            laugh += LAUGH_EMOTES[t]
        elif _XD_RE.fullmatch(t):
            laugh += 2 if len(t) > 2 else 1
        elif _LAUGH_RE.fullmatch(t) and len(t) >= 4:
            # Más largo = más risa: "JAJAJAJAJA" pesa más que "jaja", con tope.
            laugh += min(1 + len(t) // 4, 4)
        elif t in HYPE_TOKENS:
            hype += 1

    return laugh, hype


class ChannelDetector:
    """Ventanas deslizantes y detección de picos para un canal."""

    def __init__(self, channel: str):
        self.channel = channel
        self.history: deque[tuple[int, int, int]] = deque(maxlen=BASELINE_WINDOWS)
        self.bucket_start = 0.0
        self.laugh = self.hype = self.msgs = 0
        self.samples: list[str] = []
        self.last_peak_at = 0.0
        self.total_msgs = 0

    def add(self, msg: str, now: float | None = None) -> dict | None:
        now = now or time.time()
        if self.bucket_start == 0.0:
            self.bucket_start = now

        peak = None
        if now - self.bucket_start >= WINDOW_S:
            peak = self._close(now)

        laugh, hype = score_message(msg)
        self.laugh += laugh
        self.hype += hype
        self.msgs += 1
        self.total_msgs += 1
        if laugh >= 3 and len(self.samples) < 5:
            self.samples.append(msg[:120])
        return peak

    def _close(self, now: float) -> dict | None:
        laugh, hype, msgs, samples = self.laugh, self.hype, self.msgs, self.samples
        started = self.bucket_start

        self.history.append((laugh, hype, msgs))
        self.laugh = self.hype = self.msgs = 0
        self.samples = []
        # Si hubo un hueco largo sin mensajes, se reancla en vez de acumular vacíos.
        self.bucket_start = now if now - started > WINDOW_S * 2 else started + WINDOW_S

        if len(self.history) < MIN_BASELINE_WINDOWS:
            return None
        if now - self.last_peak_at < COOLDOWN_S:
            return None

        # La cubeta recién cerrada no entra en su propia línea base.
        previos = [h[0] for h in list(self.history)[:-1]]
        base = statistics.median(previos) if previos else 0.0

        if laugh < MIN_LAUGH_ABS:
            return None
        if laugh < max(base * SPIKE_FACTOR, MIN_LAUGH_ABS):
            return None

        self.last_peak_at = now
        return {
            "channel": self.channel,
            "at": started,
            "window_s": WINDOW_S,
            "laugh": laugh,
            "hype": hype,
            "messages": msgs,
            "baseline": round(base, 2),
            "ratio": round(laugh / base, 2) if base > 0 else float(laugh),
            "samples": samples,
        }


class Detector:
    """Un ChannelDetector por canal."""

    def __init__(self):
        self.channels: dict[str, ChannelDetector] = defaultdict(
            lambda: None)  # type: ignore[arg-type]
        self._map: dict[str, ChannelDetector] = {}

    def add(self, channel: str, msg: str) -> dict | None:
        det = self._map.get(channel)
        if det is None:
            det = self._map[channel] = ChannelDetector(channel)
        return det.add(msg)

    def stats(self) -> dict[str, dict]:
        return {c: {"messages": d.total_msgs, "windows": len(d.history),
                    "baseline": (statistics.median([h[0] for h in d.history])
                                 if d.history else 0)}
                for c, d in self._map.items()}

    def drop(self, channel: str) -> None:
        self._map.pop(channel, None)

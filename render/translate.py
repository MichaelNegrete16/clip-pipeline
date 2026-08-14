"""Traducción inglés → español para subtítulos de clips.

Se usa Argos Translate (local, gratis, sin claves). Traduce bien el inglés corriente
pero se rompe con la jerga de streamer, que es justo de lo que están hechos los clips.
Medido antes de escribir esto:

    "he is cooked"        -> "está cocinado"      (debía ser: está acabado)
    "on stream"           -> "en la corriente"    (corriente eléctrica)
    "he got ratioed"      -> "se racionó"         (sin sentido)
    "nah that is crazy"   -> "no es una locura"   (INVIERTE el significado)

La mitad salía mal y varias al revés. La estrategia es normalizar ANTES de traducir:
se cambia la jerga por inglés llano que el traductor sí entiende, en vez de intentar
arreglar la salida en español, que es donde el error ya se propagó.

El glosario envejece — la jerga cambia cada temporada — así que hay que revisarlo. Es
el precio de no pagar una API.
"""

from __future__ import annotations

import re

_traductor_listo = False

# ── Jerga → inglés llano ───────────────────────────────────────────────────────
# Se aplica sobre el texto en minúsculas, respetando límites de palabra.
JERGA: list[tuple[str, str]] = [
    # Estado / valoración
    (r"\bis cooked\b", "is finished"),
    (r"\bgot cooked\b", "was destroyed"),
    (r"\bhe'?s? done for\b", "he is finished"),
    (r"\bbussin'?\b", "really good"),
    (r"\bno cap\b", "seriously"),
    (r"\bfr fr\b", "seriously"),
    (r"\bfor real\b", "seriously"),
    (r"\bgoated\b", "the best"),
    (r"\bgoat\b", "the best"),
    (r"\bbuilt different\b", "on another level"),
    (r"\bcracked\b", "extremely skilled"),
    (r"\bmid\b", "mediocre"),
    (r"\bsus\b", "suspicious"),
    (r"\bcringe\b", "embarrassing"),
    (r"\bbased\b", "admirable"),
    (r"\bslaps\b", "is excellent"),
    (r"\bfire\b", "excellent"),
    (r"\bclean\b", "impressive"),
    (r"\binsane\b", "incredible"),
    # Acciones. Las sustituciones van CORTAS a propósito: con frases largas el
    # traductor se atasca y devuelve el inglés tal cual.
    (r"\bgot ratioed\b", "was criticized"),
    (r"\bratioed\b", "criticized"),
    (r"\bgriefed\b", "was sabotaged"),
    (r"\bclutch(ed)?\b", "won at the last moment"),
    (r"\bthrew\b", "lost on purpose"),
    (r"\bcarried\b", "did all the work"),
    (r"\bsent it\b", "went for it"),
    (r"\btouch grass\b", "go outside"),
    (r"\btweaking\b", "acting strange"),
    (r"\blacking\b", "unprepared"),
    (r"\bcooking\b", "doing something great"),
    # Interjecciones: sin esto, "nah" se traduce como negación y da la vuelta al sentido
    (r"\bnah\b", "wow"),
    (r"\byo\b", "hey"),
    # "brother" -> hermano, que es como se dice de verdad. "man" daba "hombre".
    (r"\bbruh\b", "brother"),
    (r"\bbro\b", "brother"),
    (r"\bdawg\b", "brother"),
    (r"\bchat\b", "guys"),
    (r"\bgang\b", "guys"),
    # Primero la frase completa: con `dead` suelto, "I am dead laughing" salía
    # "me estoy riendo duro riendo".
    (r"\bi'?m dead\b", "that is hilarious"),
    (r"\bi am dead\b", "that is hilarious"),
    (r"\bdead ass\b", "seriously"),
    (r"\blmao\b", "hilarious"),
    (r"\blmfao\b", "hilarious"),
    (r"\bomg\b", "oh my god"),
    (r"\bwtf\b", "what the hell"),
    (r"\bidk\b", "I do not know"),
    (r"\bngl\b", "honestly"),
    (r"\btbh\b", "honestly"),
    (r"\bimo\b", "in my opinion"),
    (r"\bpog\b", "amazing"),
    (r"\bpoggers\b", "amazing"),
    # Contexto de streaming: "stream" se traducía como corriente eléctrica
    (r"\bon stream\b", "on the live broadcast"),
    (r"\bthe stream\b", "the live broadcast"),
    (r"\bstreaming\b", "broadcasting live"),
    (r"\bstreamer\b", "broadcaster"),
    (r"\bsub(s)?\b", "subscriber\\1"),
    (r"\bmods?\b", "moderator"),
    (r"\bviewers\b", "audience"),
    (r"\bclip(ped)?\b", "recorded moment"),
]

# ── Retoques en español ────────────────────────────────────────────────────────
# Sólo para lo que el traductor deja mal aunque la entrada esté limpia.
RETOQUES: list[tuple[str, str]] = [
    (r"\b(la|una)\s+corriente\b", "el directo"),
    (r"\b(la|una)\s+(transmisión|emisión|difusión)( en vivo)?\b", "el directo"),
    (r"\bcorriente en vivo\b", "directo"),
    # El traductor deja el artículo del femenino original: "en la directo".
    (r"\bla directo\b", "el directo"),
    (r"\buna directo\b", "un directo"),
    (r"\bhombre,?\s+hombre\b", "hermano"),
    (r"\bmaldita sea\b", "joder"),
    (r"\bDios mío\b", "madre mía"),
    # Repeticiones que deja el glosario al solaparse con la frase original.
    (r"\b(riendo)(\s+\w+)?\s+riendo\b", r"\1"),
    (r"\b(\w+)\s+\1\b", r"\1"),
]

_JERGA = [(re.compile(p, re.IGNORECASE), r) for p, r in JERGA]
_RETOQUES = [(re.compile(p, re.IGNORECASE), r) for p, r in RETOQUES]


def _cargar() -> None:
    global _traductor_listo
    if _traductor_listo:
        return
    import argostranslate.package as pk
    import argostranslate.settings as ajustes
    import argostranslate.translate as tr

    # Sin esto, argostranslate parte las frases con `stanza`, que DESCARGA recursos de
    # GitHub la primera vez que se usa. Un render falló con 503 porque GitHub estaba
    # saturado: depender de la red al renderizar es inaceptable. MINISBD hace el mismo
    # trabajo en local y sin descargas.
    ajustes.chunk_type = ajustes.ChunkType.MINISBD

    if not any(l.code == "en" for l in tr.get_installed_languages()):
        pk.update_package_index()
        paquete = next((x for x in pk.get_available_packages()
                        if x.from_code == "en" and x.to_code == "es"), None)
        if paquete is None:
            raise RuntimeError("No hay paquete de traducción en->es disponible")
        pk.install_from_path(paquete.download())
    _traductor_listo = True


def normalizar_jerga(texto: str) -> str:
    """Cambia la jerga por inglés llano antes de traducir."""
    for patron, reemplazo in _JERGA:
        texto = patron.sub(reemplazo, texto)
    return re.sub(r"\s+", " ", texto).strip()


def pulir(texto: str) -> str:
    """Arregla los tics que el traductor deja aunque la entrada esté limpia."""
    for patron, reemplazo in _RETOQUES:
        texto = patron.sub(reemplazo, texto)
    texto = re.sub(r"\s+([,.!?])", r"\1", texto)
    return re.sub(r"\s+", " ", texto).strip()


def traducir(texto: str, desde: str = "en", hacia: str = "es") -> str:
    """Traduce una frase suelta."""
    if not texto or not texto.strip():
        return ""
    if desde == hacia:
        return texto
    _cargar()
    import argostranslate.translate as tr
    return pulir(tr.translate(normalizar_jerga(texto), desde, hacia))


def traducir_segmentos(segmentos: list[dict], desde: str = "en",
                       hacia: str = "es") -> list[dict]:
    """Traduce los segmentos de Whisper conservando sus tiempos.

    Se traduce por segmento entero y no palabra a palabra: sin la frase completa el
    traductor pierde el sujeto y el tiempo verbal, y sale peor que no traducir.
    """
    if desde == hacia:
        return segmentos
    _cargar()
    import argostranslate.translate as tr

    salida = []
    for s in segmentos:
        original = (s.get("text") or "").strip()
        traducido = pulir(tr.translate(normalizar_jerga(original), desde, hacia)) \
            if original else ""
        salida.append({**s, "text": traducido, "text_original": original})
    return salida

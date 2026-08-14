"""Generación de títulos, descripción y tags para YouTube.

Los títulos originales de los clips son basura para YouTube: los pone quien clipea, en
segundos, y salen cosas como "xd", "??" o "parce fabi". Sirven como pista de qué pasó,
no como título publicable. Aquí se limpian y se arma algo presentable.

Esto es determinista, a base de plantilla y reglas. Da títulos correctos y consistentes,
pero no creativos: para eso haría falta un modelo de lenguaje leyendo la transcripción.
Queda como mejora una vez existan los subtítulos.
"""

from __future__ import annotations

import re

MAX_TITLE = 100          # límite duro de YouTube
SHORTS_TAG = "#Shorts"

# Títulos que no aportan nada: si el clip trae uno de estos, mejor usar un genérico.
JUNK = {"xd", "xdd", "jaja", "jajaja", "??", "?", ".", "..", "...", "clip", "lol",
        "wtf", "omg", "a", "e", "aa", "ee", "uf", "ay", ""}


def clean_source_title(raw: str | None) -> str:
    """Normaliza el título original del clip."""
    if not raw:
        return ""
    t = re.sub(r"\s+", " ", raw).strip()
    t = re.sub(r"^[\-–—:;,.\s]+|[\-–—:;,.\s]+$", "", t)

    if t.lower() in JUNK or len(t) < 3:
        return ""

    # TODO EN MAYÚSCULAS se lee como grito y YouTube lo penaliza en algunos formatos.
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        t = t.capitalize()

    return t


def build_title(clip: dict, template: str | None, *, is_short: bool = True,
                blacklist: list[dict] | None = None) -> str:
    """Arma el título final a partir de la plantilla del profile.

    Variables: {title} {source} {category}
    """
    clean = _sin_palabrotas(clean_source_title(clip.get("title")), blacklist)
    source = clip.get("source_slug") or ""
    category = clip.get("category") or ""

    # Si al quitar la grosería el título se queda en nada, mejor uno genérico que
    # un resto sin sentido.
    if len(clean) < 3:
        clean = ""

    if not clean:
        clean = f"Lo mejor de {source}" if source else "Momento del directo"

    tpl = template or "{title} | {source}"
    out = tpl.format(title=clean, source=source, category=category)
    out = re.sub(r"\s*\|\s*\|\s*", " | ", out)          # variables vacías dejan barras sueltas
    out = re.sub(r"\s+", " ", out).strip(" |-")

    if is_short and SHORTS_TAG.lower() not in out.lower():
        # #Shorts sólo si cabe: truncar el título para meterlo sería peor negocio.
        if len(out) + len(SHORTS_TAG) + 1 <= MAX_TITLE:
            out = f"{out} {SHORTS_TAG}"

    return out[:MAX_TITLE]


MAX_HOOK = 62           # más largo que esto no se lee de un vistazo en un Short


def _sin_palabrotas(texto: str, blacklist: list[dict] | None) -> str:
    """Quita del texto las palabras de la blacklist.

    En el AUDIO se pita y en los subtítulos se enmascara, pero en un título los
    asteriscos quedan peor que la palabra: "f*******" grita que había una grosería.
    Aquí se elimina y ya. Si al quitarla no queda nada legible, el que llama usa el
    título genérico.

    Esto faltaba: la censura sólo miraba audio y subtítulos, así que se publicó un
    Short titulado "pokemitas follables" pese a tener `follar` en la blacklist. Y el
    título es justo lo que ven los clasificadores de YouTube y la gente en el feed.
    """
    if not texto or not blacklist:
        return texto

    claves = []
    for t in blacklist:
        k = re.sub(r"[^a-záéíóúñ0-9]", "",
                   (t.get("term") or "").lower())
        if k:
            claves.append(k)
    if not claves:
        return texto

    def limpio(palabra: str) -> str:
        base = re.sub(r"[^a-záéíóúñ0-9]", "", palabra.lower())
        base = (base.replace("á", "a").replace("é", "e").replace("í", "i")
                    .replace("ó", "o").replace("ú", "u"))
        for k in claves:
            kk = (k.replace("á", "a").replace("é", "e").replace("í", "i")
                   .replace("ó", "o").replace("ú", "u"))
            if base == kk or (len(kk) >= 4 and kk in base):
                return ""
        return palabra

    salida = " ".join(p for p in (limpio(w) for w in texto.split()) if p)
    return re.sub(r"\s+", " ", salida).strip(" |-–—·,.")


def build_hook(clip: dict, blacklist: list[dict] | None = None) -> str:
    """Texto de enganche para pintar SOBRE el video.

    No es el título de YouTube: ahí van el canal y #Shorts porque sirven para búsqueda
    y algoritmo. En pantalla eso sólo estorba — se quiere la frase pelada y corta.
    """
    text = _sin_palabrotas(clean_source_title(clip.get("title")), blacklist)
    if not text or len(text) < 3:
        return ""

    # Quitar el nombre del streamer, hashtags y colas de plantilla si vinieran pegados.
    source = (clip.get("source_slug") or "").lower()
    text = re.sub(r"#\w+", "", text)
    if source:
        text = re.sub(rf"\b{re.escape(source)}\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[|·–—-]\s*$", "", text)
    text = re.sub(r"^\s*[|·–—-]\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip(" |-–—·")

    if len(text) <= MAX_HOOK:
        return text
    # Cortar en el último espacio para no partir una palabra.
    cut = text[:MAX_HOOK].rsplit(" ", 1)[0]
    return (cut or text[:MAX_HOOK]).rstrip(" ,.;:") + "…"


def build_hashtags(clip: dict, is_short: bool = True) -> list[str]:
    """Hashtags para la descripción.

    YouTube muestra los TRES PRIMEROS encima del título, así que el orden importa:
    van primero los que identifican el contenido, no los genéricos.
    """
    tags: list[str] = []
    source = (clip.get("source_slug") or "").strip()
    if source:
        tags.append("#" + re.sub(r"[^A-Za-z0-9]", "", source))

    categoria = (clip.get("category") or "").strip()
    if categoria:
        limpia = re.sub(r"[^A-Za-z0-9]", "", categoria)
        if limpia and len(limpia) <= 20:
            tags.append("#" + limpia)

    if is_short:
        tags.append("#Shorts")
    tags += ["#clips", "#twitch" if clip.get("platform") == "twitch" else "#kick"]

    vistos, out = set(), []
    for t in tags:
        k = t.lower()
        if k not in vistos and len(t) > 1:
            vistos.add(k)
            out.append(t)
    return out[:5]


def build_description(clip: dict, profile: dict, is_short: bool = True,
                      blacklist: list[dict] | None = None) -> str:
    """Descripción con atribución al creador original.

    NO se afirma tener permiso: escribirlo sin tenerlo es una declaración falsa, y ante
    un reclamo sería prueba de que sabías que hacía falta. Lo correcto es dar crédito
    visible y enlazar al original.
    """
    source = clip.get("source_slug") or ""
    platform = (clip.get("platform") or "").capitalize()
    url = clip.get("clip_page_url") or ""
    canal_url = (f"https://www.twitch.tv/{source}" if clip.get("platform") == "twitch"
                 else f"https://kick.com/{source}")

    encabezado = _sin_palabrotas(clean_source_title(clip.get("title")), blacklist)
    lines = [encabezado if len(encabezado) >= 3 else "Momento del directo", ""]
    lines.append(" ".join(build_hashtags(clip, is_short)))
    lines.append("")
    if source:
        lines.append(f"Clip original de {source} en {platform}.")
        lines.append(f"Sigue a {source}: {canal_url}")
    if url:
        lines.append(f"Clip: {url}")
    lines += ["", "Todos los derechos del contenido original pertenecen a su creador.",
              "Si eres el creador y quieres que retire este video, escríbeme y lo hago."]
    return "\n".join(lines).strip()[:5000]


def build_tags(clip: dict, profile_tags: list[str] | None) -> list[str]:
    """Tags: los del canal + la fuente + la categoría. Sin duplicados, máx. 15.

    En YouTube los tags mueven poquísimo comparado con el título y la retención; van
    por completitud, no porque vayan a cambiar el resultado.
    """
    tags = list(profile_tags or [])
    for extra in (clip.get("source_slug"), clip.get("category"), clip.get("platform")):
        if extra and extra not in tags:
            tags.append(extra)

    seen, out = set(), []
    for t in tags:
        k = t.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(t.strip())
    return out[:15]

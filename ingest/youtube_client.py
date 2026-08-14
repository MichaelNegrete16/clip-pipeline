"""Cliente de YouTube: OAuth, subida de videos y lectura de métricas.

Sin SDK de Google: son tres llamadas HTTP y así se ve exactamente qué se manda.

SOBRE SHORTS: no existe un endpoint aparte. Se sube todo por `videos.insert` y YouTube
clasifica como Short lo que sea vertical (9:16 o cuadrado) y dure <= 3 minutos. El
formato se decide al renderizar, no al subir.

LÍMITES (medidos, no supuestos):

  - La cuota de la API es de 10.000 unidades/día por proyecto y `videos.insert` figura
    en 1.600, lo que daría 6 subidas. En la práctica se subieron 10 en un día sin que
    la cuota se agotara, así que ese cálculo NO es el techo real.
  - El techo que sí aparece es `uploadLimitExceeded`: un límite POR CANAL que YouTube
    aplica a cuentas pequeñas o nuevas, independiente de la cuota de API. No está
    documentado y sube conforme el canal gana antigüedad y reputación.

O sea: el freno práctico es el canal, no el proyecto.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3/videos"
ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",      # subir videos
    "https://www.googleapis.com/auth/youtube.readonly",    # leer datos del canal
    "https://www.googleapis.com/auth/yt-analytics.readonly",  # métricas y retención
    # Permite editar nombre, descripción y palabras clave del canal. La foto de perfil
    # NO se puede cambiar por API con ningún permiso: eso es sólo desde YouTube Studio.
    "https://www.googleapis.com/auth/youtube",
]


def update_channel(access_token: str, channel_id: str, *, title: str | None = None,
                   description: str | None = None, keywords: str | None = None,
                   country: str = "CO", language: str = "es") -> dict:
    """Actualiza la identidad textual del canal. Requiere el scope `youtube`."""
    branding: dict = {"channel": {"country": country, "defaultLanguage": language}}
    if title:
        branding["channel"]["title"] = title
    if description:
        branding["channel"]["description"] = description[:1000]
    if keywords:
        branding["channel"]["keywords"] = keywords

    resp = requests.put(
        f"{API}/channels", params={"part": "brandingSettings"},
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json"},
        json={"id": channel_id, "brandingSettings": branding}, timeout=40)
    if resp.status_code != 200:
        raise YouTubeError(
            f"No se pudo actualizar el canal ({resp.status_code}): {resp.text[:250]}")
    return resp.json()

UPLOAD_QUOTA_COST = 1600
DAILY_QUOTA = 10_000


class YouTubeError(RuntimeError):
    pass


class YouTubeNotConfigured(YouTubeError):
    pass


def _creds() -> tuple[str, str]:
    cid = config.get("YOUTUBE_CLIENT_ID")
    secret = config.get("YOUTUBE_CLIENT_SECRET")
    if not cid or not secret:
        raise YouTubeNotConfigured(
            "Faltan YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET en el .env. "
            "Créalas en https://console.cloud.google.com/apis/credentials"
        )
    return cid, secret


def is_configured() -> bool:
    try:
        _creds()
        return True
    except YouTubeNotConfigured:
        return False


def auth_url(redirect_uri: str, state: str) -> str:
    """URL de consentimiento de Google.

    access_type=offline + prompt=consent son obligatorios para recibir refresh_token:
    sin ellos Google sólo manda un access_token de una hora y la automatización muere.
    """
    cid, _ = _creds()
    return f"{AUTH_URL}?" + urlencode({
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })


def exchange_code(code: str, redirect_uri: str) -> dict:
    cid, secret = _creds()
    resp = requests.post(TOKEN_URL, data={
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        raise YouTubeError(f"No se pudo canjear el código ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict:
    cid, secret = _creds()
    resp = requests.post(TOKEN_URL, data={
        "refresh_token": refresh_token, "client_id": cid,
        "client_secret": secret, "grant_type": "refresh_token",
    }, timeout=30)
    if resp.status_code != 200:
        raise YouTubeError(
            f"No se pudo refrescar el token ({resp.status_code}): {resp.text[:300]}\n"
            "Si el proyecto está en modo 'Testing', los refresh_token vencen a los 7 días: "
            "pásalo a 'In production' en la pantalla de consentimiento."
        )
    return resp.json()


def get_my_channel(access_token: str) -> dict:
    resp = requests.get(f"{API}/channels", headers={"Authorization": f"Bearer {access_token}"},
                        params={"part": "snippet,statistics", "mine": "true"}, timeout=30)
    if resp.status_code != 200:
        raise YouTubeError(f"No se pudo leer el canal ({resp.status_code}): {resp.text[:300]}")
    items = resp.json().get("items") or []
    if not items:
        raise YouTubeError("La cuenta autorizada no tiene ningún canal de YouTube.")
    it = items[0]
    thumbs = (it["snippet"].get("thumbnails") or {})
    # De mayor a menor: queremos el avatar más grande disponible para la marca de agua.
    avatar = next((thumbs[k]["url"] for k in ("high", "medium", "default") if thumbs.get(k)), None)
    return {
        "id": it["id"],
        "title": it["snippet"]["title"],
        "avatar_url": avatar,
        "subscribers": int(it.get("statistics", {}).get("subscriberCount") or 0),
        "views": int(it.get("statistics", {}).get("viewCount") or 0),
        "videos": int(it.get("statistics", {}).get("videoCount") or 0),
    }


def upload_video(access_token: str, file_path: str | Path, *, title: str,
                 description: str = "", tags: list[str] | None = None,
                 privacy: str = "private", publish_at: str | None = None,
                 category_id: str = "24", made_for_kids: bool = False) -> dict:
    """Sube un video con el protocolo resumable (dos pasos: metadatos y luego bytes).

    privacy: 'private' | 'unlisted' | 'public'. Si el proyecto no pasó la auditoría de
    YouTube, la subida queda forzada a privada aunque pidas 'public'.
    publish_at: ISO 8601 UTC para programar; exige privacy='private'.
    """
    path = Path(file_path)
    if not path.exists():
        raise YouTubeError(f"No existe el archivo: {path}")

    status: dict = {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids}
    if publish_at:
        status["publishAt"] = publish_at
        status["privacyStatus"] = "private"   # requisito de la API para programar

    metadata = {
        "snippet": {"title": title[:100], "description": description[:5000],
                    "tags": (tags or [])[:500], "categoryId": category_id},
        "status": status,
    }

    size = path.stat().st_size
    init = requests.post(
        UPLOAD_API,
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/*",
                 "X-Upload-Content-Length": str(size)},
        params={"uploadType": "resumable", "part": "snippet,status"},
        json=metadata, timeout=60,
    )
    if init.status_code not in (200, 201):
        raise YouTubeError(f"Fallo al iniciar la subida ({init.status_code}): {init.text[:300]}")

    session_url = init.headers.get("Location")
    if not session_url:
        raise YouTubeError("Google no devolvió la URL de sesión de subida.")

    with path.open("rb") as fh:
        put = requests.put(session_url, data=fh,
                           headers={"Content-Type": "video/*", "Content-Length": str(size)},
                           timeout=None)
    if put.status_code not in (200, 201):
        raise YouTubeError(f"Fallo al subir los bytes ({put.status_code}): {put.text[:300]}")

    data = put.json()
    return {"id": data["id"], "title": data["snippet"]["title"],
            "privacy": data["status"]["privacyStatus"],
            "url": f"https://youtu.be/{data['id']}"}


def video_metrics(access_token: str, channel_id: str, start_date: str, end_date: str,
                  video_ids: list[str] | None = None) -> list[dict]:
    """Métricas por video vía YouTube Analytics API.

    `averageViewPercentage` es la métrica que de verdad importa en Shorts: mide retención,
    que es lo que mueve el algoritmo. Las vistas son consecuencia, no causa.
    """
    params = {
        "ids": f"channel=={channel_id}",
        "startDate": start_date,
        "endDate": end_date,
        "metrics": "views,estimatedMinutesWatched,averageViewDuration,"
                   "averageViewPercentage,subscribersGained,likes,comments",
        "dimensions": "video",
        "sort": "-views",
        "maxResults": 200,
    }
    if video_ids:
        params["filters"] = "video==" + ",".join(video_ids[:500])

    resp = requests.get(ANALYTICS_API, headers={"Authorization": f"Bearer {access_token}"},
                        params=params, timeout=60)
    if resp.status_code != 200:
        raise YouTubeError(f"Analytics falló ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    cols = [c["name"] for c in data.get("columnHeaders", [])]
    return [dict(zip(cols, row)) for row in data.get("rows", [])]


def quota_budget(uploads_per_day: int, subidas_hoy: int = 0) -> dict:
    """Estado de los dos límites que existen.

    `max_uploads` es orientativo: la cuota teórica daría 6, pero se han subido 10 en
    un día sin agotarla. El que corta de verdad es el límite por canal, que no se
    puede consultar — sólo se descubre cuando YouTube devuelve uploadLimitExceeded.
    """
    used = uploads_per_day * UPLOAD_QUOTA_COST
    return {
        "cost_per_upload": UPLOAD_QUOTA_COST,
        "daily_quota": DAILY_QUOTA,
        "used": used,
        "uploaded_today": subidas_hoy,
        "max_uploads": DAILY_QUOTA // UPLOAD_QUOTA_COST,
        "fits": used <= DAILY_QUOTA,
        "nota": ("El límite real es por canal (uploadLimitExceeded), no la cuota del "
                 "proyecto: se han subido 10 en un día sin agotar las unidades."),
    }

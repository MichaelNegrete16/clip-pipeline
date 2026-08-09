"""Carga de credenciales desde .env (sin dependencias externas)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Lee KEY=VALOR de .env. Lo ya presente en el entorno tiene prioridad."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    for key in list(values):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def get(key: str, default: str | None = None) -> str | None:
    return load_env().get(key) or os.environ.get(key) or default


def find_binary(name: str) -> str | None:
    """Localiza un ejecutable. Prioriza el .env, luego el PATH, luego WinGet.

    winget instala ffmpeg y modifica el PATH, pero los procesos ya abiertos no ven el
    cambio hasta reiniciar la shell: por eso buscamos también en su carpeta de paquetes.
    """
    override = get(f"{name.upper()}_PATH")
    if override and Path(override).exists():
        return override

    found = shutil.which(name)
    if found:
        return found

    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget.exists():
        for exe in winget.glob(f"**/bin/{name}.exe"):
            return str(exe)
    return None


def ffmpeg() -> str:
    path = find_binary("ffmpeg")
    if not path:
        raise RuntimeError("No se encontró ffmpeg. Instálalo con: winget install Gyan.FFmpeg")
    return path


def ffprobe() -> str:
    path = find_binary("ffprobe")
    if not path:
        raise RuntimeError("No se encontró ffprobe (viene con ffmpeg)")
    return path


def twitch_credentials() -> tuple[str, str] | None:
    cid = get("TWITCH_CLIENT_ID")
    secret = get("TWITCH_CLIENT_SECRET")
    if not cid or not secret:
        return None
    return cid, secret

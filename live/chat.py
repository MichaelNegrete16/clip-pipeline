"""Lector del chat de Twitch por IRC, anónimo.

Twitch permite leer cualquier chat público SIN autenticación: basta conectarse con un
nick `justinfan<numero>` y no mandar PASS. No consume cuota de la API.

Se usa UNA sola conexión para todos los canales, no una por canal. Twitch admite ~100
canales por conexión y limita a 20 JOIN cada 10 segundos, así que los JOIN se espacian.

Aquí sólo se lee y se parsea; la detección de picos vive en detector.py.
"""

from __future__ import annotations

import random
import re
import socket
import ssl
import threading
import time
from collections import deque
from typing import Callable

HOST, PORT = "irc.chat.twitch.tv", 6697
JOIN_BURST, JOIN_WINDOW = 18, 10.0      # margen bajo el límite real de 20/10s

# :usuario!usuario@usuario.tmi.twitch.tv PRIVMSG #canal :mensaje
_PRIVMSG = re.compile(r"^:(?P<user>[^!]+)![^ ]+ PRIVMSG #(?P<chan>[^ ]+) :(?P<msg>.*)$")


class TwitchChat:
    """Conexión única al IRC de Twitch con JOIN/PART dinámico."""

    def __init__(self, on_message: Callable[[str, str, str], None]):
        self.on_message = on_message          # (canal, usuario, mensaje)
        self.sock: ssl.SSLSocket | None = None
        self.joined: set[str] = set()
        self.wanted: set[str] = set()
        self._join_times: deque[float] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.messages_seen = 0
        self.last_error: str | None = None

    # ── conexión ──────────────────────────────────────────────────────────────
    def _connect(self) -> None:
        raw = socket.create_connection((HOST, PORT), timeout=20)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
        self.sock.settimeout(1.0)
        nick = f"justinfan{random.randint(10000, 99999)}"
        self._send(f"NICK {nick}")
        self.connected = True
        self.joined.clear()

    def _send(self, line: str) -> None:
        if self.sock:
            self.sock.sendall((line + "\r\n").encode("utf-8"))

    def _throttled_join(self, channel: str) -> bool:
        """Respeta el límite de JOIN. Devuelve False si toca esperar."""
        now = time.time()
        while self._join_times and now - self._join_times[0] > JOIN_WINDOW:
            self._join_times.popleft()
        if len(self._join_times) >= JOIN_BURST:
            return False
        self._send(f"JOIN #{channel}")
        self._join_times.append(now)
        self.joined.add(channel)
        return True

    # ── API pública ───────────────────────────────────────────────────────────
    def set_channels(self, channels: set[str]) -> None:
        """Define qué canales seguir. Los JOIN/PART se aplican en el bucle."""
        with self._lock:
            self.wanted = {c.lower().lstrip("#") for c in channels}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="twitch-chat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.connected = False

    # ── bucle ─────────────────────────────────────────────────────────────────
    def _run(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            try:
                self._connect()
                backoff = 2
                self._loop()
            except Exception as exc:  # noqa: BLE001 - la red se cae, se reconecta
                self.last_error = str(exc)
                self.connected = False
                if self._stop.is_set():
                    return
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _loop(self) -> None:
        buf = ""
        while not self._stop.is_set():
            with self._lock:
                salir = self.joined - self.wanted
                entrar = self.wanted - self.joined
            for c in salir:
                self._send(f"PART #{c}")
                self.joined.discard(c)
            for c in sorted(entrar):
                if not self._throttled_join(c):
                    break            # se reintenta en la siguiente vuelta

            try:
                data = self.sock.recv(8192).decode("utf-8", errors="replace")
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            if not data:
                raise ConnectionError("Twitch cerró la conexión")

            buf += data
            while "\r\n" in buf:
                line, buf = buf.split("\r\n", 1)
                self._handle(line)

    def _handle(self, line: str) -> None:
        if line.startswith("PING"):
            # Sin PONG, Twitch corta la conexión al poco rato.
            self._send("PONG :tmi.twitch.tv")
            return
        m = _PRIVMSG.match(line)
        if m:
            self.messages_seen += 1
            self.on_message(m.group("chan"), m.group("user"), m.group("msg"))

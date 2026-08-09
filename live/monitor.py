"""Bot que vigila los directos y detecta los momentos graciosos en tiempo real.

    python live/monitor.py            # corre indefinidamente
    python live/monitor.py --once     # una pasada de sondeo, para probar
    python live/monitor.py --minutes 10

Ciclo:
    1. Cada 2 minutos pregunta a Twitch quién de la whitelist está en directo (1 unidad
       de cuota por llamada, prácticamente gratis).
    2. Mantiene UNA conexión al IRC de Twitch unida a todos esos canales, sin autenticar.
    3. Puntúa la risa del chat en ventanas de 10 segundos y detecta saltos contra la
       línea base de cada canal.
    4. Guarda cada pico con su marca de tiempo. Después, cuando se ingestan clips, los
       creados cerca de un pico se marcan como momentos confirmados por el chat.

Kick queda para una segunda fase: usa websocket de Pusher en vez de IRC.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "live"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import db as db_mod          # noqa: E402
import twitch_client as tw   # noqa: E402
from chat import TwitchChat  # noqa: E402
from detector import Detector  # noqa: E402

POLL_S = 120


class Monitor:
    def __init__(self):
        self.detector = Detector()
        self.chat = TwitchChat(self._on_message)
        self.sessions: dict[str, int] = {}     # login -> live_sessions.id
        self.source_ids: dict[str, int] = {}   # login -> sources.id
        self.peaks = 0
        self.running = True

    # ── datos ─────────────────────────────────────────────────────────────────
    def _twitch_sources(self, conn) -> dict[str, int]:
        return {r["slug"].lower(): r["id"] for r in conn.execute(
            "SELECT id, slug FROM sources WHERE platform='twitch' AND enabled=1")}

    def _on_message(self, channel: str, user: str, msg: str) -> None:
        peak = self.detector.add(channel, msg)
        if peak:
            self._save_peak(peak)

    def _save_peak(self, peak: dict) -> None:
        source_id = self.source_ids.get(peak["channel"])
        if not source_id:
            return
        at = datetime.fromtimestamp(peak["at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        conn = db_mod.connect()
        try:
            conn.execute(
                "INSERT INTO chat_peaks (source_id, session_id, at_utc, window_s, laugh, "
                "hype, messages, baseline, ratio, samples) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source_id, self.sessions.get(peak["channel"]), at, peak["window_s"],
                 peak["laugh"], peak["hype"], peak["messages"], peak["baseline"],
                 peak["ratio"], json.dumps(peak["samples"], ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()

        self.peaks += 1
        muestra = peak["samples"][0][:60] if peak["samples"] else ""
        print(f"  PICO  {peak['channel']:<18} risa={peak['laugh']:>4} "
              f"(base {peak['baseline']:>5.1f}, x{peak['ratio']:>4.1f})  "
              f"{peak['messages']:>3} msgs   {muestra}")

    # ── sondeo ────────────────────────────────────────────────────────────────
    def poll(self) -> set[str]:
        conn = db_mod.connect()
        try:
            self.source_ids = self._twitch_sources(conn)
            if not self.source_ids:
                return set()

            live = tw.get_live_streams(list(self.source_ids))

            for login, info in live.items():
                sid = self.source_ids.get(login)
                if not sid:
                    continue
                conn.execute(
                    "INSERT INTO live_sessions (source_id, stream_id, title, category, "
                    "viewers, started_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT (source_id, stream_id) DO UPDATE SET "
                    "viewers=excluded.viewers, title=excluded.title, "
                    "seen_at=datetime('now')",
                    (sid, info["stream_id"], info["title"], info["category"],
                     info["viewers"], info["started_at"]))
                row = conn.execute(
                    "SELECT id FROM live_sessions WHERE source_id=? AND stream_id=?",
                    (sid, info["stream_id"])).fetchone()
                self.sessions[login] = row["id"]

            # Cerrar las sesiones de quienes ya no están en directo.
            for login in list(self.sessions):
                if login not in live:
                    conn.execute(
                        "UPDATE live_sessions SET ended_at=datetime('now') "
                        "WHERE id=? AND ended_at IS NULL", (self.sessions[login],))
                    self.sessions.pop(login, None)
                    self.detector.drop(login)
            conn.commit()
            return set(live)
        finally:
            conn.close()

    def run(self, minutes: float | None = None, once: bool = False) -> None:
        if not tw.is_configured():
            sys.exit("Faltan credenciales de Twitch en el .env")

        deadline = time.time() + minutes * 60 if minutes else None
        self.chat.start()
        siguiente = 0.0

        while self.running:
            if time.time() >= siguiente:
                try:
                    live = self.poll()
                except Exception as exc:  # noqa: BLE001 - un fallo de red no tumba el bot
                    print(f"  error sondeando: {exc}")
                    live = set(self.sessions)
                self.chat.set_channels(live)
                estado = "conectado" if self.chat.connected else "conectando..."
                print(f"[{datetime.now():%H:%M:%S}] en directo: {len(live)} "
                      f"({', '.join(sorted(live)[:6])}{'...' if len(live) > 6 else ''}) "
                      f"| chat {estado} | {self.chat.messages_seen} msgs | {self.peaks} picos")
                if once:
                    break
                siguiente = time.time() + POLL_S

            if deadline and time.time() >= deadline:
                break
            time.sleep(1)

        self.chat.stop()
        print(f"\nresumen: {self.chat.messages_seen} mensajes leídos, {self.peaks} picos")

    def stop(self, *_) -> None:
        self.running = False


def main() -> None:
    ap = argparse.ArgumentParser(description="Bot de chat en vivo")
    ap.add_argument("--once", action="store_true", help="sólo un sondeo y salir")
    ap.add_argument("--minutes", type=float, help="correr durante N minutos")
    args = ap.parse_args()

    mon = Monitor()
    signal.signal(signal.SIGINT, mon.stop)
    mon.run(minutes=args.minutes, once=args.once)


if __name__ == "__main__":
    main()

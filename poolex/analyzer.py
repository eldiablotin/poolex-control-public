"""
Analyseur interactif pour les tests provoqués.

Usage sur le RPi :
    python3 -m poolex.analyzer

Protocole de test :
    1. Lancer ce script
    2. Effectuer une action sur la PAC (changer consigne, allumer, etc.)
    3. Appuyer sur Entrée avec un label décrivant l'action
    4. Observer les bytes qui ont changé
    5. Le rapport final liste toutes les corrélations action → bytes

Lecture directe sur le port série OU sur la DB SQLite selon disponibilité.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import termios
import time
import tty
from collections import deque
from datetime import datetime
from typing import Optional

from .decoder import DDFrame, CDFrame, Frame, decode, diff, FRAME_SIZE, HEADERS

DB_PATH     = os.environ.get("POOLEX_DB_PATH", "/var/lib/poolex/poolex.db")
POLL_INTERVAL = 0.3   # secondes entre deux lectures DB
HISTORY_LEN   = 5     # nombre de trames gardées par type pour le diff

# Codes ANSI
_R  = "\033[0m"
_B  = "\033[1m"
_GR = "\033[32m"
_YL = "\033[33m"
_RD = "\033[31m"
_CY = "\033[36m"
_DIM = "\033[2m"
_INV = "\033[7m"

HEADER_COLOR = {
    "DD": _GR,
    "D2": _CY,
    "CC": _CY,
    "CD": _YL,
}


# ---------------------------------------------------------------------------
#  Affichage
# ---------------------------------------------------------------------------

def _clear():
    print("\033[2J\033[H", end="", flush=True)

def _color(header: str, text: str) -> str:
    return f"{HEADER_COLOR.get(header, _R)}{text}{_R}"

def _fmt_byte(val: int, changed: bool) -> str:
    s = f"{val:02X}"
    if changed:
        return f"{_RD}{_B}{s}{_R}"
    return f"{_DIM}{s}{_R}"

def _print_frame(frame: Frame, prev: Optional[Frame], label: str = "") -> None:
    changes = diff(prev, frame) if prev else {}
    header_str = _color(frame.name, f"[{frame.name}]")
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{_DIM}{ts}{_R} {header_str} {_DIM}{label}{_R}")

    # Affichage hex avec bytes changés en rouge
    hex_parts = []
    for i, b in enumerate(frame.raw):
        hex_parts.append(_fmt_byte(b, i in changes))
        if (i + 1) % 16 == 0:
            hex_parts.append("\n         ")

    print("         " + " ".join(hex_parts))

    # Décodage des champs connus
    if isinstance(frame, DDFrame):
        print(f"         {_GR}Eau: {frame.water_temp:.1f}°C  "
              f"Air: {frame.air_temp}°C  "
              f"Mode: 0x{frame.mode_byte:02X}{_R}")
    elif isinstance(frame, CDFrame):
        print(f"         {_YL}Consigne: {frame.setpoint}°C{_R}")

    # Résumé des bytes changés
    if changes:
        changed_list = ", ".join(
            f"[{i}]: {_DIM}0x{v[0]:02X}{_R}→{_RD}{_B}0x{v[1]:02X}{_R}"
            for i, v in sorted(changes.items())
            if i not in (0, 79)  # ignorer header et fin de trame
        )
        if changed_list:
            print(f"         {_RD}Δ {changed_list}{_R}")
    print()


def _print_event(label: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"\n{_INV}  ▶ ACTION [{ts}] : {label}  {_R}\n")


# ---------------------------------------------------------------------------
#  Session d'analyse
# ---------------------------------------------------------------------------

class AnalysisSession:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        self._last_ids: dict[str, int] = {}
        self._history: dict[str, deque[Frame]] = {
            h: deque(maxlen=HISTORY_LEN) for h in ("DD", "D2", "CC", "CD")
        }
        self.events: list[dict] = []          # {ts, label, frames_snapshot}
        self.correlations: dict[str, list] = {}  # label → bytes changés

    def _fetch_new(self) -> list[Frame]:
        """Lit les nouvelles trames depuis la DB."""
        try:
            conn = sqlite3.connect(self.db_path)
            frames = []
            for header in ("DD", "D2", "CC", "CD"):
                last_id = self._last_ids.get(header, 0)
                rows = conn.execute(
                    "SELECT id, raw FROM frames WHERE header=? AND id>? "
                    "ORDER BY id LIMIT 10",
                    (header, last_id),
                ).fetchall()
                for row_id, raw in rows:
                    frame = decode(raw)
                    if frame:
                        frames.append(frame)
                        self._last_ids[header] = row_id
            conn.close()
            return frames
        except sqlite3.OperationalError:
            return []

    def _init_last_ids(self) -> None:
        """Initialise les IDs au point courant (ignore l'historique)."""
        try:
            conn = sqlite3.connect(self.db_path)
            for header in ("DD", "D2", "CC", "CD"):
                row = conn.execute(
                    "SELECT MAX(id) FROM frames WHERE header=?", (header,)
                ).fetchone()
                self._last_ids[header] = row[0] or 0
            conn.close()
        except sqlite3.OperationalError:
            pass

    def mark_event(self, label: str) -> None:
        """Enregistre un événement avec snapshot des dernières trames."""
        snapshot = {h: list(q)[-1] if q else None
                    for h, q in self._history.items()}
        self.events.append({
            "ts": datetime.now().isoformat(),
            "label": label,
            "snapshot": snapshot,
        })
        _print_event(label)

    def run(self) -> None:
        _clear()
        print(f"{_B}=== Poolex — Analyseur interactif ==={_R}\n")
        print(f"DB : {self.db_path}")

        # Attendre que la DB soit disponible
        while not os.path.exists(self.db_path):
            print(f"{_YL}En attente de la base de données...{_R}", end="\r")
            time.sleep(2)

        self._init_last_ids()

        print(f"\n{_GR}Capture démarrée.{_R}")
        print(f"{_DIM}Appuyez sur {_R}{_B}Entrée{_R}{_DIM} pour marquer une action.{_R}")
        print(f"{_DIM}Tapez {_R}{_B}q{_R}{_DIM} + Entrée pour quitter et afficher le rapport.{_R}\n")

        import threading
        stop_event = threading.Event()

        def _input_loop():
            while not stop_event.is_set():
                try:
                    label = input()
                    if label.lower() == "q":
                        stop_event.set()
                    elif label:
                        self.mark_event(label)
                    else:
                        self.mark_event("action (sans label)")
                except EOFError:
                    stop_event.set()

        input_thread = threading.Thread(target=_input_loop, daemon=True)
        input_thread.start()

        while not stop_event.is_set():
            new_frames = self._fetch_new()
            for frame in new_frames:
                prev = self._history[frame.name][-1] if self._history[frame.name] else None
                self._history[frame.name].append(frame)
                _print_frame(frame, prev)
            time.sleep(POLL_INTERVAL)

        self._print_report()

    def _print_report(self) -> None:
        print(f"\n{_B}{'='*60}{_R}")
        print(f"{_B}RAPPORT DE SESSION{_R}")
        print(f"{_B}{'='*60}{_R}\n")

        if not self.events:
            print("Aucun événement enregistré.")
            return

        # Pour chaque événement, chercher les trames qui ont changé
        # dans les N secondes suivant l'action
        for evt in self.events:
            print(f"{_YL}▶ {evt['label']}{_R}  {_DIM}({evt['ts']}){_R}")
            snap = evt["snapshot"]
            for header, frame in snap.items():
                if frame:
                    hist = self._history.get(header)
                    if hist:
                        latest = list(hist)[-1]
                        changes = diff(frame, latest)
                        significant = {
                            i: v for i, v in changes.items()
                            if i not in (0, 79)
                        }
                        if significant:
                            changes_str = ", ".join(
                                f"byte[{i}]: 0x{v[0]:02X}→0x{v[1]:02X}"
                                for i, v in sorted(significant.items())
                            )
                            print(f"  {_color(header, header)} Δ {changes_str}")
            print()


if __name__ == "__main__":
    session = AnalysisSession()
    session.run()

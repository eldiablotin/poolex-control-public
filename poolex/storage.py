"""
Stockage SQLite des trames RS485.

Schéma : table unique avec la trame brute en BLOB.
Beaucoup plus efficace que le schéma normalisé (1 ligne / trame vs 80 lignes).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from .decoder import Frame, decode

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL    NOT NULL,
    header    TEXT    NOT NULL,
    raw       BLOB    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frames_ts     ON frames (timestamp);
CREATE INDEX IF NOT EXISTS idx_frames_header ON frames (header, timestamp);
"""


class Storage:
    def __init__(self, db_path: str = "/var/lib/poolex/poolex.db") -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self._init()

    # ------------------------------------------------------------------ #
    #  Interne                                                             #
    # ------------------------------------------------------------------ #

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
        logger.info("DB initialisée : %s", self.db_path)

    # ------------------------------------------------------------------ #
    #  Écriture                                                            #
    # ------------------------------------------------------------------ #

    def save(self, frame: Frame) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO frames (timestamp, header, raw) VALUES (?, ?, ?)",
                (time.time(), frame.name, bytes(frame.raw)),
            )

    # ------------------------------------------------------------------ #
    #  Lecture                                                             #
    # ------------------------------------------------------------------ #

    def last(self, header: str) -> Optional[Frame]:
        """Retourne la dernière trame du type demandé."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT raw FROM frames WHERE header = ? ORDER BY timestamp DESC LIMIT 1",
                (header,),
            ).fetchone()
        return decode(row[0]) if row else None

    def recent(
        self, header: Optional[str] = None, limit: int = 20
    ) -> list[Frame]:
        """Retourne les N dernières trames (optionnellement filtrées)."""
        with self._conn() as conn:
            if header:
                rows = conn.execute(
                    "SELECT raw FROM frames WHERE header = ? ORDER BY timestamp DESC LIMIT ?",
                    (header, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT raw FROM frames ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [f for row in rows if (f := decode(row[0])) is not None]

    def stats(self) -> dict[str, int]:
        """Retourne le nombre de trames par type."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT header, COUNT(*) FROM frames GROUP BY header"
            ).fetchall()
        return dict(rows)

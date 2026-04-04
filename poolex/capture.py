"""
Capture temps réel des trames RS485 via adaptateur USB (Waveshare FT232RNL).

Branchement :
  Adaptateur TX_A → borne A+ du bus RS485 PAC
  Adaptateur RX_B → borne B- du bus RS485 PAC
  Switch 120Ω     → OFF (on se branche en parallèle, pas en terminaison)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import serial

from .decoder import FRAME_SIZE, HEADERS, Frame, decode

logger = logging.getLogger(__name__)

# Timeout inter-octet : à 9600 baud 1 octet ≈ 1 ms, donc 50 ms détecte
# une coupure de trame sans ambiguïté.
INTER_BYTE_TIMEOUT = 0.05  # secondes
PORT_RETRY_DELAY   = 10    # secondes entre deux tentatives d'ouverture du port


class RS485Capture:
    """Capture en continu les trames RS485 sur un port série USB."""

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 9600,
        on_frame: Optional[Callable[[Frame], None]] = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.on_frame = on_frame
        self._serial: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------ #
    #  Cycle de vie                                                        #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Démarre le thread de capture. Ne plante pas si le port est absent :
        le thread réessaiera toutes les PORT_RETRY_DELAY secondes."""
        self._running = True
        self._thread = threading.Thread(
            target=self._read_loop, daemon=True, name="rs485-capture"
        )
        self._thread.start()
        logger.info("Thread de capture démarré (port cible : %s)", self.port)

    def _open_port(self) -> bool:
        """Tente d'ouvrir le port série. Retourne True si succès."""
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=INTER_BYTE_TIMEOUT,
            )
            logger.info("Port %s ouvert à %d baud", self.port, self.baudrate)
            return True
        except serial.SerialException as e:
            logger.warning(
                "Port %s indisponible, nouvelle tentative dans %ds : %s",
                self.port, PORT_RETRY_DELAY, e,
            )
            return False

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        if self._serial and self._serial.is_open:
            self._serial.close()
        logger.info("Capture arrêtée")

    # ------------------------------------------------------------------ #
    #  Émission                                                            #
    # ------------------------------------------------------------------ #

    def send(self, data: bytes) -> None:
        """
        Envoie des données sur le bus RS485.

        L'adaptateur Waveshare (FT232RNL) gère le basculement DE/RE
        automatiquement via RTS. Après émission, on attend la fin de
        la transmission et on vide le buffer de réception pour éliminer
        l'éventuel écho half-duplex.
        """
        if not self._serial or not self._serial.is_open:
            raise RuntimeError("Port série non ouvert")

        self._serial.write(data)
        self._serial.flush()

        # Attendre la fin physique de la transmission
        # 10 bits/octet (8N1 + start + stop) à 9600 baud ≈ 1.04 ms/octet
        tx_duration = len(data) * 10 / self.baudrate
        time.sleep(tx_duration + 0.005)  # +5 ms de marge

        # Ne pas vider le buffer RX : l'écho half-duplex est capturé
        # par le read_loop comme confirmation d'envoi.

    # ------------------------------------------------------------------ #
    #  Boucle de lecture                                                   #
    # ------------------------------------------------------------------ #

    def _read_loop(self) -> None:
        buf = bytearray()
        in_frame = False

        while self._running:

            # --- Ouverture du port (avec retry si absent) ---
            if not self._serial or not self._serial.is_open:
                if not self._open_port():
                    time.sleep(PORT_RETRY_DELAY)
                    continue
                buf.clear()
                in_frame = False

            # --- Lecture d'un octet ---
            try:
                byte = self._serial.read(1)
            except serial.SerialException as e:
                logger.error("Erreur série, reconnexion : %s", e)
                self._serial.close()
                self._serial = None
                buf.clear()
                in_frame = False
                continue

            if not byte:
                # Timeout inter-octet → trame incomplète abandonnée
                if in_frame and buf:
                    logger.debug(
                        "Trame incomplète abandonnée (%d octets, header=0x%02X)",
                        len(buf), buf[0],
                    )
                    buf.clear()
                    in_frame = False
                continue

            b = byte[0]

            if not in_frame:
                if b in HEADERS:
                    buf.clear()
                    buf.append(b)
                    in_frame = True
            else:
                buf.append(b)
                if len(buf) == FRAME_SIZE:
                    frame = decode(bytes(buf))
                    if frame:
                        if self.on_frame:
                            self.on_frame(frame)
                    else:
                        logger.debug(
                            "Trame invalide rejetée (header=0x%02X, end=0x%02X)",
                            buf[0], buf[79],
                        )
                    buf.clear()
                    in_frame = False

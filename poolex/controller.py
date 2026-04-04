"""
Contrôle de la PAC par substitution complète de la télécommande filaire.

La télécommande filaire doit être débranchée. Le RPi envoie D2 et CC
en continu à ~1/s, exactement comme le faisait la télécommande.

Protocole confirmé avr 2026 :
  D2/CC/CD byte[11]        → consigne température °C
  D2/CC/CD byte[1] bit 0   → état on/off  (0=arrêt, 1=allumage)
  DD byte[3]               → état PAC en retour (0xa1=chauffe, 0x21=standby,
                             0x20=arrêt en cours, 0x00=éteint)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .capture import RS485Capture
from .decoder import Frame

logger = logging.getLogger(__name__)

SETPOINT_MIN = 10
SETPOINT_MAX = 40

# Intervalle d'envoi D2/CC (la télécommande émet ~1 trame/s par type)
_SEND_INTERVAL = 1.0


class Controller:
    """
    Remplace la télécommande filaire sur le bus RS485.

    Envoie D2 et CC en continu à 1/s avec les consignes courantes.
    """

    def __init__(self, capture: RS485Capture) -> None:
        self._capture = capture
        self._lock = threading.Lock()

        # Templates D2 et CC (capturés depuis le bus au démarrage)
        self._d2_template: Optional[bytearray] = None
        self._cc_template: Optional[bytearray] = None

        # État courant
        self._setpoint: Optional[int] = None
        self._power: Optional[bool] = None  # True=on, False=off

        # Intercepte les trames D2/CC pour les capturer comme templates
        _prev = capture.on_frame

        def _intercept(frame: Frame) -> None:
            with self._lock:
                if frame.raw[0] == 0xD2 and self._d2_template is None:
                    self._d2_template = bytearray(frame.raw)
                    self._setpoint = frame.raw[11]
                    self._power = bool(frame.raw[1] & 0x01)
                    logger.info(
                        "Template D2 capturé: consigne=%d°C power=%s b1=0x%02x",
                        self._setpoint, self._power, frame.raw[1]
                    )
                elif frame.raw[0] == 0xCC and self._cc_template is None:
                    self._cc_template = bytearray(frame.raw)
                    logger.info("Template CC capturé")
            if _prev:
                _prev(frame)

        capture.on_frame = _intercept

        # Thread d'émission continu
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    #  Cycle de vie                                                        #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Démarre le thread d'émission (attend que les templates soient prêts)."""
        self._running = True
        self._thread = threading.Thread(
            target=self._send_loop, daemon=True, name="rs485-controller"
        )
        self._thread.start()
        logger.info("Contrôleur démarré (en attente des templates D2/CC)")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def ready(self) -> bool:
        """True quand les templates D2 et CC ont été capturés."""
        return self._d2_template is not None and self._cc_template is not None

    @property
    def setpoint(self) -> Optional[int]:
        with self._lock:
            return self._setpoint

    @property
    def power(self) -> Optional[bool]:
        with self._lock:
            return self._power

    # ------------------------------------------------------------------ #
    #  Commandes                                                           #
    # ------------------------------------------------------------------ #

    def set_temperature(self, temperature: int) -> bool:
        """Change la consigne de température (10–40°C)."""
        if not (SETPOINT_MIN <= temperature <= SETPOINT_MAX):
            raise ValueError(
                f"Température {temperature}°C hors plage ({SETPOINT_MIN}-{SETPOINT_MAX}°C)"
            )
        with self._lock:
            if not self.ready:
                return False
            self._setpoint = temperature
            self._d2_template[11] = temperature
            self._cc_template[11] = temperature
        logger.info("Consigne mise à jour: %d°C", temperature)
        return True

    def set_power(self, on: bool) -> bool:
        """Allume (True) ou éteint (False) la PAC."""
        with self._lock:
            if not self.ready:
                return False
            self._power = on
            if on:
                self._d2_template[1] |= 0x01   # bit 0 = 1 → allumage
                self._cc_template[1] |= 0x01
            else:
                self._d2_template[1] &= 0xFE   # bit 0 = 0 → arrêt
                self._cc_template[1] &= 0xFE
        logger.info("Commande power: %s", "ON" if on else "OFF")
        return True

    # ------------------------------------------------------------------ #
    #  Boucle d'émission                                                  #
    # ------------------------------------------------------------------ #

    def _send_loop(self) -> None:
        while self._running:
            with self._lock:
                ready = self.ready
                d2 = bytes(self._d2_template) if self._d2_template else None
                cc = bytes(self._cc_template) if self._cc_template else None

            if not ready:
                time.sleep(0.5)
                continue

            try:
                self._capture.send(d2)
                time.sleep(_SEND_INTERVAL / 2)
                self._capture.send(cc)
                time.sleep(_SEND_INTERVAL / 2)
            except Exception:
                logger.exception("Erreur lors de l'envoi des trames de contrôle")
                time.sleep(1)

"""
Contrôle de la PAC par injection de trames CD modifiées.

Stratégie (identique à celle validée sur ESP32 dans PoolexCommand) :
  1. Écouter passivement le bus et mémoriser la dernière trame CD reçue
     comme template (elle contient tous les réglages courants).
  2. Lors d'une demande de changement de consigne :
     - Copier le template
     - Modifier byte[11] = nouvelle température
     - Injecter la trame sur le bus après un délai inter-trame

Octet de contrôle identifié :
  CD byte[11] → consigne température °C (confirmé par analyse + ESP32)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .capture import RS485Capture
from .decoder import CDFrame, Frame

logger = logging.getLogger(__name__)

# Délai avant émission pour s'assurer que le bus est calme
# (intervalle inter-trame observé ≈ 333 ms, on attend 30 ms après réception)
_TRANSMIT_DELAY = 0.03  # secondes

SETPOINT_MIN = 10
SETPOINT_MAX = 40


class Controller:
    """Contrôle la PAC en injectant des trames CD modifiées."""

    def __init__(self, capture: RS485Capture) -> None:
        self._capture = capture
        self._last_cd: Optional[CDFrame] = None
        self._lock = threading.Lock()

        # Chaîner avec le callback existant pour intercepter les trames CD
        _prev = capture.on_frame

        def _intercept(frame: Frame) -> None:
            if isinstance(frame, CDFrame):
                with self._lock:
                    self._last_cd = frame
                logger.debug("Template CD mis à jour : consigne=%d°C", frame.setpoint)
            if _prev:
                _prev(frame)

        capture.on_frame = _intercept

    # ------------------------------------------------------------------ #
    #  État                                                                #
    # ------------------------------------------------------------------ #

    @property
    def has_template(self) -> bool:
        return self._last_cd is not None

    @property
    def current_setpoint(self) -> Optional[int]:
        with self._lock:
            return self._last_cd.setpoint if self._last_cd else None

    # ------------------------------------------------------------------ #
    #  Commande                                                            #
    # ------------------------------------------------------------------ #

    def set_temperature(self, temperature: int) -> bool:
        """
        Envoie une trame CD modifiée pour changer la consigne.

        Returns:
            True  si la trame a été envoyée.
            False si aucune trame CD n'a encore été reçue (pas de template).

        Raises:
            ValueError si la température est hors plage.
        """
        if not (SETPOINT_MIN <= temperature <= SETPOINT_MAX):
            raise ValueError(
                f"Température {temperature}°C hors plage "
                f"({SETPOINT_MIN}-{SETPOINT_MAX}°C)"
            )

        with self._lock:
            if self._last_cd is None:
                logger.warning(
                    "Aucune trame CD reçue — impossible d'envoyer la consigne"
                )
                return False
            frame = bytearray(self._last_cd.raw)

        # Modifier uniquement la consigne (byte[11])
        frame[11] = temperature

        time.sleep(_TRANSMIT_DELAY)
        try:
            self._capture.send(bytes(frame))
            logger.info("Consigne envoyée : %d°C", temperature)
            return True
        except Exception:
            logger.exception("Erreur lors de l'envoi de la consigne")
            return False

"""
Contrôle de la PAC via protocole RS485 réactif.

Protocole confirmé avr 2026 :
  PAC → remote : D2 (~1/s) — config courante (setpoint, power, mode)
                 DD (~1/s) — statut live (températures, état)
  Remote → PAC : CC — réponse keepalive après chaque D2 (miroir du D2 reçu)
                 CD — commande de changement → 8 cycles pour fiabilité

  D2/CD byte[11]      → consigne température °C
  D2/CD byte[1] bit 0 → on/off  (0=arrêt, 1=allumage)
  D2/CD byte[1] + byte[4] → mode :
      0x5b / 0x01 = inverter
      0x3b / 0x01 = fix
      0x1b / 0x01 = sun
      0x1b / 0x02 = cooling (refroidissement)
  byte[79]            → checksum : (sum(bytes[0..78]) + 0xAF) & 0xFF
  DD byte[3]          → état PAC (0xa1=chauffe, 0x21=standby/marche,
                        0x20=arrêt en cours, 0x00=éteint)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

from .capture import RS485Capture
from .decoder import Frame

if TYPE_CHECKING:
    from .storage import Storage

logger = logging.getLogger(__name__)

SETPOINT_MIN = 8
SETPOINT_MAX = 40

# Modes de chauffe : (byte[1], byte[4])
MODES: dict[str, tuple[int, int]] = {
    "inverter": (0x5B, 0x01),
    "fix":      (0x3B, 0x01),
    "sun":      (0x1B, 0x01),
    "cooling":  (0x1B, 0x02),
}

# Délai entre la réception de D2 et l'envoi de CC (laisser le bus se stabiliser)
_CC_DELAY = 0.05   # 50 ms
# Nombre de répétitions CD pour s'assurer que la commande passe
_CD_REPEAT = 3
_CD_GAP    = 0.05  # 50 ms entre chaque CD


class Controller:
    """
    Contrôle la PAC en répondant aux trames D2 avec CC (keepalive)
    et en envoyant des trames CD pour les changements de configuration.
    """

    def __init__(self, capture: RS485Capture, storage: "Storage | None" = None) -> None:
        self._capture = capture
        self._lock = threading.Lock()

        # Template courant (appris depuis D2 de la PAC ou chargé depuis DB)
        self._template: Optional[bytearray] = None

        # État courant
        self._setpoint: Optional[int] = None
        self._power: Optional[bool] = None  # True=on, False=off
        self._mode: Optional[str] = None

        # Commande en attente d'envoi + compteur de répétitions restantes
        self._pending_cmd: Optional[bytearray] = None
        self._pending_repeats: int = 0

        # Intercepte les trames du bus
        _prev = capture.on_frame

        def _intercept(frame: Frame) -> None:
            with self._lock:
                if frame.raw[0] == 0xD2:
                    # Met à jour le template depuis la PAC
                    self._template = bytearray(frame.raw)
                    self._setpoint = frame.raw[11]
                    self._power = bool(frame.raw[1] & 0x01)
                    self._mode = self._decode_mode(frame.raw[1], frame.raw[4])
                    # Signale pour répondre avec CC
                    self._d2_event.set()
            if _prev:
                _prev(frame)

        capture.on_frame = _intercept

        # Amorçage depuis la DB si disponible (pour démarrer sans attendre le D2)
        if storage is not None:
            d2 = storage.last("D2")
            if d2 is not None:
                self._template = bytearray(d2.raw)
                self._setpoint = d2.raw[11]
                self._power = bool(d2.raw[1] & 0x01)
                self._mode = self._decode_mode(d2.raw[1], d2.raw[4])
                logger.info(
                    "Template chargé depuis DB: consigne=%d°C power=%s mode=%s b1=0x%02x",
                    self._setpoint, self._power, self._mode, d2.raw[1],
                )

        # Event : déclenche CC après D2 reçu
        self._d2_event = threading.Event()

        # Thread de contrôle
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    #  Cycle de vie                                                        #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._control_loop, daemon=True, name="rs485-controller"
        )
        self._thread.start()
        logger.info("Contrôleur démarré")

    def stop(self) -> None:
        self._running = False
        self._d2_event.set()   # débloquer l'attente
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def ready(self) -> bool:
        return self._template is not None

    @property
    def setpoint(self) -> Optional[int]:
        with self._lock:
            return self._setpoint

    @property
    def power(self) -> Optional[bool]:
        with self._lock:
            return self._power

    @property
    def mode(self) -> Optional[str]:
        with self._lock:
            return self._mode

    # ------------------------------------------------------------------ #
    #  Commandes                                                           #
    # ------------------------------------------------------------------ #

    def set_temperature(self, temperature: int) -> bool:
        """Change la consigne de température (8–40°C)."""
        if not (SETPOINT_MIN <= temperature <= SETPOINT_MAX):
            raise ValueError(
                f"Température {temperature}°C hors plage ({SETPOINT_MIN}-{SETPOINT_MAX}°C)"
            )
        with self._lock:
            if not self.ready:
                return False
            self._setpoint = temperature
            self._template[11] = temperature
            self._pending_cmd = self._make_cd(self._template)
            self._pending_repeats = 8   # 8 cycles D2 (~8s) pour s'assurer que la commande passe
        logger.info("Consigne mise à jour: %d°C", temperature)
        return True

    def set_mode(self, mode: str) -> bool:
        """Change le mode de chauffe (inverter/fix/sun/cooling)."""
        if mode not in MODES:
            raise ValueError(f"Mode '{mode}' inconnu. Valeurs : {list(MODES)}")
        with self._lock:
            if not self.ready:
                return False
            b1, b4 = MODES[mode]
            # Préserver le bit on/off dans b1
            if self._template[1] & 0x01:
                b1 |= 0x01
            else:
                b1 &= 0xFE
            self._template[1] = b1
            self._template[4] = b4
            self._mode = mode
            self._pending_cmd = self._make_cd(self._template)
            self._pending_repeats = 8
        logger.info("Mode mis à jour: %s (b1=0x%02x b4=0x%02x)", mode, b1, b4)
        return True

    def set_power(self, on: bool) -> bool:
        """Allume (True) ou éteint (False) la PAC."""
        with self._lock:
            if not self.ready:
                return False
            self._power = on
            if on:
                self._template[1] |= 0x01
            else:
                self._template[1] &= 0xFE
            self._pending_cmd = self._make_cd(self._template)
            self._pending_repeats = 8
        logger.info("Commande power: %s", "ON" if on else "OFF")
        return True

    # ------------------------------------------------------------------ #
    #  Interne                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _decode_mode(b1: int, b4: int) -> str:
        """Retourne le nom du mode depuis byte[1] et byte[4]."""
        for name, (mb1, mb4) in MODES.items():
            if (b1 & 0xFE) == (mb1 & 0xFE) and b4 == mb4:
                return name
        return f"unknown(b1=0x{b1:02x},b4=0x{b4:02x})"

    @staticmethod
    def _checksum(frame: bytearray) -> int:
        """Calcule byte[79] : (sum(bytes[0..78]) + 0xaf) & 0xFF."""
        return (sum(frame[:79]) + 0xAF) & 0xFF

    def _make_cc(self, d2_frame: bytearray) -> bytes:
        """Construit un CC à partir d'un D2 (même contenu, header/checksum recalculés)."""
        cc = bytearray(d2_frame)
        cc[0]  = 0xCC
        cc[79] = self._checksum(cc)
        return bytes(cc)

    def _make_cd(self, template: bytearray) -> bytearray:
        """Construit un CD de commande à partir du template courant."""
        cd = bytearray(template)
        cd[0]  = 0xCD
        cd[79] = self._checksum(cd)
        return cd

    # ------------------------------------------------------------------ #
    #  Boucle de contrôle                                                  #
    # ------------------------------------------------------------------ #

    def _control_loop(self) -> None:
        while self._running:
            # Attendre un D2 de la PAC (timeout 2s)
            self._d2_event.wait(timeout=2.0)
            self._d2_event.clear()

            if not self._running:
                break

            with self._lock:
                if not self.ready:
                    continue
                cc = self._make_cc(self._template)
                cmd = bytes(self._pending_cmd) if self._pending_cmd else None
                if self._pending_repeats > 0:
                    self._pending_repeats -= 1
                    if self._pending_repeats == 0:
                        self._pending_cmd = None

            try:
                # Répondre avec CC (keepalive / confirmation)
                time.sleep(_CC_DELAY)
                self._capture.send(cc)

                # Si commande active, envoyer CD une fois par cycle D2
                if cmd is not None:
                    time.sleep(_CD_GAP)
                    self._capture.send(cmd)
                    logger.debug(
                        "CD envoyé (reste %d): b[1]=0x%02x b[11]=%d",
                        self._pending_repeats, cmd[1], cmd[11],
                    )
            except Exception:
                logger.exception("Erreur lors de l'envoi des trames de contrôle")

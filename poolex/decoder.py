"""
Décodage des trames RS485 de la pompe à chaleur (80 octets, 9600 baud 8N1).

Rôles des trames — confirmés avr 2026 :
  0xDD  PAC → remote  : statut live (températures, état)        ~1/s
  0xD2  PAC → remote  : config broadcast (consigne, mode, power) ~1/s  ← maître bus
  0xCC  remote → PAC  : keepalive/ACK en réponse à chaque D2
  0xCD  remote → PAC  : commande (setpoint, power, mode)        burst x8

Décodages confirmés avr 2026 :
  DD byte[29] / 10 → température eau piscine °C  (ex: 114 → 11.4°C)
  DD byte[20] / 2  → température air extérieur °C (ex:  26 → 13.0°C)
  DD byte[3]       → état PAC (0xa1=chauffe, 0x21=standby, 0x20=arrêt, 0x00=éteint)
  D2/CD byte[11]   → consigne température °C
  D2/CD byte[1]    → mode + power  (bit0: 0=off 1=on ; bits4-6: 0x1b/0x3b/0x5b=mode)
  D2/CD byte[4]    → sous-mode     (0x01=normal, 0x02=cooling)

  Checksum byte[79] = (sum(bytes[0..78]) + 0xAF) & 0xFF  — toujours recalculer après modif.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

FRAME_SIZE = 80
HEADERS = frozenset({0xDD, 0xD2, 0xCC, 0xCD})
HEADER_NAMES: dict[int, str] = {0xDD: "DD", 0xD2: "D2", 0xCC: "CC", 0xCD: "CD"}


@dataclass
class Frame:
    header: int
    raw: bytes

    @property
    def name(self) -> str:
        return HEADER_NAMES.get(self.header, f"0x{self.header:02X}")

    @property
    def is_valid(self) -> bool:
        if len(self.raw) != FRAME_SIZE or self.raw[0] != self.header:
            return False
        match self.header:
            case 0xD2 | 0xCC | 0xCD | 0xDD:
                return True  # byte[79] varie selon le type (compteur/checksum) — pas de marqueur fixe fiable
        return False


@dataclass
class DDFrame(Frame):
    """Trame statut PAC → télécommande (données capteurs temps réel)."""
    water_temp: float   # byte[29] / 10  (ex: 114 → 11.4°C) ✓ avr 2026
    air_temp: float     # byte[20] / 2   (ex:  26 → 13.0°C) ✓ avr 2026
    mode_byte: int      # byte[3]        mode de fonctionnement (à décoder)

    @classmethod
    def from_raw(cls, raw: bytes) -> DDFrame:
        return cls(
            header=0xDD,
            raw=raw,
            water_temp=raw[29] / 10.0,
            air_temp=raw[20] / 2.0,
            mode_byte=raw[3],
        )


@dataclass
class CDFrame(Frame):
    """Trame commande télécommande → PAC (consigne température)."""
    setpoint: int   # byte[11] consigne en °C

    @classmethod
    def from_raw(cls, raw: bytes) -> CDFrame:
        return cls(header=0xCD, raw=raw, setpoint=raw[11])


def decode(raw: bytes) -> Optional[Frame]:
    """Décode une trame brute de 80 octets. Retourne None si invalide."""
    if len(raw) != FRAME_SIZE or raw[0] not in HEADERS:
        return None
    frame = Frame(header=raw[0], raw=raw)
    if not frame.is_valid:
        return None
    match raw[0]:
        case 0xDD:
            return DDFrame.from_raw(raw)
        case 0xCD:
            return CDFrame.from_raw(raw)
        case _:
            return frame  # D2 / CC : trame brute (configuration, pas encore décodée)


def diff(a: Frame, b: Frame) -> dict[int, tuple[int, int]]:
    """Retourne les bytes qui diffèrent entre deux trames : {index: (val_a, val_b)}."""
    if len(a.raw) != FRAME_SIZE or len(b.raw) != FRAME_SIZE:
        return {}
    return {
        i: (a.raw[i], b.raw[i])
        for i in range(FRAME_SIZE)
        if a.raw[i] != b.raw[i]
    }

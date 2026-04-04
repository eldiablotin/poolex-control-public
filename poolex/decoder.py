"""
Décodage des trames RS485 de la pompe à chaleur.

Structure des trames (80 octets, 9600 baud 8N1) :
  Byte[0]  : header (0xDD / 0xD2 / 0xCC / 0xCD)
  Byte[79] : marqueur de fin = header répété (sauf DD où c'est un compteur)

Trames identifiées :
  0xDD  PAC → télécommande  : données capteurs temps réel
  0xD2  télécommande → PAC  : configuration / consignes (appareil A)
  0xCC  télécommande → PAC  : configuration / consignes (appareil B, contenu identique à D2)
  0xCD  télécommande → PAC  : trame de commande (rare, modification consigne)

Décodages confirmés :
  DD byte[29] / 10 → température eau piscine (°C)   ex: 114 → 11.4°C  ✓ avr 2026
  DD byte[20] / 2  → température air extérieur (°C) ex:  26 → 13.0°C  ✓ avr 2026
  CD byte[11]      → consigne température (°C)                         ✓ été 2025

Bytes à préciser :
  DD byte[22] / 2  → valeur proche temp eau (2ème capteur ? eau de sortie ?)
  DD byte[3]       → mode de fonctionnement (flags, décodage en cours)

Note : l'analyse été 2025 (23 452 trames) avait identifié byte[22]/2 → eau et byte[29] → air,
mais les validations terrain avr 2026 montrent byte[29]/10 → eau et byte[20]/2 → air.
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
            case 0xD2 | 0xCC:
                return self.raw[79] == self.header
            case 0xCD:
                # byte[79] observé à 0xCD ou 0xCE selon les trames capturées
                return self.raw[79] in (0xCD, 0xCE)
            case 0xDD:
                return True  # byte[79] = compteur roulant, pas de marqueur fixe
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

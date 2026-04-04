from unittest.mock import MagicMock

import pytest

from poolex.controller import SETPOINT_MAX, SETPOINT_MIN, Controller
from poolex.decoder import FRAME_SIZE


def _make_d2(setpoint: int = 28, b1: int = 0x5B, b4: int = 0x01) -> bytes:
    """Construit une trame D2 minimale (PAC → remote, template de config)."""
    raw = bytearray(FRAME_SIZE)
    raw[0]  = 0xD2
    raw[1]  = b1       # mode + power bit
    raw[4]  = b4       # sous-mode
    raw[11] = setpoint
    # checksum correct
    raw[79] = (sum(raw[:79]) + 0xAF) & 0xFF
    return bytes(raw)


def _make_capture():
    cap = MagicMock()
    cap.on_frame = None
    return cap


class TestControllerReady:
    def test_no_template_returns_false(self):
        ctrl = Controller(_make_capture())
        assert not ctrl.ready
        assert ctrl.set_temperature(28) is False

    def test_setpoint_none_without_template(self):
        ctrl = Controller(_make_capture())
        assert ctrl.setpoint is None
        assert ctrl.power is None
        assert ctrl.mode is None

    def test_template_captured_on_d2_frame(self):
        """Un D2 entrant met à jour le template et les propriétés."""
        from poolex.decoder import Frame

        cap = _make_capture()
        ctrl = Controller(cap)

        # Simuler l'arrivée d'une trame D2 via le callback intercept
        d2_raw = _make_d2(setpoint=26, b1=0x5B, b4=0x01)
        frame = Frame(header=0xD2, raw=d2_raw)
        cap.on_frame(frame)

        assert ctrl.ready
        assert ctrl.setpoint == 26
        assert ctrl.power is True       # bit 0 de 0x5B = 1
        assert ctrl.mode == "inverter"

    def test_power_off_detected_from_d2(self):
        from poolex.decoder import Frame

        cap = _make_capture()
        ctrl = Controller(cap)
        d2_raw = _make_d2(b1=0x5A)   # bit 0 = 0 → power off
        cap.on_frame(Frame(header=0xD2, raw=d2_raw))
        assert ctrl.power is False


class TestSetTemperature:
    def _ctrl_with_template(self, setpoint=28):
        from poolex.decoder import Frame
        cap = _make_capture()
        ctrl = Controller(cap)
        cap.on_frame(Frame(header=0xD2, raw=_make_d2(setpoint=setpoint)))
        return ctrl

    def test_set_temperature_updates_state(self):
        ctrl = self._ctrl_with_template(28)
        result = ctrl.set_temperature(30)
        assert result is True
        assert ctrl.setpoint == 30

    def test_set_temperature_pending_cmd_is_cd(self):
        ctrl = self._ctrl_with_template(28)
        ctrl.set_temperature(30)
        assert ctrl._pending_cmd is not None
        assert ctrl._pending_cmd[0] == 0xCD
        assert ctrl._pending_cmd[11] == 30

    def test_set_temperature_checksum_correct(self):
        ctrl = self._ctrl_with_template(28)
        ctrl.set_temperature(30)
        cmd = ctrl._pending_cmd
        expected = (sum(cmd[:79]) + 0xAF) & 0xFF
        assert cmd[79] == expected

    def test_out_of_range_raises(self):
        ctrl = self._ctrl_with_template(28)
        with pytest.raises(ValueError):
            ctrl.set_temperature(SETPOINT_MAX + 1)
        with pytest.raises(ValueError):
            ctrl.set_temperature(SETPOINT_MIN - 1)

    def test_boundary_temperatures(self):
        ctrl = self._ctrl_with_template(28)
        assert ctrl.set_temperature(SETPOINT_MIN) is True
        assert ctrl.set_temperature(SETPOINT_MAX) is True

    def test_pending_repeats_set(self):
        ctrl = self._ctrl_with_template(28)
        ctrl.set_temperature(30)
        assert ctrl._pending_repeats == 8


class TestSetPower:
    def _ctrl(self):
        from poolex.decoder import Frame
        cap = _make_capture()
        ctrl = Controller(cap)
        cap.on_frame(Frame(header=0xD2, raw=_make_d2(b1=0x5B)))   # power on
        return ctrl

    def test_power_off_clears_bit0(self):
        ctrl = self._ctrl()
        ctrl.set_power(False)
        assert ctrl._pending_cmd is not None
        assert ctrl._pending_cmd[1] & 0x01 == 0

    def test_power_on_sets_bit0(self):
        from poolex.decoder import Frame
        cap = _make_capture()
        ctrl = Controller(cap)
        cap.on_frame(Frame(header=0xD2, raw=_make_d2(b1=0x5A)))   # power off
        ctrl.set_power(True)
        assert ctrl._pending_cmd[1] & 0x01 == 1

    def test_power_off_checksum(self):
        ctrl = self._ctrl()
        ctrl.set_power(False)
        cmd = ctrl._pending_cmd
        expected = (sum(cmd[:79]) + 0xAF) & 0xFF
        assert cmd[79] == expected


class TestSetMode:
    def _ctrl(self):
        from poolex.decoder import Frame
        cap = _make_capture()
        ctrl = Controller(cap)
        cap.on_frame(Frame(header=0xD2, raw=_make_d2(b1=0x5B, b4=0x01)))
        return ctrl

    @pytest.mark.parametrize("mode,b1_masked,b4", [
        ("inverter", 0x5A, 0x01),
        ("fix",      0x3A, 0x01),
        ("sun",      0x1A, 0x01),
        ("cooling",  0x1A, 0x02),
    ])
    def test_mode_bytes(self, mode, b1_masked, b4):
        ctrl = self._ctrl()
        ctrl.set_power(True)    # ensure power bit is set
        ctrl.set_mode(mode)
        cmd = ctrl._pending_cmd
        assert (cmd[1] & 0xFE) == b1_masked
        assert cmd[4] == b4
        assert ctrl.mode == mode

    def test_mode_preserves_power_bit_on(self):
        ctrl = self._ctrl()
        ctrl.set_power(True)
        ctrl.set_mode("fix")
        assert ctrl._pending_cmd[1] & 0x01 == 1

    def test_mode_preserves_power_bit_off(self):
        from poolex.decoder import Frame
        cap = _make_capture()
        ctrl = Controller(cap)
        cap.on_frame(Frame(header=0xD2, raw=_make_d2(b1=0x5A)))   # power off
        ctrl.set_mode("fix")
        assert ctrl._pending_cmd[1] & 0x01 == 0

    def test_unknown_mode_raises(self):
        ctrl = self._ctrl()
        with pytest.raises(ValueError):
            ctrl.set_mode("turbo")


class TestDecodeMode:
    @pytest.mark.parametrize("mode,b1,b4", [
        ("inverter", 0x5B, 0x01),
        ("inverter", 0x5A, 0x01),   # power off variant
        ("fix",      0x3B, 0x01),
        ("sun",      0x1B, 0x01),
        ("cooling",  0x1B, 0x02),
    ])
    def test_known_modes(self, mode, b1, b4):
        assert Controller._decode_mode(b1, b4) == mode

    def test_unknown_mode(self):
        result = Controller._decode_mode(0xFF, 0xFF)
        assert result.startswith("unknown")


class TestChecksum:
    def test_checksum_formula(self):
        frame = bytearray(FRAME_SIZE)
        frame[0] = 0xCD
        frame[11] = 28
        cs = Controller._checksum(frame)
        # vérifier manuellement
        assert cs == (sum(frame[:79]) + 0xAF) & 0xFF

    def test_checksum_changes_with_content(self):
        frame_a = bytearray(FRAME_SIZE)
        frame_a[11] = 28
        frame_b = bytearray(FRAME_SIZE)
        frame_b[11] = 29
        assert Controller._checksum(frame_a) != Controller._checksum(frame_b)

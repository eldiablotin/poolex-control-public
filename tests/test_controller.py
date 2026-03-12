from unittest.mock import MagicMock, patch

import pytest

from poolex.controller import SETPOINT_MAX, SETPOINT_MIN, Controller
from poolex.decoder import CDFrame, FRAME_SIZE


def _make_cd(setpoint: int = 28) -> CDFrame:
    raw = bytearray(FRAME_SIZE)
    raw[0] = 0xCD
    raw[11] = setpoint
    raw[79] = 0xCD
    return CDFrame(header=0xCD, raw=bytes(raw), setpoint=setpoint)


def _make_capture():
    cap = MagicMock()
    cap.on_frame = None
    return cap


class TestController:
    def test_no_template_returns_false(self):
        ctrl = Controller(_make_capture())
        assert not ctrl.has_template
        assert ctrl.set_temperature(28) is False

    def test_current_setpoint_none_without_template(self):
        ctrl = Controller(_make_capture())
        assert ctrl.current_setpoint is None

    def test_template_captured_on_cd_frame(self):
        cap = _make_capture()
        ctrl = Controller(cap)
        # Simuler l'arrivée d'une trame CD via le callback
        cap.on_frame(_make_cd(28))
        assert ctrl.has_template
        assert ctrl.current_setpoint == 28

    def test_set_temperature_sends_correct_byte(self):
        cap = _make_capture()
        ctrl = Controller(cap)
        cap.on_frame(_make_cd(28))

        with patch("time.sleep"):
            result = ctrl.set_temperature(30)

        assert result is True
        sent: bytes = cap.send.call_args[0][0]
        assert len(sent) == FRAME_SIZE
        assert sent[11] == 30       # nouvelle consigne
        assert sent[0] == 0xCD      # header préservé

    def test_template_byte_not_mutated(self):
        """Le template original ne doit pas être modifié après envoi."""
        cap = _make_capture()
        ctrl = Controller(cap)
        cap.on_frame(_make_cd(28))

        with patch("time.sleep"):
            ctrl.set_temperature(30)

        assert ctrl.current_setpoint == 28  # template inchangé

    def test_out_of_range_raises(self):
        cap = _make_capture()
        ctrl = Controller(cap)
        cap.on_frame(_make_cd(28))

        with pytest.raises(ValueError):
            ctrl.set_temperature(SETPOINT_MAX + 1)

        with pytest.raises(ValueError):
            ctrl.set_temperature(SETPOINT_MIN - 1)

    def test_boundary_temperatures(self):
        cap = _make_capture()
        ctrl = Controller(cap)
        cap.on_frame(_make_cd(28))

        with patch("time.sleep"):
            assert ctrl.set_temperature(SETPOINT_MIN) is True
            assert ctrl.set_temperature(SETPOINT_MAX) is True

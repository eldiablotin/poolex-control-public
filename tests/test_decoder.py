from poolex.decoder import (
    FRAME_SIZE,
    CDFrame,
    DDFrame,
    Frame,
    decode,
    diff,
)


def make_raw(header: int, overrides: dict[int, int] | None = None) -> bytes:
    """Crée une trame brute minimalement valide pour les tests."""
    raw = bytearray(FRAME_SIZE)
    raw[0] = header
    raw[79] = header
    if overrides:
        for idx, val in overrides.items():
            raw[idx] = val
    return bytes(raw)


class TestDecode:
    def test_dd_water_temp(self):
        # byte[22] = 56 → 28.0°C
        frame = decode(make_raw(0xDD, {22: 56, 29: 25, 3: 128}))
        assert isinstance(frame, DDFrame)
        assert frame.water_temp == 28.0

    def test_dd_air_temp(self):
        frame = decode(make_raw(0xDD, {29: 22}))
        assert isinstance(frame, DDFrame)
        assert frame.air_temp == 22

    def test_dd_water_temp_half_degree(self):
        # byte[22] = 57 → 28.5°C
        frame = decode(make_raw(0xDD, {22: 57}))
        assert isinstance(frame, DDFrame)
        assert frame.water_temp == 28.5

    def test_cd_setpoint(self):
        frame = decode(make_raw(0xCD, {11: 28}))
        assert isinstance(frame, CDFrame)
        assert frame.setpoint == 28

    def test_d2_frame(self):
        frame = decode(make_raw(0xD2))
        assert isinstance(frame, Frame)
        assert frame.name == "D2"

    def test_cc_frame(self):
        frame = decode(make_raw(0xCC))
        assert isinstance(frame, Frame)
        assert frame.name == "CC"

    def test_invalid_too_short(self):
        assert decode(bytes(FRAME_SIZE - 1)) is None

    def test_invalid_unknown_header(self):
        raw = bytearray(FRAME_SIZE)
        raw[0] = 0xAB
        assert decode(bytes(raw)) is None

    def test_d2_bad_end_byte(self):
        raw = bytearray(make_raw(0xD2))
        raw[79] = 0xFF
        assert decode(bytes(raw)) is None

    def test_cc_bad_end_byte(self):
        raw = bytearray(make_raw(0xCC))
        raw[79] = 0x00
        assert decode(bytes(raw)) is None

    def test_cd_end_byte_ce(self):
        # byte[79] = 0xCE est aussi valide pour CD (observé dans les captures)
        raw = bytearray(make_raw(0xCD))
        raw[79] = 0xCE
        frame = decode(bytes(raw))
        assert isinstance(frame, CDFrame)

    def test_frame_name(self):
        for header, name in [(0xDD, "DD"), (0xD2, "D2"), (0xCC, "CC"), (0xCD, "CD")]:
            frame = decode(make_raw(header))
            assert frame is not None
            assert frame.name == name


class TestDiff:
    def test_identical_frames(self):
        raw = make_raw(0xDD, {22: 56, 29: 25})
        f1 = decode(raw)
        f2 = decode(raw)
        assert diff(f1, f2) == {}

    def test_single_byte_diff(self):
        f1 = decode(make_raw(0xDD, {22: 56}))
        f2 = decode(make_raw(0xDD, {22: 58}))
        result = diff(f1, f2)
        assert result == {22: (56, 58)}

    def test_multiple_diffs(self):
        f1 = decode(make_raw(0xDD, {22: 56, 29: 20}))
        f2 = decode(make_raw(0xDD, {22: 58, 29: 22}))
        result = diff(f1, f2)
        assert 22 in result
        assert 29 in result
        assert result[22] == (56, 58)
        assert result[29] == (20, 22)

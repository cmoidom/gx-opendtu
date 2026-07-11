from src.grid_meter_modbus import _to_signed_int16


def test_positive_value_unchanged():
    assert _to_signed_int16(500) == 500


def test_zero_unchanged():
    assert _to_signed_int16(0) == 0


def test_max_positive_int16_unchanged():
    assert _to_signed_int16(32767) == 32767


def test_negative_value_converted_from_twos_complement():
    # -500 W (exporting) is stored as 65036 in the unsigned register pymodbus returns.
    assert _to_signed_int16(65036) == -500


def test_boundary_just_above_max_positive_is_negative():
    assert _to_signed_int16(32768) == -32768

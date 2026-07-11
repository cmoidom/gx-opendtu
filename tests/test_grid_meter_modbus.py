import pytest

from src.grid_meter_modbus import _read_holding_registers, _to_signed_int16


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


class _FakeClientAcceptingOnly:
    """Simulates a pymodbus client whose read_holding_registers only accepts
    one specific unit-id keyword, raising TypeError on any other -- exactly
    how real pymodbus behaves across its 2.x/3.x/3.8+ API variants."""

    def __init__(self, accepted_kwarg):
        self.accepted_kwarg = accepted_kwarg
        self.received = None

    def read_holding_registers(self, address, count, **kwargs):
        if self.accepted_kwarg not in kwargs or len(kwargs) != 1:
            raise TypeError(f"unexpected keyword argument {next(iter(kwargs))!r}")
        self.received = (address, count, kwargs[self.accepted_kwarg])
        return "ok"


@pytest.mark.parametrize("accepted_kwarg", ["device_id", "slave", "unit"])
def test_read_holding_registers_falls_back_across_pymodbus_versions(accepted_kwarg):
    client = _FakeClientAcceptingOnly(accepted_kwarg)
    result = _read_holding_registers(client, address=820, count=1, unit_id=100)
    assert result == "ok"
    assert client.received == (820, 1, 100)


def test_read_holding_registers_raises_if_no_keyword_is_accepted():
    client = _FakeClientAcceptingOnly("something_else")
    with pytest.raises(TypeError):
        _read_holding_registers(client, address=820, count=1, unit_id=100)

from src.battery_soc_modbus import POWER_REGISTER, SOC_REGISTER, ModbusBatterySoc


class _FakeResult:
    def __init__(self, value):
        self.registers = [value]

    def isError(self):
        return False


class _FakeModbusClient:
    def __init__(self, values):
        self.values = values  # {register: raw unsigned register value}
        self.connected = True

    def read_holding_registers(self, address, count, **kwargs):
        return _FakeResult(self.values[address])


def _make_reader(values):
    reader = ModbusBatterySoc(host="192.168.1.50")
    reader._client = _FakeModbusClient(values)  # bypass real pymodbus connection
    return reader


def test_read_soc_pct_unsigned():
    reader = _make_reader({SOC_REGISTER: 87})
    assert reader.read_soc_pct() == 87.0


def test_read_power_w_positive_is_charging():
    reader = _make_reader({POWER_REGISTER: 250})
    assert reader.read_power_w() == 250.0


def test_read_power_w_negative_is_discharging():
    # -300W (discharging) is stored as two's complement (65536 - 300) in the
    # unsigned register pymodbus returns.
    reader = _make_reader({POWER_REGISTER: 65236})
    assert reader.read_power_w() == -300.0

from src.grid_meter_modbus import ENERGY_FROM_NET_REGISTER, ENERGY_TO_NET_REGISTER, ModbusGridMeter


class _FakeResult:
    def __init__(self, registers):
        self.registers = registers

    def isError(self):
        return False


class _FakeModbusClient:
    def __init__(self, values):
        self.values = values  # {register: [reg0, reg1]}
        self.connected = True

    def read_holding_registers(self, address, count, **kwargs):
        return _FakeResult(self.values[address])


def _make_reader(values, energy_unit_id=None):
    reader = ModbusGridMeter(host="192.168.1.50", energy_unit_id=energy_unit_id)
    reader._client = _FakeModbusClient(values)
    return reader


def test_read_energy_kwh_combines_high_and_low_words():
    # 123456 raw -> 1234.56 kWh; high=1, low=57920 -> (1<<16)|57920 = 123456
    reader = _make_reader({
        ENERGY_FROM_NET_REGISTER: [1, 57920],
        ENERGY_TO_NET_REGISTER: [0, 500],
    })
    from_kwh, to_kwh = reader.read_energy_kwh()
    assert from_kwh == 1234.56
    assert to_kwh == 5.0


def test_energy_unit_id_defaults_to_unit_id():
    reader = ModbusGridMeter(host="192.168.1.50", unit_id=42)
    assert reader.energy_unit_id == 42


def test_energy_unit_id_can_be_overridden():
    reader = ModbusGridMeter(host="192.168.1.50", unit_id=100, energy_unit_id=30)
    assert reader.energy_unit_id == 30

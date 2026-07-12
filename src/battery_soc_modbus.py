"""Reads the Cerbo GX's aggregated battery SOC/power over Modbus TCP.

Same fixed system-aggregate unit ID already used for grid power
(src/grid_meter_modbus.py, com.victronenergy.system, unit ID 100):
- register 843 = /Dc/Battery/Soc, unsigned (uint16, 0-100%, scale 1) -- no
  two's-complement conversion needed.
- register 842 = /Dc/Battery/Power, signed (int16, W, scale 1) -- same sign
  convention as Victron's own UI: positive = charging, negative =
  discharging. Confirmed against Victron's official Modbus-TCP register
  list (adjacent to the SOC register, same service). Dashboard display
  only -- not used by the injection-control gating logic (SOC alone drives
  that, see src/controller.BatteryFullHysteresis).
"""

from __future__ import annotations

from src.battery_soc import BatterySocUnavailable
from src.grid_meter_modbus import SYSTEM_UNIT_ID, _read_holding_registers, _to_signed_int16

SOC_REGISTER = 843
POWER_REGISTER = 842


class ModbusBatterySoc:
    def __init__(self, host: str, port: int = 502, unit_id: int = SYSTEM_UNIT_ID, timeout_s: float = 5.0):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout_s = timeout_s
        self._client = None

    def _connected_client(self):
        from pymodbus.client import ModbusTcpClient

        if self._client is None:
            self._client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout_s)
        if not self._client.connected and not self._client.connect():
            raise BatterySocUnavailable(f"cannot connect to Modbus TCP at {self.host}:{self.port}")
        return self._client

    def _read_register(self, register: int) -> int:
        client = self._connected_client()
        try:
            result = _read_holding_registers(client, register, 1, self.unit_id)
        except Exception as exc:  # pymodbus exception types vary by version/transport error
            raise BatterySocUnavailable(f"Modbus read failed: {exc}") from exc
        if result is None or result.isError():
            raise BatterySocUnavailable(f"Modbus read error from {self.host}:{self.port}: {result}")
        return result.registers[0]

    def read_soc_pct(self) -> float:
        return float(self._read_register(SOC_REGISTER))

    def read_power_w(self) -> float:
        return float(_to_signed_int16(self._read_register(POWER_REGISTER)))

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

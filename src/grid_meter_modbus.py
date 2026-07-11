"""Reads instantaneous grid power from a Cerbo GX over Modbus TCP.

For running the controller off-device (e.g. a separate Linux VM on the same
LAN as the Cerbo GX) instead of directly on Venus OS, where local D-Bus
access (src/grid_meter.py) isn't available.

Uses the Cerbo GX's fixed system-aggregate Modbus unit ID 100
(com.victronenergy.system), register 820 = Grid L1 active power (int16, W,
1:1 scale, negative = export -- same sign convention as the D-Bus path).
This is deliberately NOT the grid meter's own com.victronenergy.grid.* Modbus
service, whose unit ID equals its per-install VRM DeviceInstance and would
need to be looked up on every install; unit ID 100 always works.

Single-phase installation: only register 820 (L1) is read.

Requires Settings > Services > Modbus/TCP enabled on the Cerbo GX (port 502
by default), and the `pymodbus` package on the machine running this module
(not needed on Venus OS itself, so it stays out of src/grid_meter.py).
"""

from __future__ import annotations

from src.grid_meter import GridMeterUnavailable

SYSTEM_UNIT_ID = 100
GRID_L1_POWER_REGISTER = 820


def _to_signed_int16(raw: int) -> int:
    """pymodbus returns registers as unsigned 16-bit; Victron's are signed."""
    return raw - 65536 if raw > 32767 else raw


def _read_holding_registers(client, address: int, count: int, unit_id: int):
    # pymodbus renamed its unit-id keyword across major versions (unit= in
    # 2.x, slave= in 3.x) -- try both rather than pinning an exact version.
    try:
        return client.read_holding_registers(address=address, count=count, slave=unit_id)
    except TypeError:
        return client.read_holding_registers(address=address, count=count, unit=unit_id)


class ModbusGridMeter:
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
            raise GridMeterUnavailable(f"cannot connect to Modbus TCP at {self.host}:{self.port}")
        return self._client

    def read_grid_power_w(self) -> float:
        client = self._connected_client()
        try:
            result = _read_holding_registers(client, GRID_L1_POWER_REGISTER, 1, self.unit_id)
        except Exception as exc:  # pymodbus exception types vary by version/transport error
            raise GridMeterUnavailable(f"Modbus read failed: {exc}") from exc
        if result is None or result.isError():
            raise GridMeterUnavailable(f"Modbus read error from {self.host}:{self.port}: {result}")
        return float(_to_signed_int16(result.registers[0]))

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

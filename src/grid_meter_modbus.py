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

from typing import Optional

from src.grid_meter import GridMeterUnavailable

SYSTEM_UNIT_ID = 100
GRID_L1_POWER_REGISTER = 820

# com.victronenergy.grid (the meter's OWN Modbus service, not the fixed
# com.victronenergy.system aggregate used above) -- unit ID is the meter's
# per-install VRM device instance, NOT guaranteed to be 100. Confirmed
# against Victron's official CCGX-Modbus-TCP-register-list.xlsx:
# - 2634/2635 = Total Energy from net (/Ac/Energy/Forward), uint32, scale
#   100 -> kWh. Deliberately NOT the per-phase 2603 (uint16, wraps at
#   655.35 kWh) -- this project is single-phase so "L1" and "Total" are the
#   same physical quantity, and uint32 avoids the wraparound entirely.
# - 2636/2637 = Total Energy to net (/Ac/Energy/Reverse), same scale/type.
# 32-bit values are big-endian at the word level (first/lower-address
# register holds the high 16 bits), the common convention for Victron's
# Modbus-TCP registers.
ENERGY_FROM_NET_REGISTER = 2634
ENERGY_TO_NET_REGISTER = 2636


def _to_signed_int16(raw: int) -> int:
    """pymodbus returns registers as unsigned 16-bit; Victron's are signed."""
    return raw - 65536 if raw > 32767 else raw


# pymodbus has renamed its unit-id keyword across versions: unit= (2.x),
# slave= (3.0-3.7ish), device_id= (3.8+, confirmed on 3.13.1). Tried in this
# order (newest first) rather than pinning an exact version, since pinning
# already broke once between when this was written and when it was deployed.
_UNIT_ID_KEYWORDS = ("device_id", "slave", "unit")


def _read_holding_registers(client, address: int, count: int, unit_id: int):
    last_error: Optional[TypeError] = None
    for kwarg in _UNIT_ID_KEYWORDS:
        try:
            return client.read_holding_registers(address=address, count=count, **{kwarg: unit_id})
        except TypeError as exc:
            last_error = exc
    raise TypeError(
        f"read_holding_registers accepts none of {_UNIT_ID_KEYWORDS} on this pymodbus version"
    ) from last_error


class ModbusGridMeter:
    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = SYSTEM_UNIT_ID,
        timeout_s: float = 5.0,
        energy_unit_id: Optional[int] = None,
    ):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        # Defaults to unit_id -- on installs where the grid meter's own
        # com.victronenergy.grid service happens to share the same Modbus
        # unit ID as the system aggregate, no separate config is needed;
        # override only if that's not the case on a given install.
        self.energy_unit_id = energy_unit_id if energy_unit_id is not None else unit_id
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

    def _read_uint32_kwh(self, register: int) -> float:
        client = self._connected_client()
        try:
            result = _read_holding_registers(client, register, 2, self.energy_unit_id)
        except Exception as exc:  # pymodbus exception types vary by version/transport error
            raise GridMeterUnavailable(f"Modbus read failed: {exc}") from exc
        if result is None or result.isError():
            raise GridMeterUnavailable(f"Modbus read error from {self.host}:{self.port}: {result}")
        high, low = result.registers[0], result.registers[1]
        return ((high << 16) | low) / 100.0

    def read_energy_kwh(self) -> "tuple[float, float]":
        """Returns (energy_from_net_kwh, energy_to_net_kwh) -- cumulative
        totals since the meter's own counter was last reset, not a rate."""
        from_kwh = self._read_uint32_kwh(ENERGY_FROM_NET_REGISTER)
        to_kwh = self._read_uint32_kwh(ENERGY_TO_NET_REGISTER)
        return from_kwh, to_kwh

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

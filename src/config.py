"""Loading and validation of the zero-export controller configuration file."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OpenDTUConfig:
    base_url: str


@dataclass
class ModbusGridConfig:
    host: str
    port: int = 502
    unit_id: int = 100  # com.victronenergy.system aggregate service - fixed, no per-install lookup needed


@dataclass
class GridConfig:
    export_setpoint_w: float = 30.0
    read_interval_s: float = 2.0
    smoothing_samples: int = 3
    # "dbus": read locally, only works when running directly on the Cerbo GX.
    # "modbus": read over the network (Modbus TCP), for running off-device
    # (e.g. a separate Linux VM) - requires grid.modbus.host below.
    source: str = "dbus"
    modbus: Optional[ModbusGridConfig] = None


@dataclass
class ControlConfig:
    kp: float = 0.4
    ki: float = 0.05
    decision_interval_s: float = 5.0
    step_absolute_w: float = 100.0
    step_relative_pct: float = 10.0
    min_change_w: float = 5.0


@dataclass
class CapacityProbeConfig:
    step_w: float = 10.0
    interval_s: float = 30.0


@dataclass
class InverterConfig:
    serial: str
    nominal_power_w: float


@dataclass
class AppConfig:
    opendtu: OpenDTUConfig
    grid: GridConfig
    control: ControlConfig
    capacity_probe: CapacityProbeConfig
    inverters: list[InverterConfig] = field(default_factory=list)

    @property
    def total_nominal_power_w(self) -> float:
        return sum(inv.nominal_power_w for inv in self.inverters)


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return parse_config(raw)


def parse_config(raw: dict) -> AppConfig:
    if "opendtu" not in raw or "base_url" not in raw["opendtu"]:
        raise ValueError("config.opendtu.base_url is required")
    inverters = [
        InverterConfig(serial=str(inv["serial"]), nominal_power_w=float(inv["nominal_power_w"]))
        for inv in raw.get("inverters", [])
    ]
    if not inverters:
        raise ValueError("config.inverters must contain at least one inverter")

    grid_raw = raw.get("grid", {})
    control_raw = raw.get("control", {})
    probe_raw = raw.get("capacity_probe", {})

    grid_source = grid_raw.get("source", "dbus")
    if grid_source not in ("dbus", "modbus"):
        raise ValueError("config.grid.source must be 'dbus' or 'modbus'")
    modbus_cfg = None
    if grid_source == "modbus":
        modbus_raw = grid_raw.get("modbus") or {}
        if "host" not in modbus_raw:
            raise ValueError("config.grid.modbus.host is required when grid.source == 'modbus'")
        modbus_cfg = ModbusGridConfig(
            host=str(modbus_raw["host"]),
            port=int(modbus_raw.get("port", 502)),
            unit_id=int(modbus_raw.get("unit_id", 100)),
        )

    return AppConfig(
        opendtu=OpenDTUConfig(base_url=raw["opendtu"]["base_url"].rstrip("/")),
        grid=GridConfig(
            export_setpoint_w=float(grid_raw.get("export_setpoint_w", 30.0)),
            read_interval_s=float(grid_raw.get("read_interval_s", 2.0)),
            smoothing_samples=int(grid_raw.get("smoothing_samples", 3)),
            source=grid_source,
            modbus=modbus_cfg,
        ),
        control=ControlConfig(
            kp=float(control_raw.get("kp", 0.4)),
            ki=float(control_raw.get("ki", 0.05)),
            decision_interval_s=float(control_raw.get("decision_interval_s", 5.0)),
            step_absolute_w=float(control_raw.get("step_absolute_w", 100.0)),
            step_relative_pct=float(control_raw.get("step_relative_pct", 10.0)),
            min_change_w=float(control_raw.get("min_change_w", 5.0)),
        ),
        capacity_probe=CapacityProbeConfig(
            step_w=float(probe_raw.get("step_w", 10.0)),
            interval_s=float(probe_raw.get("interval_s", 30.0)),
        ),
        inverters=inverters,
    )

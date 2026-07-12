"""Loading and validation of the zero-export controller configuration file."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OpenDTUConfig:
    base_url: str
    # Only needed if OpenDTU's write endpoints require Basic Auth (default
    # username is "admin", set in OpenDTU's own Security settings) -- the
    # read-only API works without these on a default OpenDTU install.
    username: Optional[str] = None
    password: Optional[str] = None


@dataclass
class ModbusGridConfig:
    host: str
    port: int = 502
    unit_id: int = 100  # com.victronenergy.system aggregate service - fixed, no per-install lookup needed
    # Unit ID of the grid meter's OWN com.victronenergy.grid service (used
    # only for the energy import/export counters, registers 2634/2636) --
    # this is the meter's per-install VRM device instance, NOT guaranteed to
    # equal unit_id above. Defaults to unit_id if unset (works when both
    # happen to coincide on a given install; override via config.grid.modbus
    # .energy_unit_id if not -- check Settings > Services > Modbus TCP on
    # the Cerbo GX for the grid meter's actual unit ID).
    energy_unit_id: Optional[int] = None


@dataclass
class GridConfig:
    export_setpoint_w: float = 30.0
    read_interval_s: float = 2.0
    # EMA filter coefficient for grid power (0, 1]. Higher = faster reaction
    # to a genuine load step, at the cost of passing through more noise.
    # Time constant is roughly read_interval_s / ema_alpha.
    ema_alpha: float = 0.5
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
    # Global floor, as a % of each inverter's own nominal power: an inverter
    # that's producing at all is never asked for less than this (some
    # micro-inverters don't regulate reliably near zero). 0 disables it --
    # a genuine 0 target (fail-safe, full curtailment) is never affected.
    min_inverter_pct: float = 10.0


@dataclass
class CapacityProbeConfig:
    step_w: float = 10.0
    interval_s: float = 30.0


@dataclass
class BatteryConfig:
    # When enabled, injection control (curtailment) is released (inverters
    # run uncapped) until the battery SOC reaches activate_at_pct, so the
    # Victron ESS/battery charger -- not this project -- absorbs AC-coupled
    # PV surplus by charging. Once activated, stays active until SOC drops
    # below deactivate_below_pct (hysteresis, avoids flapping on/off).
    # Uses the same connection as grid.source/grid.modbus (same Cerbo GX).
    enabled: bool = False
    activate_at_pct: float = 100.0
    deactivate_below_pct: float = 98.0
    # Activates injection control early (without waiting for SOC to reach
    # activate_at_pct exactly) if real grid export beyond this magnitude
    # (W) is observed while SOC is already >= deactivate_below_pct --
    # empirical proof the battery can't absorb more. 0 disables this.
    export_confirms_full_w: float = 50.0


@dataclass
class InverterConfig:
    serial: str
    nominal_power_w: float
    # Display only (dashboard legend/table) -- never used to address the
    # inverter, OpenDTU is only ever talked to by serial. Optional: falls
    # back to the serial itself wherever it's shown if unset.
    name: Optional[str] = None


@dataclass
class WebConfig:
    # Built-in config editor (src/webui.py), served in a background thread
    # of the same process. Writes config.json directly; does not reload the
    # running control loop or restart the service (see README.md) -- a
    # restart is required for edits to take effect.
    enabled: bool = True
    port: int = 8080


@dataclass
class LoggingConfig:
    # Gates only the per-cycle state line ("grid_meter=... injection_control=...")
    # logged every decision cycle whether or not anything changed -- errors,
    # warnings and one-off actions (fail-safe, release for charging, restart
    # requests) always log regardless of this setting. Defaults to True
    # (unchanged behaviour); turn off once the web dashboard (/dashboard)
    # covers what you'd otherwise be tailing logs for.
    verbose_traces: bool = True


@dataclass
class AppConfig:
    opendtu: OpenDTUConfig
    grid: GridConfig
    control: ControlConfig
    capacity_probe: CapacityProbeConfig
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    web: WebConfig = field(default_factory=WebConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
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
        InverterConfig(
            serial=str(inv["serial"]),
            nominal_power_w=float(inv["nominal_power_w"]),
            name=(str(inv["name"]) if inv.get("name") else None),
        )
        for inv in raw.get("inverters", [])
    ]
    if not inverters:
        raise ValueError("config.inverters must contain at least one inverter")

    grid_raw = raw.get("grid", {})
    control_raw = raw.get("control", {})
    probe_raw = raw.get("capacity_probe", {})
    battery_raw = raw.get("battery", {})
    web_raw = raw.get("web", {})
    logging_raw = raw.get("logging", {})

    grid_source = grid_raw.get("source", "dbus")
    if grid_source not in ("dbus", "modbus"):
        raise ValueError("config.grid.source must be 'dbus' or 'modbus'")
    modbus_cfg = None
    if grid_source == "modbus":
        modbus_raw = grid_raw.get("modbus") or {}
        if "host" not in modbus_raw:
            raise ValueError("config.grid.modbus.host is required when grid.source == 'modbus'")
        energy_unit_id_raw = modbus_raw.get("energy_unit_id")
        modbus_cfg = ModbusGridConfig(
            host=str(modbus_raw["host"]),
            port=int(modbus_raw.get("port", 502)),
            unit_id=int(modbus_raw.get("unit_id", 100)),
            energy_unit_id=int(energy_unit_id_raw) if energy_unit_id_raw is not None else None,
        )

    opendtu_raw = raw["opendtu"]
    return AppConfig(
        opendtu=OpenDTUConfig(
            base_url=opendtu_raw["base_url"].rstrip("/"),
            username=opendtu_raw.get("username") or None,
            password=opendtu_raw.get("password") or None,
        ),
        grid=GridConfig(
            export_setpoint_w=float(grid_raw.get("export_setpoint_w", 30.0)),
            read_interval_s=float(grid_raw.get("read_interval_s", 2.0)),
            ema_alpha=float(grid_raw.get("ema_alpha", 0.5)),
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
            min_inverter_pct=float(control_raw.get("min_inverter_pct", 10.0)),
        ),
        capacity_probe=CapacityProbeConfig(
            step_w=float(probe_raw.get("step_w", 10.0)),
            interval_s=float(probe_raw.get("interval_s", 30.0)),
        ),
        battery=BatteryConfig(
            enabled=bool(battery_raw.get("enabled", False)),
            activate_at_pct=float(battery_raw.get("activate_at_pct", 100.0)),
            deactivate_below_pct=float(battery_raw.get("deactivate_below_pct", 98.0)),
            export_confirms_full_w=float(battery_raw.get("export_confirms_full_w", 50.0)),
        ),
        web=WebConfig(
            enabled=bool(web_raw.get("enabled", True)),
            port=int(web_raw.get("port", 8080)),
        ),
        logging=LoggingConfig(
            verbose_traces=bool(logging_raw.get("verbose_traces", True)),
        ),
        inverters=inverters,
    )

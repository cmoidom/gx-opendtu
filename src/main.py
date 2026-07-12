"""Entry point: wires the grid meter, OpenDTU client and control loop together.

Two cadences: a fast read/smooth loop for grid power (config.grid.read_interval_s)
and a slower, quantized decision loop that talks to OpenDTU
(config.control.decision_interval_s). See ARCHITECTURE.md for the full design.
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Dict, Iterable, Optional

from src.allocator import water_fill_allocate
from src.battery_soc import BatterySocUnavailable, DbusBatterySoc
from src.config import AppConfig, load_config
from src.controller import BatteryFullHysteresis, CapacityEstimator, GridPowerSmoother, SoftTargetController
from src.energy_history import HourlyEnergyHistory
from src.grid_meter import DbusGridMeter, GridMeterUnavailable
from src.live_state import LiveState
from src.opendtu_client import OpenDTUClient, OpenDTUError

FAILSAFE_AFTER_CONSECUTIVE_FAILURES = 3

log = logging.getLogger("gx-opendtu-zero-export")


def _make_grid_reader(config: AppConfig):
    if config.grid.source == "modbus":
        from src.grid_meter_modbus import ModbusGridMeter

        return ModbusGridMeter(
            host=config.grid.modbus.host,
            port=config.grid.modbus.port,
            unit_id=config.grid.modbus.unit_id,
            energy_unit_id=config.grid.modbus.energy_unit_id,
        )
    return DbusGridMeter()


def _make_battery_reader(config: AppConfig):
    if not config.battery.enabled:
        return None
    if config.grid.source == "modbus":
        from src.battery_soc_modbus import ModbusBatterySoc

        return ModbusBatterySoc(
            host=config.grid.modbus.host,
            port=config.grid.modbus.port,
            unit_id=config.grid.modbus.unit_id,
        )
    return DbusBatterySoc()


def _decision_cycle(
    client: OpenDTUClient,
    controller: SoftTargetController,
    capacity: CapacityEstimator,
    serials: Iterable[str],
    grid_power_raw_w: float,
    grid_power_avg_w: float,
    live_state: Optional[LiveState] = None,
    soc_pct: Optional[float] = None,
    battery_power_w: Optional[float] = None,
    dry_run: bool = False,
    verbose_traces: bool = True,
    min_inverter_pct: float = 0.0,
    name_by_serial: Optional[Dict[str, str]] = None,
) -> None:
    name_by_serial = name_by_serial or {}
    live_power_w = client.get_live_power_w(serials)
    limit_status = client.get_limit_status()

    current_total_actual_w = sum(live_power_w.get(s, 0.0) for s in serials)
    total_capacity_w = sum(capacity.ceilings_w.get(s, 0.0) for s in serials)

    decision = controller.compute_target(grid_power_avg_w, current_total_actual_w, total_capacity_w)
    allocation = water_fill_allocate(
        decision.target_w,
        serials,
        capacity.ceilings_w,
        min_inverter_pct=min_inverter_pct,
        nominal_power_w=capacity.nominal_power_w,
    )
    rounded_allocation = {s: round(w) for s, w in allocation.items()}

    if not dry_run and decision.changed:
        for serial, watts in allocation.items():
            client.set_absolute_limit_w(serial, watts)

    floor_warning, recommended_min_inverter_pct = _min_inverter_floor_warning(
        min_inverter_pct,
        grid_power_avg_w,
        decision.target_w,
        allocation,
        capacity.ceilings_w,
        capacity.nominal_power_w,
        serials,
    )
    if floor_warning:
        # Always logged (unlike the verbose_traces-gated line below): the
        # floor is doing exactly what config.control.min_inverter_pct asked
        # for, but if this fires often the configured value is probably
        # higher than this install's real demand right now.
        log.warning(
            "min_inverter_pct=%.0f%% causing grid export this cycle (grid_ema=%+.0fW, consigne=%.0fW) "
            "-- valeur qui n'aurait pas depasse la consigne ce cycle: %.1f%%",
            min_inverter_pct,
            grid_power_avg_w,
            decision.target_w,
            recommended_min_inverter_pct if recommended_min_inverter_pct is not None else 0.0,
        )

    if live_state is not None:
        live_state.update_decision(
            soc_pct,
            "ON",
            decision.target_w,
            [
                {
                    "serial": serial,
                    "name": name_by_serial.get(serial),
                    "allocated_w": rounded_allocation.get(serial, 0),
                    "actual_w": live_power_w.get(serial, 0.0),
                    "limit_relative_pct": (
                        limit_status[serial].limit_relative if serial in limit_status else None
                    ),
                    "max_power_w": capacity.ceilings_w.get(serial, 0.0),
                    "acknowledged": limit_status[serial].acknowledged if serial in limit_status else None,
                }
                for serial in serials
            ],
            battery_power_w=battery_power_w,
            min_inverter_floor_warning=floor_warning,
            recommended_min_inverter_pct=recommended_min_inverter_pct,
        )

    # Logs full state every cycle (not just on change) for debug visibility
    # when verbose_traces is on -- this only affects local logging, not
    # OpenDTU traffic (still gated by decision.changed above), so it doesn't
    # undo the rate-limiting the soft controller is there for. Independent
    # of the /dashboard live view (src/live_state.py), which always updates.
    if verbose_traces:
        soc_str = f" soc={soc_pct:.0f}%" if soc_pct is not None else ""
        log.info(
            "%sgrid_meter=%+.0fW ema=%+.0fW opendtu_actual=%.0fW%s injection_control=ON consigne=%.0fW allocation=%s changed=%s%s",
            "[DRY-RUN] " if dry_run else "",
            grid_power_raw_w,
            grid_power_avg_w,
            current_total_actual_w,
            soc_str,
            decision.target_w,
            rounded_allocation,
            decision.changed,
            " (rien envoye)" if dry_run else "",
        )

    for serial in serials:
        status = limit_status.get(serial)
        capacity.observe(
            serial,
            allocated_w=allocation.get(serial, 0.0),
            actual_w=live_power_w.get(serial, 0.0),
            limit_acknowledged=status.acknowledged if status else True,
        )


def _min_inverter_floor_warning(
    min_inverter_pct: float,
    grid_power_avg_w: float,
    target_w: float,
    allocation: Dict[str, float],
    ceilings_w: Dict[str, float],
    nominal_power_w: Dict[str, float],
    serials: Iterable[str],
) -> "tuple[bool, Optional[float]]":
    """Detects the min_inverter_pct floor pushing the total allocation above
    what the controller actually wanted while the grid is exporting -- a
    sign the configured floor is higher than this install's real demand
    right now (config.control.min_inverter_pct is authoritative regardless,
    see ARCHITECTURE.md -- this is purely informational).

    recommended_pct is the largest floor that would NOT have exceeded this
    cycle's target, given the nominal power of inverters that currently
    have real capacity -- a live, instantaneous suggestion (will fluctuate
    cycle to cycle), not a universal fixed value."""
    if min_inverter_pct <= 0 or grid_power_avg_w >= 0:
        return False, None
    serials = list(serials)
    total_allocated_w = sum(allocation.get(s, 0.0) for s in serials)
    if total_allocated_w <= target_w + 1e-6:
        return False, None
    capacity_w = sum(nominal_power_w.get(s, 0.0) for s in serials if ceilings_w.get(s, 0.0) > 0)
    recommended_pct = round(max(0.0, target_w) / capacity_w * 100.0, 1) if capacity_w > 0 else 0.0
    return True, recommended_pct


def _off_state_inverters_payload(
    client: OpenDTUClient,
    serials: Iterable[str],
    nominal_power_w: Dict[str, float],
    name_by_serial: Optional[Dict[str, str]] = None,
) -> list:
    """Live per-inverter power for the dashboard while injection control is
    OFF (charge batterie prioritaire): inverters are uncapped, so there is
    no allocated/limit-status data to report, but the actual measured power
    is still meaningful and otherwise leaves the dashboard looking empty/
    broken during the whole charge-priority window."""
    name_by_serial = name_by_serial or {}
    try:
        live_power_w = client.get_live_power_w(serials)
    except OpenDTUError:
        return []
    return [
        {
            "serial": serial,
            "name": name_by_serial.get(serial),
            "allocated_w": None,
            "actual_w": live_power_w.get(serial, 0.0),
            "limit_relative_pct": 100,
            "max_power_w": nominal_power_w.get(serial, 0.0),
            "acknowledged": None,
        }
        for serial in serials
    ]


def _release_for_charging(client: OpenDTUClient, serials: Iterable[str], dry_run: bool = False) -> None:
    """Battery not yet full: release curtailment so PV runs uncapped and the
    Victron ESS/battery charger absorbs the surplus by charging."""
    if dry_run:
        log.info("[DRY-RUN] charge batterie prioritaire: onduleurs seraient debloques a 100%% (rien envoye)")
        return
    log.info("charge batterie prioritaire: deblocage des onduleurs a 100%%")
    for serial in serials:
        try:
            client.set_relative_limit_pct(serial, 100)
        except OpenDTUError as exc:
            log.error("release to 100%% of %s failed: %s", serial, exc)


def _apply_failsafe(client: OpenDTUClient, serials: Iterable[str], dry_run: bool = False) -> None:
    if dry_run:
        log.warning("[DRY-RUN] fail-safe se declencherait: mise a 0%% de tous les onduleurs (rien envoye)")
        return
    log.warning("applying fail-safe: curtailing all inverters to 0%%")
    for serial in serials:
        try:
            client.set_relative_limit_pct(serial, 0)
        except OpenDTUError as exc:
            log.error("fail-safe curtail of %s failed: %s", serial, exc)


def run(
    config: AppConfig,
    dry_run: bool = False,
    live_state: Optional[LiveState] = None,
    energy_history: Optional[HourlyEnergyHistory] = None,
) -> None:
    if live_state is None:
        live_state = LiveState()
    if energy_history is None:
        energy_history = HourlyEnergyHistory()
    grid_reader = _make_grid_reader(config)
    battery_reader = _make_battery_reader(config)
    hysteresis = (
        BatteryFullHysteresis(
            config.battery.activate_at_pct,
            config.battery.deactivate_below_pct,
            export_confirms_full_w=config.battery.export_confirms_full_w,
        )
        if battery_reader is not None
        else None
    )
    client = OpenDTUClient(
        config.opendtu.base_url, username=config.opendtu.username, password=config.opendtu.password
    )
    smoother = GridPowerSmoother(config.grid.ema_alpha)
    controller = SoftTargetController(
        export_setpoint_w=config.grid.export_setpoint_w,
        kp=config.control.kp,
        ki=config.control.ki,
        step_absolute_w=config.control.step_absolute_w,
        step_relative_pct=config.control.step_relative_pct,
        min_change_w=config.control.min_change_w,
    )
    nominal_power_w: Dict[str, float] = {inv.serial: inv.nominal_power_w for inv in config.inverters}
    name_by_serial: Dict[str, str] = {inv.serial: inv.name for inv in config.inverters if inv.name}
    capacity = CapacityEstimator(nominal_power_w, config.capacity_probe.step_w)
    serials = [inv.serial for inv in config.inverters]

    last_decision_time = 0.0
    last_probe_time = 0.0
    consecutive_grid_failures = 0
    released_for_charging = False

    while True:
        now = time.monotonic()

        try:
            grid_power_w = grid_reader.read_grid_power_w()
            smoother.add(grid_power_w)
            live_state.record_grid(grid_power_w, smoother.average)
            consecutive_grid_failures = 0
        except GridMeterUnavailable as exc:
            consecutive_grid_failures += 1
            log.error("grid meter read failed (%d in a row): %s", consecutive_grid_failures, exc)
            if consecutive_grid_failures >= FAILSAFE_AFTER_CONSECUTIVE_FAILURES:
                _apply_failsafe(client, serials, dry_run=dry_run)
            time.sleep(config.grid.read_interval_s)
            continue

        if now - last_decision_time >= config.control.decision_interval_s:
            last_decision_time = now

            try:
                from_kwh, to_kwh = grid_reader.read_energy_kwh()
                energy_history.record(from_kwh, to_kwh)
            except GridMeterUnavailable as exc:
                log.error("grid energy counters read failed (dashboard display only): %s", exc)

            soc_pct: Optional[float] = None
            battery_power_w: Optional[float] = None
            injection_active = True
            if battery_reader is not None:
                try:
                    soc_pct = battery_reader.read_soc_pct()
                    injection_active = hysteresis.update(soc_pct, grid_power_w=smoother.average)
                except BatterySocUnavailable as exc:
                    # Safe default: if we can't tell whether the battery is
                    # full, assume it is and keep injection control active
                    # rather than releasing curtailment unsupervised. Does
                    # not touch the latch itself, only this cycle's action.
                    log.error(
                        "battery SOC read failed, defaulting injection control to ACTIVE (safe): %s", exc
                    )
                    injection_active = True
                try:
                    battery_power_w = battery_reader.read_power_w()
                except BatterySocUnavailable:
                    battery_power_w = None  # dashboard display only, not safety-critical

            if not injection_active:
                if not released_for_charging:
                    _release_for_charging(client, serials, dry_run=dry_run)
                    released_for_charging = True
                live_state.update_decision(
                    soc_pct,
                    "OFF",
                    None,
                    _off_state_inverters_payload(client, serials, nominal_power_w, name_by_serial),
                    battery_power_w=battery_power_w,
                )
                if config.logging.verbose_traces:
                    log.info(
                        "%ssoc=%.0f%% grid_meter=%+.0fW ema=%+.0fW injection_control=OFF (charge batterie prioritaire)%s",
                        "[DRY-RUN] " if dry_run else "",
                        soc_pct if soc_pct is not None else float("nan"),
                        grid_power_w,
                        smoother.average,
                        " (rien envoye)" if dry_run else "",
                    )
            else:
                released_for_charging = False
                try:
                    _decision_cycle(
                        client,
                        controller,
                        capacity,
                        serials,
                        grid_power_w,
                        smoother.average,
                        live_state=live_state,
                        soc_pct=soc_pct,
                        battery_power_w=battery_power_w,
                        dry_run=dry_run,
                        verbose_traces=config.logging.verbose_traces,
                        min_inverter_pct=config.control.min_inverter_pct,
                        name_by_serial=name_by_serial,
                    )
                except OpenDTUError as exc:
                    log.error("OpenDTU communication failed: %s", exc)
                    _apply_failsafe(client, serials, dry_run=dry_run)

        if now - last_probe_time >= config.capacity_probe.interval_s:
            last_probe_time = now
            capacity.probe_tick()

        time.sleep(config.grid.read_interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Zero-injection PV controller (Cerbo GX + OpenDTU)")
    parser.add_argument("--config", default="/data/gx-opendtu-zero-export/config.json")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ne rien envoyer a OpenDTU: trace seulement grid_meter/opendtu_actual/consigne a chaque cycle.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config)
    live_state = LiveState()
    energy_history = HourlyEnergyHistory()

    if config.web.enabled:
        from src.webui import start_webui_server

        start_webui_server(args.config, config.web.port, live_state, energy_history)
        log.info("page de configuration disponible sur http://0.0.0.0:%d/", config.web.port)

    run(config, dry_run=args.dry_run, live_state=live_state, energy_history=energy_history)


if __name__ == "__main__":
    main()

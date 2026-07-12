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
from src.grid_meter import DbusGridMeter, GridMeterUnavailable
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
    soc_pct: Optional[float] = None,
    dry_run: bool = False,
) -> None:
    live_power_w = client.get_live_power_w()
    limit_status = client.get_limit_status()

    current_total_actual_w = sum(live_power_w.get(s, 0.0) for s in serials)
    total_capacity_w = sum(capacity.ceilings_w.get(s, 0.0) for s in serials)

    decision = controller.compute_target(grid_power_avg_w, current_total_actual_w, total_capacity_w)
    allocation = water_fill_allocate(decision.target_w, serials, capacity.ceilings_w)
    rounded_allocation = {s: round(w) for s, w in allocation.items()}

    if not dry_run and decision.changed:
        for serial, watts in allocation.items():
            client.set_absolute_limit_w(serial, watts)

    # Always log full state every cycle (not just on change) for debug
    # visibility -- this only affects local logging, not OpenDTU traffic
    # (still gated by decision.changed above), so it doesn't undo the
    # rate-limiting the soft controller is there for.
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


def run(config: AppConfig, dry_run: bool = False) -> None:
    grid_reader = _make_grid_reader(config)
    battery_reader = _make_battery_reader(config)
    hysteresis = (
        BatteryFullHysteresis(config.battery.activate_at_pct, config.battery.deactivate_below_pct)
        if battery_reader is not None
        else None
    )
    client = OpenDTUClient(config.opendtu.base_url)
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

            soc_pct: Optional[float] = None
            injection_active = True
            if battery_reader is not None:
                try:
                    soc_pct = battery_reader.read_soc_pct()
                    injection_active = hysteresis.update(soc_pct)
                except BatterySocUnavailable as exc:
                    # Safe default: if we can't tell whether the battery is
                    # full, assume it is and keep injection control active
                    # rather than releasing curtailment unsupervised. Does
                    # not touch the latch itself, only this cycle's action.
                    log.error(
                        "battery SOC read failed, defaulting injection control to ACTIVE (safe): %s", exc
                    )
                    injection_active = True

            if not injection_active:
                if not released_for_charging:
                    _release_for_charging(client, serials, dry_run=dry_run)
                    released_for_charging = True
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
                        soc_pct=soc_pct,
                        dry_run=dry_run,
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

    if config.web.enabled:
        from src.webui import start_webui_server

        start_webui_server(args.config, config.web.port)
        log.info("page de configuration disponible sur http://0.0.0.0:%d/", config.web.port)

    run(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

"""Entry point: wires the grid meter, OpenDTU client and control loop together.

Two cadences: a fast read/smooth loop for grid power (config.grid.read_interval_s)
and a slower, quantized decision loop that talks to OpenDTU
(config.control.decision_interval_s). See ARCHITECTURE.md for the full design.
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Dict, Iterable

from src.allocator import water_fill_allocate
from src.config import AppConfig, load_config
from src.controller import CapacityEstimator, GridPowerSmoother, SoftTargetController
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


def _decision_cycle(
    client: OpenDTUClient,
    controller: SoftTargetController,
    capacity: CapacityEstimator,
    serials: Iterable[str],
    grid_power_avg_w: float,
    dry_run: bool = False,
) -> None:
    live_power_w = client.get_live_power_w()
    limit_status = client.get_limit_status()

    current_total_actual_w = sum(live_power_w.get(s, 0.0) for s in serials)
    total_capacity_w = sum(capacity.ceilings_w.get(s, 0.0) for s in serials)

    decision = controller.compute_target(grid_power_avg_w, current_total_actual_w, total_capacity_w)
    allocation = water_fill_allocate(decision.target_w, serials, capacity.ceilings_w)
    rounded_allocation = {s: round(w) for s, w in allocation.items()}

    # dry_run always logs (even with no change) since the whole point is to
    # observe what the controller *would* do; real mode only logs/sends on
    # an actual change, to keep the log as quiet as the HTTP traffic.
    if dry_run:
        log.info(
            "[DRY-RUN] grid_meter=%+.0fW opendtu_actual=%.0fW consigne=%.0fW allocation=%s changed=%s (rien envoye)",
            grid_power_avg_w,
            current_total_actual_w,
            decision.target_w,
            rounded_allocation,
            decision.changed,
        )
    elif decision.changed:
        for serial, watts in allocation.items():
            client.set_absolute_limit_w(serial, watts)
        log.info(
            "grid_meter=%+.0fW opendtu_actual=%.0fW target=%.0fW allocation=%s",
            grid_power_avg_w,
            current_total_actual_w,
            decision.target_w,
            rounded_allocation,
        )

    for serial in serials:
        status = limit_status.get(serial)
        capacity.observe(
            serial,
            allocated_w=allocation.get(serial, 0.0),
            actual_w=live_power_w.get(serial, 0.0),
            limit_acknowledged=status.acknowledged if status else True,
        )


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
    client = OpenDTUClient(config.opendtu.base_url)
    smoother = GridPowerSmoother(config.grid.smoothing_samples)
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
            try:
                _decision_cycle(client, controller, capacity, serials, smoother.average, dry_run=dry_run)
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
    run(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

"""Soft, rate-limited zero-export control loop logic.

Pure logic, no I/O: takes measurements in, returns decisions out. Wired to the
real D-Bus grid meter and OpenDTU HTTP client by main.py.
"""

from __future__ import annotations

from collections import deque
from typing import Dict, Optional


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class GridPowerSmoother:
    """Moving average over the last N grid power samples, to damp measurement noise."""

    def __init__(self, samples: int):
        self._samples: deque = deque(maxlen=max(1, samples))

    def add(self, watts: float) -> float:
        self._samples.append(watts)
        return self.average

    @property
    def average(self) -> float:
        if not self._samples:
            return 0.0
        return sum(self._samples) / len(self._samples)


class PIController:
    def __init__(self, kp: float, ki: float, integral_limit: Optional[float] = None):
        self.kp = kp
        self.ki = ki
        self.integral_limit = integral_limit
        self.integral = 0.0

    def step(self, error: float) -> float:
        self.integral += error * self.ki
        if self.integral_limit is not None:
            self.integral = clamp(self.integral, -self.integral_limit, self.integral_limit)
        return self.kp * error + self.integral


def quantize(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(value / step) * step


def ramp_limit(current: float, target: float, max_step: float) -> float:
    delta = clamp(target - current, -max_step, max_step)
    return current + delta


class SoftTargetController:
    """Turns a grid-power error into a rate-limited, quantized total power target.

    The effective step is the larger of the absolute and relative step settings:
    a small install still gets a meaningful watt-sized deadband, and a large
    install doesn't get flooded with tiny percentage-sized command changes.
    """

    def __init__(
        self,
        export_setpoint_w: float,
        kp: float,
        ki: float,
        step_absolute_w: float,
        step_relative_pct: float,
        min_change_w: float,
        integral_limit: Optional[float] = None,
    ):
        self.export_setpoint_w = export_setpoint_w
        self.pi = PIController(kp, ki, integral_limit)
        self.step_absolute_w = step_absolute_w
        self.step_relative_pct = step_relative_pct
        self.min_change_w = min_change_w
        self.last_sent_total_w: Optional[float] = None

    def effective_step_w(self, total_capacity_w: float) -> float:
        relative_step = self.step_relative_pct / 100.0 * total_capacity_w
        return max(self.step_absolute_w, relative_step)

    def compute_target(
        self, grid_power_avg_w: float, current_total_actual_w: float, total_capacity_w: float
    ) -> "ControlDecision":
        error = grid_power_avg_w - self.export_setpoint_w
        delta = self.pi.step(error)
        raw_target = clamp(current_total_actual_w + delta, 0.0, total_capacity_w)

        step = self.effective_step_w(total_capacity_w)
        quantized = quantize(raw_target, step)

        baseline = self.last_sent_total_w if self.last_sent_total_w is not None else quantized
        next_target = ramp_limit(baseline, quantized, step)

        changed = (
            self.last_sent_total_w is None
            or abs(next_target - self.last_sent_total_w) >= self.min_change_w
        )
        if changed:
            self.last_sent_total_w = next_target

        return ControlDecision(target_w=next_target, changed=changed)


class ControlDecision:
    def __init__(self, target_w: float, changed: bool):
        self.target_w = target_w
        self.changed = changed


class CapacityEstimator:
    """Tracks a per-inverter capacity ceiling used by the water-filling allocator.

    Starts at each inverter's nominal power. If an inverter is unable to reach
    its allocated share while OpenDTU reports its limit as acknowledged (not
    still-limiting), it's assumed to be irradiance-limited, and its ceiling is
    lowered to its actual measured output. A slow periodic probe nudges the
    ceiling back up so a passing cloud doesn't permanently cap the inverter.
    """

    def __init__(self, nominal_power_w: Dict[str, float], probe_step_w: float):
        self.nominal_power_w = dict(nominal_power_w)
        self.probe_step_w = probe_step_w
        self.ceilings_w: Dict[str, float] = dict(nominal_power_w)

    def observe(self, serial: str, allocated_w: float, actual_w: float, limit_acknowledged: bool) -> None:
        nominal = self.nominal_power_w.get(serial, actual_w)
        if limit_acknowledged and actual_w < allocated_w - 1e-6:
            self.ceilings_w[serial] = max(0.0, actual_w)
        else:
            self.ceilings_w[serial] = min(nominal, self.ceilings_w.get(serial, nominal))

    def probe_tick(self) -> None:
        for serial, nominal in self.nominal_power_w.items():
            current = self.ceilings_w.get(serial, nominal)
            self.ceilings_w[serial] = min(nominal, current + self.probe_step_w)

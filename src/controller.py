"""Soft, rate-limited zero-export control loop logic.

Pure logic, no I/O: takes measurements in, returns decisions out. Wired to the
real D-Bus grid meter and OpenDTU HTTP client by main.py.
"""

from __future__ import annotations

from typing import Dict, Optional


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class GridPowerSmoother:
    """Exponential moving average over grid power readings, to damp
    measurement noise without adding the step-discontinuity a fixed-window
    moving average has when an old sample drops out of the window.

    filtered += alpha * (raw - filtered). Higher alpha reacts faster to a
    genuine load step (at the cost of passing through more noise); lower
    alpha is smoother but slower. Tune based on read_interval_s: the time
    constant is roughly read_interval_s / alpha.
    """

    def __init__(self, alpha: float):
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = alpha
        self._filtered: Optional[float] = None

    def add(self, watts: float) -> float:
        if self._filtered is None:
            self._filtered = watts
        else:
            self._filtered += self.alpha * (watts - self._filtered)
        return self._filtered

    @property
    def average(self) -> float:
        return self._filtered if self._filtered is not None else 0.0


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

    Only treats underperformance as evidence of a genuine capacity limit when
    the allocated share was already close to the current ceiling
    (>= NEAR_CEILING_RATIO of it) -- the zero-export target is often well
    below any inverter's max on purpose (just enough to cover load without
    exporting), so a small shortfall against a much lower allocated share
    proves nothing about true capacity. Without this guard, ordinary
    measurement noise ratchets the ceiling down under full sun too (probe_tick
    recovery is comparatively slow, so a false positive here is costly) --
    confirmed against a live install stuck producing ~5-8% of nominal per
    inverter with no real shading.
    """

    NEAR_CEILING_RATIO = 0.9

    def __init__(self, nominal_power_w: Dict[str, float], probe_step_w: float):
        self.nominal_power_w = dict(nominal_power_w)
        self.probe_step_w = probe_step_w
        self.ceilings_w: Dict[str, float] = dict(nominal_power_w)

    def observe(self, serial: str, allocated_w: float, actual_w: float, limit_acknowledged: bool) -> None:
        nominal = self.nominal_power_w.get(serial, actual_w)
        current_ceiling = self.ceilings_w.get(serial, nominal)
        was_near_ceiling = allocated_w >= self.NEAR_CEILING_RATIO * current_ceiling
        if limit_acknowledged and was_near_ceiling and actual_w < allocated_w - 1e-6:
            self.ceilings_w[serial] = max(0.0, actual_w)
        else:
            self.ceilings_w[serial] = min(nominal, current_ceiling)

    def probe_tick(self) -> None:
        for serial, nominal in self.nominal_power_w.items():
            current = self.ceilings_w.get(serial, nominal)
            self.ceilings_w[serial] = min(nominal, current + self.probe_step_w)


class BatteryFullHysteresis:
    """Latches injection control (curtailment) ON only once the battery SOC
    reaches activate_at_pct, and OFF only once it drops below
    deactivate_below_pct -- a dead zone between the two thresholds prevents
    flapping on/off as SOC drifts around either boundary during the day.

    Also activates early -- without waiting for soc_pct to reach
    activate_at_pct exactly -- if real grid export is observed while SOC is
    already at or above deactivate_below_pct: that's empirical proof the
    battery can no longer absorb the AC-coupled PV surplus, regardless of
    what the SOC estimate says (SOC reporting can lag reality, especially
    near full on a flat-voltage-curve chemistry like LFP; it also handles a
    latch that reset to inactive on a service restart while the battery was
    already full). Disabled by export_confirms_full_w <= 0.
    """

    def __init__(
        self,
        activate_at_pct: float = 100.0,
        deactivate_below_pct: float = 98.0,
        active: bool = False,
        export_confirms_full_w: float = 50.0,
    ):
        self.activate_at_pct = activate_at_pct
        self.deactivate_below_pct = deactivate_below_pct
        self.active = active
        self.export_confirms_full_w = export_confirms_full_w

    def update(self, soc_pct: float, grid_power_w: Optional[float] = None) -> bool:
        if self.active:
            if soc_pct < self.deactivate_below_pct:
                self.active = False
        elif soc_pct >= self.activate_at_pct:
            self.active = True
        elif (
            self.export_confirms_full_w > 0
            and grid_power_w is not None
            and grid_power_w <= -self.export_confirms_full_w
            and soc_pct >= self.deactivate_below_pct
        ):
            self.active = True
        return self.active

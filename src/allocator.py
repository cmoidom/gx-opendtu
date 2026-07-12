"""Water-filling distribution of a total power target across several inverters.

Splits the total equally, but caps any inverter at its known capacity ceiling
(nominal power, or a lower value if it's currently irradiance-limited) and
redistributes the remainder equally among the inverters that still have room.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

INFINITE = float("inf")


def water_fill_allocate(
    total_target_w: float,
    serials: Iterable[str],
    capacity_estimates: Dict[str, float],
    min_inverter_pct: float = 0.0,
    nominal_power_w: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    active = list(serials)
    remaining = max(0.0, total_target_w)
    allocation: Dict[str, float] = {}

    while active:
        share = remaining / len(active)
        saturated = [s for s in active if capacity_estimates.get(s, INFINITE) <= share]
        if not saturated:
            for s in active:
                allocation[s] = share
            break
        for s in saturated:
            cap = max(0.0, capacity_estimates.get(s, INFINITE))
            allocation[s] = cap
            remaining -= cap
            active.remove(s)
        remaining = max(0.0, remaining)

    # Global floor (config.control.min_inverter_pct, % of each inverter's own
    # nominal power): never ask an inverter with real producible capacity to
    # go below this -- some micro-inverters don't regulate reliably near
    # zero. Applied even when the water-filled share is exactly 0 (e.g. the
    # controller wants zero net contribution because the grid is already at
    # or below the export setpoint without these inverters' help) -- the
    # config value is authoritative, on the reasoning that if this ever
    # causes real export, the fix is to lower the configured percentage, not
    # to silently skip the floor (see main._decision_cycle's warning when
    # that happens). Clamped to the inverter's own capacity ceiling, so an
    # inverter with no real capacity right now (capacity_estimates == 0,
    # e.g. actually irradiance-limited to zero) is never floored above 0 --
    # fail-safe and the battery-charge-priority release don't go through
    # this function at all, so there's no other "genuine zero" to protect.
    if min_inverter_pct > 0 and nominal_power_w:
        for s in list(allocation.keys()):
            cap = capacity_estimates.get(s, INFINITE)
            if cap <= 0:
                continue
            floor_w = min_inverter_pct / 100.0 * nominal_power_w.get(s, 0.0)
            if floor_w > allocation[s]:
                allocation[s] = min(cap, floor_w)

    return allocation

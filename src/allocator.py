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
    # nominal power): never ask an inverter that's producing at all to go
    # below this -- some micro-inverters don't regulate reliably near zero.
    # Never applied to a legitimate 0 (fail-safe, or a target that's
    # genuinely zero) since that's a deliberate full curtailment, not a
    # low-but-nonzero share. Clamped to the inverter's own capacity ceiling
    # so the floor can never ask for more than it can currently give.
    if min_inverter_pct > 0 and nominal_power_w:
        for s, watts in list(allocation.items()):
            if watts <= 0:
                continue
            floor_w = min_inverter_pct / 100.0 * nominal_power_w.get(s, 0.0)
            if floor_w > watts:
                allocation[s] = min(capacity_estimates.get(s, INFINITE), floor_w)

    return allocation

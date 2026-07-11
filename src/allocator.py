"""Water-filling distribution of a total power target across several inverters.

Splits the total equally, but caps any inverter at its known capacity ceiling
(nominal power, or a lower value if it's currently irradiance-limited) and
redistributes the remainder equally among the inverters that still have room.
"""

from __future__ import annotations

from typing import Dict, Iterable

INFINITE = float("inf")


def water_fill_allocate(
    total_target_w: float,
    serials: Iterable[str],
    capacity_estimates: Dict[str, float],
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

    return allocation

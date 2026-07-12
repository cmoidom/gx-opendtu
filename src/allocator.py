"""Water-filling distribution of a total power target across several inverters.

Equalizes by **percentage of each inverter's own nominal power**, not equal
absolute watts -- so reducing the total curtails the inverter currently
producing the highest % of its own rating first (and, symmetrically,
raising the total favours whichever is producing the lowest % first),
converging every inverter toward the same percentage. An inverter unable to
reach that common percentage (irradiance-limited) is capped at its actual
capacity, and the shortfall is redistributed among the rest by recomputing
a new common percentage over them.

Explicit user requirement (2026-07-12): a bigger inverter shouldn't be left
producing more absolute watts than a smaller one just because an equal-watts
split doesn't account for their different ratings -- curtailment/relief
should track how "maxed out" each inverter already is relative to itself.
"""

from __future__ import annotations

from typing import Dict, Iterable

INFINITE = float("inf")


def water_fill_allocate(
    total_target_w: float,
    serials: Iterable[str],
    capacity_estimates: Dict[str, float],
    nominal_power_w: Dict[str, float],
    min_inverter_pct: float = 0.0,
) -> Dict[str, float]:
    active = list(serials)
    remaining = max(0.0, total_target_w)
    allocation: Dict[str, float] = {}

    while active:
        total_nominal_active = sum(nominal_power_w.get(s, 0.0) for s in active)
        if total_nominal_active > 0:
            share_pct = remaining / total_nominal_active
            shares = {s: share_pct * nominal_power_w.get(s, 0.0) for s in active}
        else:
            # No nominal-power data for any remaining inverter -- fall back
            # to an equal-watts split so this still terminates sensibly
            # rather than dividing by zero.
            equal_share = remaining / len(active)
            shares = {s: equal_share for s in active}

        saturated = [s for s in active if capacity_estimates.get(s, INFINITE) <= shares[s]]
        if not saturated:
            allocation.update(shares)
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
    if min_inverter_pct > 0:
        for s in list(allocation.keys()):
            cap = capacity_estimates.get(s, INFINITE)
            if cap <= 0:
                continue
            floor_w = min_inverter_pct / 100.0 * nominal_power_w.get(s, 0.0)
            if floor_w > allocation[s]:
                allocation[s] = min(cap, floor_w)

    return allocation

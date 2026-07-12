"""Hourly-bucketed grid energy import/export for the dashboard bar chart.

Derived from the grid meter's cumulative from-net/to-net counters
(/Ac/Energy/Forward, /Ac/Energy/Reverse -- see src/grid_meter.py and
src/grid_meter_modbus.py): each record() call computes the delta since the
last reading and adds it to the current wall-clock hour's bucket.

Thread-safe like live_state.LiveState -- written by the control loop, read
by the webui dashboard. Lost on every service restart, same as LiveState:
this is a rolling recent-history view, not a persistent energy meter log.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import List, Optional

DEFAULT_RETAIN_HOURS = 48
SECONDS_PER_HOUR = 3600


def _hour_start(t: float) -> float:
    return t - (t % SECONDS_PER_HOUR)


class HourlyEnergyHistory:
    def __init__(self, retain_hours: int = DEFAULT_RETAIN_HOURS):
        self._lock = threading.Lock()
        self._buckets: deque = deque(maxlen=retain_hours + 1)  # +1 for the in-progress hour
        self._last_from_kwh: Optional[float] = None
        self._last_to_kwh: Optional[float] = None

    def record(self, from_kwh: float, to_kwh: float, now: Optional[float] = None) -> None:
        t = now if now is not None else time.time()
        hour = _hour_start(t)
        with self._lock:
            if self._last_from_kwh is None:
                # First reading (including right after a restart, when this
                # object's state -- but not the meter's own counter -- has
                # been lost): nothing to diff against yet, so start an empty
                # bucket rather than attributing the meter's entire lifetime
                # total to a single hour.
                if not self._buckets or self._buckets[-1]["hour"] != hour:
                    self._buckets.append({"hour": hour, "from_kwh": 0.0, "to_kwh": 0.0})
            else:
                delta_from = from_kwh - self._last_from_kwh
                delta_to = to_kwh - self._last_to_kwh
                # A cumulative counter only ever increases; a drop means the
                # meter/counter was reset (replaced, or a different source
                # after a restart) -- skip this one delta rather than
                # recording a bogus negative bar.
                if delta_from >= 0 and delta_to >= 0:
                    if self._buckets and self._buckets[-1]["hour"] == hour:
                        self._buckets[-1]["from_kwh"] += delta_from
                        self._buckets[-1]["to_kwh"] += delta_to
                    else:
                        self._buckets.append({"hour": hour, "from_kwh": delta_from, "to_kwh": delta_to})
            self._last_from_kwh = from_kwh
            self._last_to_kwh = to_kwh

    def snapshot(self) -> List[dict]:
        with self._lock:
            return [dict(b) for b in self._buckets]

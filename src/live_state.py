"""Thread-safe in-memory ring buffer of recent control-loop samples.

Consumed by the live dashboard (src/webui.py) to draw near-real-time charts
(grid power, SOC, per-inverter power) without a database and without
touching the control loop's own scheduling. Written by the control loop
(src/main.py) every fast-loop tick; read by the webui HTTP server's request
threads. The lock only needs to guard against torn reads while the deque is
being appended to -- samples are immutable dicts once built, never mutated
in place.

Lost on every service restart (including the "Enregistrer et appliquer"
button in the config page) -- this is a live view, not a historian.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import List, Optional

DEFAULT_MAX_SAMPLES = 900  # ~30 min of history at the default grid.read_interval_s=2s


class LiveState:
    def __init__(self, max_samples: int = DEFAULT_MAX_SAMPLES):
        self._lock = threading.Lock()
        self._history: deque = deque(maxlen=max_samples)
        self._soc_pct: Optional[float] = None
        self._battery_power_w: Optional[float] = None
        self._injection_control: Optional[str] = None
        self._consigne_w: Optional[float] = None
        self._inverters: List[dict] = []

    def update_decision(
        self,
        soc_pct: Optional[float],
        injection_control: str,
        consigne_w: Optional[float],
        inverters: List[dict],
        battery_power_w: Optional[float] = None,
    ) -> None:
        """Called once per decision cycle (control.decision_interval_s) --
        carried forward into every grid sample recorded until the next one."""
        with self._lock:
            self._soc_pct = soc_pct
            self._battery_power_w = battery_power_w
            self._injection_control = injection_control
            self._consigne_w = consigne_w
            self._inverters = list(inverters)

    def record_grid(self, grid_raw_w: float, grid_ema_w: float) -> None:
        """Called once per fast-loop tick (grid.read_interval_s) -- this is
        what sets the sampling rate of the history buffer."""
        with self._lock:
            sample = {
                "t": time.time(),
                "grid_raw_w": grid_raw_w,
                "grid_ema_w": grid_ema_w,
                "soc_pct": self._soc_pct,
                "battery_power_w": self._battery_power_w,
                "injection_control": self._injection_control,
                "consigne_w": self._consigne_w,
                "inverters": self._inverters,
            }
            self._history.append(sample)

    def snapshot_since(self, since: float = 0.0) -> dict:
        """History strictly newer than `since` (epoch seconds), plus the
        latest sample regardless -- lets a client poll incrementally after
        an initial full fetch (since=0)."""
        with self._lock:
            history = [s for s in self._history if s["t"] > since]
            latest = self._history[-1] if self._history else None
        return {"latest": latest, "history": history}

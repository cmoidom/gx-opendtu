"""Thread-safe manual override: force all inverters to a fixed relative %
from the dashboard, for a bounded duration, then automatically resume
normal PI control -- no risk of a forgotten override exporting indefinitely.

Written by the webui HTTP server threads (src/webui.py), read by the
control loop (src/main.py) once per decision cycle. Deliberately checked
only where main.run() would otherwise call _decision_cycle: a lost grid
meter (fail-safe) or battery-charge-priority release are both checked
earlier in that loop and always take priority over an active override --
this module has no way to prevent that, by construction, not by convention.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

DEFAULT_DURATION_S = 300.0  # 5 minutes


class ManualOverride:
    def __init__(self):
        self._lock = threading.Lock()
        self._pct: Optional[float] = None
        self._expires_at: Optional[float] = None

    def set(self, pct: float, duration_s: float = DEFAULT_DURATION_S) -> None:
        with self._lock:
            self._pct = pct
            self._expires_at = time.monotonic() + duration_s

    def clear(self) -> None:
        with self._lock:
            self._pct = None
            self._expires_at = None

    def active_pct(self) -> Optional[float]:
        """The forced percentage if the override is still within its
        window, else None -- also clears the override once expired, so a
        single call both checks and lets it lapse."""
        with self._lock:
            if self._pct is None:
                return None
            if time.monotonic() >= self._expires_at:
                self._pct = None
                self._expires_at = None
                return None
            return self._pct

    def snapshot(self) -> Optional[dict]:
        """{"pct": ..., "remaining_s": ...} for the dashboard, or None if inactive."""
        with self._lock:
            if self._pct is None:
                return None
            remaining = self._expires_at - time.monotonic()
            if remaining <= 0:
                return None
            return {"pct": self._pct, "remaining_s": remaining}


MODES = ("AUTO", "ON", "OFF")


class InjectionModeOverride:
    """Sticky AUTO/ON/OFF override for which branch of the control loop
    runs (SOC-hysteresis-driven PI curtailment vs charge-priority release).

    Distinct from ManualOverride above: this doesn't bypass the PI or
    auto-expire -- its purpose is to let a user un-stick a wrong hysteresis
    state (e.g. right after a restart, before state_store's persisted value
    or BatteryFullHysteresis.update's own export-detection heuristic have
    had a chance to correct it) or deliberately hold a mode for as long as
    needed. ON/OFF directly set BatteryFullHysteresis.active (see
    main.run()), so switching back to AUTO resumes hysteresis-driven
    behaviour from wherever that left it, not from scratch.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._mode = "AUTO"

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        with self._lock:
            self._mode = mode

    def get_mode(self) -> str:
        with self._lock:
            return self._mode

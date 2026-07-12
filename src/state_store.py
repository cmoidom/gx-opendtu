"""Tiny on-disk persistence for the one piece of control-loop state that
must survive a restart: the battery-full hysteresis latch
(controller.BatteryFullHysteresis.active).

Everything else live (LiveState, HourlyEnergyHistory) is deliberately
ephemeral -- this is the one exception, because losing it silently changes
real regulation behaviour: without it, a restart while the battery is
already full resets the latch to "not yet full", which can leave injection
control stuck OFF (uncapped inverters) until SOC climbs all the way back to
activate_at_pct, possibly never happening exactly if it plateaus just below
it. Written next to config.json; missing/corrupt is not an error, just
"unknown" (caller decides the safe default -- see main.run()).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger("gx-opendtu-zero-export")


def _state_path(config_path: str) -> str:
    directory = os.path.dirname(os.path.abspath(config_path))
    return os.path.join(directory, "state.json")


def load_injection_active(config_path: str) -> Optional[bool]:
    try:
        with open(_state_path(config_path), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    value = data.get("injection_active")
    return value if isinstance(value, bool) else None


def save_injection_active(config_path: str, active: bool) -> None:
    path = _state_path(config_path)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"injection_active": active}, f)
        os.replace(tmp_path, path)
    except OSError as exc:
        log.error("failed to persist injection_active state to %s: %s", path, exc)

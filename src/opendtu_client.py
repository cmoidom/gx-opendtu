"""Minimal HTTP client for the OpenDTU REST API (no MQTT, no external deps).

Uses only the stdlib (urllib) so nothing needs to be installed on Venus OS.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List

# limit_type values, see OpenDTU ActivePowerControlCommand.h. Persistent
# variants write to inverter flash and are deliberately not exposed here -
# this client only ever sends non-persistent limits.
LIMIT_TYPE_ABSOLUTE_NONPERSISTENT = 0
LIMIT_TYPE_RELATIVE_NONPERSISTENT = 1


class OpenDTUError(Exception):
    pass


@dataclass
class InverterInfo:
    serial: str
    name: str
    max_power_w: float


@dataclass
class LimitStatus:
    limit_relative: float
    max_power: float
    limit_set_status: str

    @property
    def acknowledged(self) -> bool:
        return self.limit_set_status == "Ok"


def _extract_value(node) -> float:
    """OpenDTU numeric fields are either a bare number or {"v": ..., "u": ..., "d": ...}."""
    if isinstance(node, dict):
        return float(node.get("v", 0.0))
    return float(node or 0.0)


class OpenDTUClient:
    def __init__(self, base_url: str, timeout_s: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise OpenDTUError(f"GET {url} failed: {exc}") from exc

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        body = urllib.parse.urlencode({"data": json.dumps(payload)}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise OpenDTUError(f"POST {url} failed: {exc}") from exc

    def get_live_power_w(self) -> Dict[str, float]:
        """Returns {serial: current_ac_power_w} for every reachable inverter."""
        data = self._get("/api/livedata/status")
        result: Dict[str, float] = {}
        for inv in data.get("inverters", []):
            serial = str(inv.get("serial"))
            ac = inv.get("AC", {})
            channel0 = ac.get("0", ac)
            power_node = channel0.get("Power") if isinstance(channel0, dict) else None
            result[serial] = _extract_value(power_node)
        return result

    def list_inverters(self) -> List[InverterInfo]:
        """All inverters OpenDTU currently knows about, with their rated
        power -- used by the config web UI (src/webui.py) to let a user pick
        which ones to manage instead of typing serial/power by hand.

        There is no dedicated "/api/inverter/list" endpoint in OpenDTU:
        serial/name come from /api/livedata/status, rated power (max_power)
        from /api/limit/status (see ARCHITECTURE.md).
        """
        livedata = self._get("/api/livedata/status")
        limit_status = self.get_limit_status()
        result: List[InverterInfo] = []
        for inv in livedata.get("inverters", []):
            serial = str(inv.get("serial"))
            status = limit_status.get(serial)
            result.append(
                InverterInfo(
                    serial=serial,
                    name=str(inv.get("name", "")),
                    max_power_w=status.max_power if status is not None else 0.0,
                )
            )
        return result

    def get_limit_status(self) -> Dict[str, LimitStatus]:
        data = self._get("/api/limit/status")
        result: Dict[str, LimitStatus] = {}
        for serial, status in data.items():
            result[serial] = LimitStatus(
                limit_relative=float(status.get("limit_relative", 0.0)),
                max_power=float(status.get("max_power", 0.0)),
                limit_set_status=str(status.get("limit_set_status", "Unknown")),
            )
        return result

    def set_absolute_limit_w(self, serial: str, watts: float) -> None:
        self._post(
            "/api/limit/config",
            {
                "serial": serial,
                "limit_type": LIMIT_TYPE_ABSOLUTE_NONPERSISTENT,
                "limit_value": round(watts),
            },
        )

    def set_relative_limit_pct(self, serial: str, percent: float) -> None:
        self._post(
            "/api/limit/config",
            {
                "serial": serial,
                "limit_type": LIMIT_TYPE_RELATIVE_NONPERSISTENT,
                "limit_value": round(percent),
            },
        )

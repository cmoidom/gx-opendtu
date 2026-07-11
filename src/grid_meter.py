"""Reads instantaneous grid power from the Victron grid meter over D-Bus.

Single-phase installation: only the aggregate /Ac/Power path is needed.
Positive = importing from the grid, negative = exporting to it.

`dbus` is only imported inside functions (not at module level) so this module
-- and anything that imports it, like src.main -- stays importable on a dev
machine without `dbus-python` installed (e.g. for unit tests exercising the
decision-cycle logic via a fake OpenDTU client). Actually calling these
functions still requires running on Venus OS itself.
"""

from __future__ import annotations

from typing import Optional

GRID_SERVICE_PREFIX = "com.victronenergy.grid."
BUS_ITEM_IFACE = "com.victronenergy.BusItem"


class GridMeterUnavailable(Exception):
    pass


def get_system_bus():
    import dbus

    return dbus.SystemBus()


def find_grid_service(bus) -> Optional[str]:
    for name in bus.list_names():
        if name.startswith(GRID_SERVICE_PREFIX):
            return str(name)
    return None


def read_grid_power_w(bus, service_name: Optional[str] = None) -> float:
    import dbus

    service = service_name or find_grid_service(bus)
    if service is None:
        raise GridMeterUnavailable(f"no {GRID_SERVICE_PREFIX}* service found on D-Bus")
    try:
        obj = bus.get_object(service, "/Ac/Power")
        value = dbus.Interface(obj, BUS_ITEM_IFACE).GetValue()
    except dbus.DBusException as exc:
        raise GridMeterUnavailable(f"failed to read {service}/Ac/Power: {exc}") from exc
    if value is None:
        raise GridMeterUnavailable(f"{service}/Ac/Power is invalid (no meter data)")
    return float(value)


class DbusGridMeter:
    """Grid reader for running directly on the Cerbo GX itself (local D-Bus)."""

    def __init__(self, service_name: Optional[str] = None):
        self.service_name = service_name
        self._bus = None

    def read_grid_power_w(self) -> float:
        if self._bus is None:
            self._bus = get_system_bus()
        return read_grid_power_w(self._bus, self.service_name)

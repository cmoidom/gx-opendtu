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
ENERGY_FORWARD_PATH = "/Ac/Energy/Forward"  # cumulative kWh imported from the grid
ENERGY_REVERSE_PATH = "/Ac/Energy/Reverse"  # cumulative kWh exported to the grid


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


def _read_bus_item(bus, service: str, path: str) -> float:
    import dbus

    try:
        obj = bus.get_object(service, path)
        value = dbus.Interface(obj, BUS_ITEM_IFACE).GetValue()
    except dbus.DBusException as exc:
        raise GridMeterUnavailable(f"failed to read {service}{path}: {exc}") from exc
    if value is None:
        raise GridMeterUnavailable(f"{service}{path} is invalid (no meter data)")
    return float(value)


def read_grid_power_w(bus, service_name: Optional[str] = None) -> float:
    service = service_name or find_grid_service(bus)
    if service is None:
        raise GridMeterUnavailable(f"no {GRID_SERVICE_PREFIX}* service found on D-Bus")
    return _read_bus_item(bus, service, "/Ac/Power")


def read_grid_energy_kwh(bus, service_name: Optional[str] = None) -> "tuple[float, float]":
    """Returns (energy_from_net_kwh, energy_to_net_kwh) -- cumulative totals
    since the meter's own counter was last reset, not a rate."""
    service = service_name or find_grid_service(bus)
    if service is None:
        raise GridMeterUnavailable(f"no {GRID_SERVICE_PREFIX}* service found on D-Bus")
    from_kwh = _read_bus_item(bus, service, ENERGY_FORWARD_PATH)
    to_kwh = _read_bus_item(bus, service, ENERGY_REVERSE_PATH)
    return from_kwh, to_kwh


class DbusGridMeter:
    """Grid reader for running directly on the Cerbo GX itself (local D-Bus)."""

    def __init__(self, service_name: Optional[str] = None):
        self.service_name = service_name
        self._bus = None

    def read_grid_power_w(self) -> float:
        if self._bus is None:
            self._bus = get_system_bus()
        return read_grid_power_w(self._bus, self.service_name)

    def read_energy_kwh(self) -> "tuple[float, float]":
        if self._bus is None:
            self._bus = get_system_bus()
        return read_grid_energy_kwh(self._bus, self.service_name)

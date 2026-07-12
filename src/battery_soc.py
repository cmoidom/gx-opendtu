"""Reads the Cerbo GX's aggregated battery State of Charge over D-Bus.

Used to gate injection control: while the battery isn't yet full, curtailment
is released (inverters run uncapped) so the Victron ESS/battery charger --
not this project -- absorbs AC-coupled PV surplus by charging the battery.
Only once the battery is full does this project need to actively curtail to
avoid real grid export (see src/controller.BatteryFullHysteresis).

Single system-wide aggregate (com.victronenergy.system /Dc/Battery/Soc),
same rationale as grid power: correct regardless of how many physical
battery packs/monitors are behind the system, no per-install device
instance lookup needed.
"""

from __future__ import annotations

SYSTEM_SERVICE = "com.victronenergy.system"
SOC_PATH = "/Dc/Battery/Soc"
POWER_PATH = "/Dc/Battery/Power"
BUS_ITEM_IFACE = "com.victronenergy.BusItem"


class BatterySocUnavailable(Exception):
    pass


class DbusBatterySoc:
    def __init__(self):
        self._bus = None

    def _bus_instance(self):
        if self._bus is None:
            import dbus

            self._bus = dbus.SystemBus()
        return self._bus

    def _read_path(self, path: str) -> float:
        import dbus

        bus = self._bus_instance()
        try:
            obj = bus.get_object(SYSTEM_SERVICE, path)
            value = dbus.Interface(obj, BUS_ITEM_IFACE).GetValue()
        except dbus.DBusException as exc:
            raise BatterySocUnavailable(f"failed to read {SYSTEM_SERVICE}{path}: {exc}") from exc
        if value is None:
            raise BatterySocUnavailable(f"{SYSTEM_SERVICE}{path} is invalid (no battery data)")
        return float(value)

    def read_soc_pct(self) -> float:
        return self._read_path(SOC_PATH)

    def read_power_w(self) -> float:
        """Positive = charging, negative = discharging -- dashboard display only,
        not used by the injection-control gating logic (SOC alone drives that)."""
        return self._read_path(POWER_PATH)

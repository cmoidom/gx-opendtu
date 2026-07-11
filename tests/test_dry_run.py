from src.controller import CapacityEstimator, SoftTargetController
from src.main import _apply_failsafe, _decision_cycle
from src.opendtu_client import LimitStatus


class FakeOpenDTUClient:
    """Stand-in for OpenDTUClient: no real HTTP, just records what would be sent."""

    def __init__(self, live_power_w, limit_status):
        self._live_power_w = live_power_w
        self._limit_status = limit_status
        self.absolute_calls = []
        self.relative_calls = []

    def get_live_power_w(self):
        return dict(self._live_power_w)

    def get_limit_status(self):
        return dict(self._limit_status)

    def set_absolute_limit_w(self, serial, watts):
        self.absolute_calls.append((serial, watts))

    def set_relative_limit_pct(self, serial, percent):
        self.relative_calls.append((serial, percent))


def _make_controller():
    return SoftTargetController(
        export_setpoint_w=30,
        kp=0.4,
        ki=0.0,
        step_absolute_w=100,
        step_relative_pct=0,
        min_change_w=5,
    )


def _make_capacity():
    return CapacityEstimator(nominal_power_w={"a": 600, "b": 400}, probe_step_w=10)


def test_dry_run_never_calls_opendtu_write_endpoints():
    client = FakeOpenDTUClient(
        live_power_w={"a": 200.0, "b": 150.0},
        limit_status={
            "a": LimitStatus(limit_relative=100, max_power=600, limit_set_status="Ok"),
            "b": LimitStatus(limit_relative=100, max_power=400, limit_set_status="Ok"),
        },
    )
    _decision_cycle(
        client,
        _make_controller(),
        _make_capacity(),
        ["a", "b"],
        grid_power_avg_w=100.0,
        dry_run=True,
    )
    assert client.absolute_calls == []


def test_normal_mode_calls_opendtu_on_first_decision():
    client = FakeOpenDTUClient(
        live_power_w={"a": 200.0, "b": 150.0},
        limit_status={
            "a": LimitStatus(limit_relative=100, max_power=600, limit_set_status="Ok"),
            "b": LimitStatus(limit_relative=100, max_power=400, limit_set_status="Ok"),
        },
    )
    _decision_cycle(
        client,
        _make_controller(),
        _make_capacity(),
        ["a", "b"],
        grid_power_avg_w=100.0,
        dry_run=False,
    )
    assert len(client.absolute_calls) == 2


def test_dry_run_failsafe_never_calls_opendtu():
    client = FakeOpenDTUClient(live_power_w={}, limit_status={})
    _apply_failsafe(client, ["a", "b"], dry_run=True)
    assert client.relative_calls == []


def test_normal_failsafe_curtails_every_inverter_to_zero():
    client = FakeOpenDTUClient(live_power_w={}, limit_status={})
    _apply_failsafe(client, ["a", "b"], dry_run=False)
    assert client.relative_calls == [("a", 0), ("b", 0)]

import time

from src.live_state import LiveState


def test_record_grid_carries_forward_latest_decision():
    state = LiveState(max_samples=10)
    state.update_decision(soc_pct=80.0, injection_control="ON", consigne_w=300.0, inverters=[{"serial": "a"}])
    state.record_grid(grid_raw_w=10.0, grid_ema_w=8.0)

    snap = state.snapshot_since(0.0)
    assert len(snap["history"]) == 1
    sample = snap["history"][0]
    assert sample["grid_raw_w"] == 10.0
    assert sample["grid_ema_w"] == 8.0
    assert sample["soc_pct"] == 80.0
    assert sample["injection_control"] == "ON"
    assert sample["consigne_w"] == 300.0
    assert sample["inverters"] == [{"serial": "a"}]
    assert snap["latest"] == sample


def test_record_grid_before_any_decision_has_none_fields():
    state = LiveState(max_samples=10)
    state.record_grid(grid_raw_w=5.0, grid_ema_w=5.0)

    sample = state.snapshot_since(0.0)["history"][0]
    assert sample["soc_pct"] is None
    assert sample["injection_control"] is None
    assert sample["consigne_w"] is None
    assert sample["inverters"] == []


def test_ring_buffer_respects_max_samples():
    state = LiveState(max_samples=3)
    for i in range(5):
        state.record_grid(grid_raw_w=float(i), grid_ema_w=float(i))

    history = state.snapshot_since(0.0)["history"]
    assert len(history) == 3
    assert [s["grid_raw_w"] for s in history] == [2.0, 3.0, 4.0]


def test_snapshot_since_filters_older_samples():
    state = LiveState(max_samples=10)
    state.record_grid(1.0, 1.0)
    first_t = state.snapshot_since(0.0)["latest"]["t"]
    time.sleep(0.01)  # ensure a distinct, strictly-later timestamp regardless of clock resolution
    state.record_grid(2.0, 2.0)

    snap = state.snapshot_since(first_t)
    assert len(snap["history"]) == 1
    assert snap["history"][0]["grid_raw_w"] == 2.0
    # latest is always returned regardless of `since`
    assert snap["latest"]["grid_raw_w"] == 2.0


def test_mutating_inverters_list_after_call_does_not_affect_recorded_sample():
    state = LiveState(max_samples=10)
    inverters = [{"serial": "a"}]
    state.update_decision(soc_pct=None, injection_control="ON", consigne_w=100.0, inverters=inverters)
    inverters.append({"serial": "b"})
    state.record_grid(1.0, 1.0)

    sample = state.snapshot_since(0.0)["history"][0]
    assert sample["inverters"] == [{"serial": "a"}]

from src.controller import (
    CapacityEstimator,
    GridPowerSmoother,
    SoftTargetController,
    quantize,
    ramp_limit,
)


def test_grid_power_smoother_first_sample_is_passed_through():
    smoother = GridPowerSmoother(alpha=0.5)
    assert smoother.add(100) == 100.0


def test_grid_power_smoother_applies_exponential_moving_average():
    smoother = GridPowerSmoother(alpha=0.5)
    smoother.add(100)
    # filtered = 100 + 0.5*(200-100) = 150
    assert smoother.add(200) == 150.0
    # filtered = 150 + 0.5*(0-150) = 75
    assert smoother.add(0) == 75.0


def test_grid_power_smoother_alpha_one_tracks_raw_value_instantly():
    smoother = GridPowerSmoother(alpha=1.0)
    smoother.add(100)
    assert smoother.add(500) == 500.0


def test_grid_power_smoother_rejects_invalid_alpha():
    for bad_alpha in (0, -0.1, 1.1):
        try:
            GridPowerSmoother(alpha=bad_alpha)
            assert False, f"expected ValueError for alpha={bad_alpha}"
        except ValueError:
            pass


def test_quantize_rounds_to_nearest_step():
    assert quantize(149, 100) == 100
    assert quantize(151, 100) == 200
    assert quantize(0, 100) == 0


def test_quantize_with_zero_step_is_noop():
    assert quantize(123.4, 0) == 123.4


def test_ramp_limit_caps_movement_per_cycle():
    assert ramp_limit(current=100, target=1000, max_step=100) == 200
    assert ramp_limit(current=100, target=50, max_step=100) == 50
    assert ramp_limit(current=100, target=100, max_step=100) == 100


def test_soft_target_controller_is_quantized_and_rate_limited():
    controller = SoftTargetController(
        export_setpoint_w=30,
        kp=1.0,
        ki=0.0,
        step_absolute_w=100,
        step_relative_pct=0,
        min_change_w=5,
    )
    # First call always "changes" (no previous baseline) and establishes the target.
    first = controller.compute_target(grid_power_avg_w=30, current_total_actual_w=200, total_capacity_w=1000)
    assert first.changed is True
    assert first.target_w == 200.0

    # Small error shouldn't move the target by more than a quantized step, and
    # shouldn't fire a change if it stays within min_change_w of last sent value.
    second = controller.compute_target(grid_power_avg_w=32, current_total_actual_w=200, total_capacity_w=1000)
    assert second.changed is False
    assert second.target_w == 200.0


def test_soft_target_controller_ramps_large_jumps_over_multiple_cycles():
    controller = SoftTargetController(
        export_setpoint_w=0,
        kp=2.0,
        ki=0.0,
        step_absolute_w=100,
        step_relative_pct=0,
        min_change_w=5,
    )
    # Establish a baseline at 0 first.
    first = controller.compute_target(grid_power_avg_w=0, current_total_actual_w=0, total_capacity_w=2000)
    assert first.target_w == 0.0

    # Then a big swing to heavy import (lots of headroom to raise production):
    # the jump must be spread over several decision cycles, not applied at once.
    second = controller.compute_target(grid_power_avg_w=1000, current_total_actual_w=0, total_capacity_w=2000)
    assert second.target_w == 100.0  # capped to one step this cycle

    third = controller.compute_target(grid_power_avg_w=1000, current_total_actual_w=0, total_capacity_w=2000)
    assert third.target_w == 200.0  # ramps up by another step


def test_effective_step_uses_larger_of_absolute_and_relative():
    controller = SoftTargetController(
        export_setpoint_w=0,
        kp=1.0,
        ki=0.0,
        step_absolute_w=100,
        step_relative_pct=10,
        min_change_w=5,
    )
    # 10% of 3000W (300W) is larger than the 100W absolute floor.
    assert controller.effective_step_w(3000) == 300.0
    # 10% of 500W (50W) is smaller than the 100W absolute floor.
    assert controller.effective_step_w(500) == 100.0


def test_capacity_estimator_lowers_ceiling_when_inverter_cannot_keep_up():
    estimator = CapacityEstimator(nominal_power_w={"a": 600}, probe_step_w=10)
    assert estimator.ceilings_w["a"] == 600

    # Allocated 550W (near the 600W ceiling -- we were actually testing the
    # limit), but only 250W actually produced, and OpenDTU confirms the
    # limit itself isn't what's holding it back (limit_acknowledged=True at a
    # higher value) -> assume irradiance-limited, cap drops to actual output.
    estimator.observe("a", allocated_w=550, actual_w=250, limit_acknowledged=True)
    assert estimator.ceilings_w["a"] == 250

    # A slow probe should nudge the ceiling back up towards nominal.
    estimator.probe_tick()
    assert estimator.ceilings_w["a"] == 260
    estimator.probe_tick()
    assert estimator.ceilings_w["a"] == 270


def test_capacity_estimator_keeps_ceiling_when_inverter_keeps_up():
    estimator = CapacityEstimator(nominal_power_w={"a": 600}, probe_step_w=10)
    estimator.observe("a", allocated_w=400, actual_w=400, limit_acknowledged=True)
    assert estimator.ceilings_w["a"] == 600


def test_capacity_estimator_ignores_shortfall_when_target_was_not_near_ceiling():
    # The zero-export target is often well below any inverter's real max on
    # purpose -- a small shortfall against a modest allocated share (here
    # 400W out of a 600W ceiling, 67%) is ordinary noise, not proof the
    # inverter can't do more. Must NOT collapse the ceiling.
    estimator = CapacityEstimator(nominal_power_w={"a": 600}, probe_step_w=10)
    estimator.observe("a", allocated_w=400, actual_w=380, limit_acknowledged=True)
    assert estimator.ceilings_w["a"] == 600


def test_capacity_estimator_near_ceiling_threshold_is_90_percent():
    estimator = CapacityEstimator(nominal_power_w={"a": 600}, probe_step_w=10)
    # 89% of the 600W ceiling: just under the threshold -> ignored.
    estimator.observe("a", allocated_w=534, actual_w=500, limit_acknowledged=True)
    assert estimator.ceilings_w["a"] == 600

    # 90% of the current (still 600W) ceiling: at the threshold -> counts.
    estimator.observe("a", allocated_w=540, actual_w=500, limit_acknowledged=True)
    assert estimator.ceilings_w["a"] == 500

from src.allocator import water_fill_allocate


def test_equal_split_when_all_have_headroom():
    allocation = water_fill_allocate(600.0, ["a", "b", "c"], {"a": 600, "b": 600, "c": 600})
    assert allocation == {"a": 200.0, "b": 200.0, "c": 200.0}


def test_saturated_inverter_gets_capped_and_rest_redistributed():
    # "a" can only give 50W; the other 550W target should be split between b and c.
    allocation = water_fill_allocate(600.0, ["a", "b", "c"], {"a": 50, "b": 600, "c": 600})
    assert allocation["a"] == 50.0
    assert allocation["b"] == 275.0
    assert allocation["c"] == 275.0


def test_cascading_saturation():
    # a and b are both capacity-limited below their equal share, c absorbs the rest.
    allocation = water_fill_allocate(300.0, ["a", "b", "c"], {"a": 10, "b": 20, "c": 1000})
    assert allocation["a"] == 10.0
    assert allocation["b"] == 20.0
    assert allocation["c"] == 270.0


def test_zero_target_gives_zero_to_all():
    allocation = water_fill_allocate(0.0, ["a", "b"], {"a": 100, "b": 100})
    assert allocation == {"a": 0.0, "b": 0.0}


def test_missing_capacity_estimate_treated_as_unlimited():
    allocation = water_fill_allocate(100.0, ["a", "b"], {"a": 10})
    assert allocation["a"] == 10.0
    assert allocation["b"] == 90.0


def test_negative_target_clamped_to_zero():
    allocation = water_fill_allocate(-50.0, ["a", "b"], {"a": 100, "b": 100})
    assert allocation == {"a": 0.0, "b": 0.0}


def test_min_inverter_pct_floors_a_low_nonzero_share():
    # 10W total split two ways is 5W each; a 10% floor of 600W nominal is 60W.
    allocation = water_fill_allocate(
        10.0, ["a", "b"], {"a": 600, "b": 600}, min_inverter_pct=10.0, nominal_power_w={"a": 600, "b": 600}
    )
    assert allocation == {"a": 60.0, "b": 60.0}


def test_min_inverter_pct_never_overrides_a_genuine_zero():
    allocation = water_fill_allocate(
        0.0, ["a", "b"], {"a": 600, "b": 600}, min_inverter_pct=10.0, nominal_power_w={"a": 600, "b": 600}
    )
    assert allocation == {"a": 0.0, "b": 0.0}


def test_min_inverter_pct_never_exceeds_capacity_ceiling():
    # Floor would be 10% of 600 = 60W, but this inverter is shaded down to 20W -- must not exceed that.
    allocation = water_fill_allocate(
        5.0, ["a"], {"a": 20}, min_inverter_pct=10.0, nominal_power_w={"a": 600}
    )
    assert allocation == {"a": 20.0}


def test_min_inverter_pct_disabled_by_default():
    allocation = water_fill_allocate(10.0, ["a", "b"], {"a": 600, "b": 600})
    assert allocation == {"a": 5.0, "b": 5.0}

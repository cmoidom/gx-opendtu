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

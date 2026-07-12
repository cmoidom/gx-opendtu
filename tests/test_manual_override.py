import pytest

from src.manual_override import InjectionModeOverride, ManualOverride


def test_inactive_by_default():
    override = ManualOverride()
    assert override.active_pct() is None
    assert override.snapshot() is None


def test_set_makes_it_active():
    override = ManualOverride()
    override.set(50.0, duration_s=60.0)
    assert override.active_pct() == 50.0
    snap = override.snapshot()
    assert snap["pct"] == 50.0
    assert 0 < snap["remaining_s"] <= 60.0


def test_clear_deactivates_immediately():
    override = ManualOverride()
    override.set(100.0, duration_s=60.0)
    override.clear()
    assert override.active_pct() is None
    assert override.snapshot() is None


def test_expires_after_duration():
    override = ManualOverride()
    override.set(25.0, duration_s=-1.0)  # already expired
    assert override.active_pct() is None
    assert override.snapshot() is None


def test_active_pct_clears_state_once_expired():
    override = ManualOverride()
    override.set(75.0, duration_s=-1.0)
    override.active_pct()  # first call observes expiry and clears
    assert override.snapshot() is None


def test_set_again_replaces_previous_value_and_duration():
    override = ManualOverride()
    override.set(25.0, duration_s=60.0)
    override.set(100.0, duration_s=60.0)
    assert override.active_pct() == 100.0


def test_injection_mode_defaults_to_auto():
    mode = InjectionModeOverride()
    assert mode.get_mode() == "AUTO"


def test_injection_mode_can_be_set_to_on_or_off():
    mode = InjectionModeOverride()
    mode.set_mode("ON")
    assert mode.get_mode() == "ON"
    mode.set_mode("OFF")
    assert mode.get_mode() == "OFF"
    mode.set_mode("AUTO")
    assert mode.get_mode() == "AUTO"


def test_injection_mode_rejects_invalid_value():
    mode = InjectionModeOverride()
    with pytest.raises(ValueError):
        mode.set_mode("BOGUS")

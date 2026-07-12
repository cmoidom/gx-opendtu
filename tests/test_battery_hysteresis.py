from src.controller import BatteryFullHysteresis


def _make(active=False):
    return BatteryFullHysteresis(activate_at_pct=100.0, deactivate_below_pct=98.0, active=active)


def test_starts_inactive_and_stays_inactive_below_activation_threshold():
    h = _make()
    assert h.update(50) is False
    assert h.update(99) is False
    assert h.update(99.9) is False


def test_activates_only_at_the_activation_threshold():
    h = _make()
    assert h.update(100) is True


def test_no_yoyo_around_100_once_active():
    h = _make()
    h.update(100)
    assert h.active is True
    # Battery discharges a bit during the day, still well above the
    # deactivation threshold -- must NOT flip back off.
    assert h.update(99) is True
    assert h.update(98.5) is True
    assert h.update(98) is True


def test_deactivates_only_below_the_deactivation_threshold():
    h = _make()
    h.update(100)
    assert h.update(97.9) is False


def test_does_not_reactivate_until_back_to_100_after_deactivating():
    h = _make()
    h.update(100)
    h.update(97)  # deactivates
    assert h.active is False
    # Recovers to 99% (above the old deactivation threshold) but hasn't
    # reached the activation threshold again -- must stay off (no yoyo).
    assert h.update(99) is False
    assert h.update(99.9) is False
    assert h.update(100) is True


def test_initial_active_state_can_be_seeded():
    h = _make(active=True)
    assert h.update(99) is True
    assert h.update(97) is False

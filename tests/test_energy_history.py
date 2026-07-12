from src.energy_history import HourlyEnergyHistory, _hour_start


def test_hour_start_floors_to_the_hour():
    # 2024-01-01 12:34:56 UTC epoch -> floored to 12:00:00 UTC
    t = 1704112496.0  # 2024-01-01 12:34:56 UTC
    assert _hour_start(t) == 1704110400.0  # 2024-01-01 12:00:00 UTC


def test_first_reading_creates_an_empty_bucket_not_a_bogus_total():
    history = HourlyEnergyHistory()
    history.record(from_kwh=12345.0, to_kwh=6789.0, now=1704110400.0)
    snap = history.snapshot()
    assert len(snap) == 1
    assert snap[0] == {"hour": 1704110400.0, "from_kwh": 0.0, "to_kwh": 0.0}


def test_subsequent_reading_in_same_hour_accumulates_delta():
    history = HourlyEnergyHistory()
    history.record(from_kwh=100.0, to_kwh=50.0, now=1704110400.0)  # hour start
    history.record(from_kwh=100.5, to_kwh=50.2, now=1704110700.0)  # +5 min, same hour
    history.record(from_kwh=101.0, to_kwh=50.5, now=1704111000.0)  # +10 min, same hour
    snap = history.snapshot()
    assert len(snap) == 1
    assert snap[0]["from_kwh"] == 1.0
    assert round(snap[0]["to_kwh"], 4) == 0.5


def test_new_hour_starts_a_new_bucket():
    history = HourlyEnergyHistory()
    history.record(from_kwh=100.0, to_kwh=50.0, now=1704110400.0)  # 12:00
    history.record(from_kwh=101.0, to_kwh=50.5, now=1704113900.0)  # 12:58
    history.record(from_kwh=102.0, to_kwh=51.0, now=1704114100.0)  # 13:01 -> new hour
    snap = history.snapshot()
    assert len(snap) == 2
    assert snap[0]["hour"] == 1704110400.0
    assert snap[0]["from_kwh"] == 1.0
    assert snap[1]["hour"] == 1704114000.0
    assert snap[1]["from_kwh"] == 1.0


def test_counter_reset_skips_the_bogus_negative_delta():
    history = HourlyEnergyHistory()
    history.record(from_kwh=500.0, to_kwh=200.0, now=1704110400.0)
    history.record(from_kwh=5.0, to_kwh=200.0, now=1704110700.0)  # counter reset/replaced
    history.record(from_kwh=6.0, to_kwh=200.5, now=1704111000.0)  # resumes counting up from new baseline
    snap = history.snapshot()
    assert len(snap) == 1
    # the reset tick itself contributes nothing, only the delta after it
    assert snap[0]["from_kwh"] == 1.0
    assert round(snap[0]["to_kwh"], 4) == 0.5


def test_retain_hours_bounds_the_bucket_count():
    history = HourlyEnergyHistory(retain_hours=2)
    base = 1704110400.0
    for i in range(5):
        history.record(from_kwh=float(i), to_kwh=float(i), now=base + i * 3600)
    snap = history.snapshot()
    assert len(snap) == 3  # retain_hours + 1 (in-progress hour)

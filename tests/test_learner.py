"""learner.py: meter buffer, switch tracker and device statistics, with 10-second meter readings."""

from __future__ import annotations

import json
import math

import pytest

from watt_learner.learner import (
    REJECT_FLAPPING,
    REJECT_GAP,
    REJECT_NOISY,
    REJECT_OVERLAP,
    DeviceStats,
    Result,
    SampleBuffer,
    Settings,
    Tracker,
    group_state,
    member_is_on,
)

T = 1005.0  # switch time, between two meter readings (they arrive at 1000, 1010, ...)
JITTER = (0.0, 4.0, -3.0, 2.0, -5.0, 3.0, -1.0)


def fill(buffer: SampleBuffer, start: float, end: float, level, step: float = 10.0) -> None:
    """One reading every ``step`` seconds from start to end; ``level(ts)`` gives the watts."""
    ts = start
    i = 0
    while ts <= end:
        buffer.add(ts, level(ts) + JITTER[i % len(JITTER)])
        ts += step
        i += 1


def jump(before: float, after: float, at: float = T):
    return lambda ts: before if ts < at else after


def one_device(post_delay_s: float | None = None) -> tuple[Tracker, SampleBuffer]:
    tracker = Tracker()
    buffer = SampleBuffer()
    tracker.register("tv", buffer, post_delay_s)
    return tracker, buffer


def result_for(results: list[Result], device_id: str) -> Result:
    (found,) = [r for r in results if r.device_id == device_id]
    return found


# --- member_is_on / group_state ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "state", "expected"),
    [
        ("light", "on", True),
        ("light", "off", False),
        ("switch", "on", True),
        ("switch", "off", False),
        ("fan", "on", True),
        ("input_boolean", "off", False),
        ("light", "unavailable", None),
        ("light", "unknown", None),
        ("switch", None, None),
        ("switch", "standby", None),
        ("media_player", "off", False),
        ("media_player", "standby", False),
        ("media_player", "playing", True),
        ("media_player", "idle", True),
        ("media_player", "on", True),
        ("media_player", "paused", True),
        ("media_player", "unavailable", None),
        ("media_player", "unknown", None),
        ("media_player", None, None),
    ],
)
def test_member_is_on(domain, state, expected):
    assert member_is_on(domain, state) is expected


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        ([True], True),
        ([False], False),
        ([False, False, False], False),
        ([False, True, None], True),
        ([None, True], True),
        ([False, None], None),
        ([None], None),
        ([], None),
    ],
)
def test_group_state(members, expected):
    assert group_state(members) is expected


def test_group_state_accepts_a_generator():
    assert group_state(x for x in (False, False)) is False


# --- SampleBuffer -----------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [32970.0, -2_100_000_000.0, math.nan, math.inf, -math.inf])
def test_buffer_drops_read_errors(bad):
    buffer = SampleBuffer()
    assert buffer.add(100.0, 300.0)
    assert buffer.add(110.0, bad) is False
    assert buffer.add(120.0, 310.0)
    assert buffer.window(0, 200) == [300.0, 310.0]


def test_buffer_limit_is_configurable_and_inclusive():
    buffer = SampleBuffer(max_abs_w=1000.0)
    assert buffer.add(1.0, 1000.0)
    assert buffer.add(2.0, -1000.0)
    assert buffer.add(3.0, 1000.5) is False
    assert buffer.window(0, 10) == [1000.0, -1000.0]


def test_buffer_window_bounds_are_inclusive_and_ordered():
    buffer = SampleBuffer()
    for ts, w in ((30.0, 3.0), (10.0, 1.0), (20.0, 2.0), (40.0, 4.0)):
        buffer.add(ts, w)
    assert buffer.window(10.0, 30.0) == [1.0, 2.0, 3.0]
    assert buffer.window(11.0, 29.0) == [2.0]
    assert buffer.window(50.0, 60.0) == []


def test_buffer_forgets_readings_older_than_keep_s():
    buffer = SampleBuffer(keep_s=60.0)
    fill(buffer, 0.0, 200.0, lambda ts: 100.0)
    assert len(buffer) == 7  # 140 ... 200
    assert buffer.window(0.0, 139.0) == []


# --- Tracker: clean measurements --------------------------------------------------------------


def test_clean_on_measurement():
    tracker, buffer = one_device()
    fill(buffer, 900.0, 1200.0, jump(300.0, 400.0))
    tracker.transition("tv", True, T)
    (result,) = tracker.collect(T + 80.0)
    assert result.device_id == "tv"
    assert result.ts == T
    assert result.direction == "on"
    assert result.reject is None
    assert result.delta_w == pytest.approx(100.0, abs=6.0)


def test_off_measurement_is_positive():
    tracker, buffer = one_device()
    fill(buffer, 900.0, 1200.0, jump(400.0, 300.0))
    tracker.transition("tv", False, T)
    (result,) = tracker.collect(T + 80.0)
    assert result.direction == "off"
    assert result.reject is None
    assert result.delta_w == pytest.approx(100.0, abs=6.0)


def test_settling_time_is_skipped():
    # Inrush right after switching (first 15 s) must not land in the post window.
    tracker, buffer = one_device()
    fill(buffer, 900.0, 1200.0, lambda ts: 300.0 if ts < T else (900.0 if ts < T + 15 else 400.0))
    tracker.transition("tv", True, T)
    (result,) = tracker.collect(T + 80.0)
    assert result.reject is None
    assert result.delta_w == pytest.approx(100.0, abs=6.0)


def test_per_device_post_delay_moves_the_post_window():
    tracker, buffer = one_device(post_delay_s=40.0)
    # Settles only after 35 s: with the default 15 s the post window would be noisy.
    fill(buffer, 900.0, 1300.0, lambda ts: 300.0 if ts < T else (700.0 if ts < T + 35 else 400.0))
    tracker.transition("tv", True, T)
    assert tracker.next_due() == T + 100.0
    (result,) = tracker.collect(T + 100.0)
    assert result.reject is None
    assert result.delta_w == pytest.approx(100.0, abs=6.0)

    default_tracker = Tracker()
    default_tracker.register("tv", buffer)
    default_tracker.transition("tv", True, T)
    (default_result,) = default_tracker.collect(T + 80.0)
    assert default_result.reject == REJECT_NOISY


# --- Tracker: rejections ----------------------------------------------------------------------


def test_overlap_rejects_both_devices():
    tracker = Tracker()
    buffer = SampleBuffer()
    tracker.register("tv", buffer)
    tracker.register("lamp", buffer)
    fill(buffer, 900.0, 1300.0, lambda ts: 300.0 + (100.0 if ts >= T else 0.0) + (50.0 if ts >= T + 40 else 0.0))
    tracker.transition("tv", True, T)
    tracker.transition("lamp", True, T + 40.0)
    results = tracker.collect(T + 200.0)
    assert result_for(results, "tv").reject == REJECT_OVERLAP
    assert result_for(results, "lamp").reject == REJECT_OVERLAP
    assert all(r.delta_w is None for r in results)


def test_other_device_outside_isolation_window_does_not_overlap():
    tracker = Tracker()
    buffer = SampleBuffer()
    tracker.register("tv", buffer)
    tracker.register("lamp", buffer)
    fill(buffer, 900.0, 1400.0, lambda ts: 300.0 + (100.0 if ts >= T else 0.0) + (50.0 if ts >= T + 90 else 0.0))
    tracker.transition("tv", True, T)
    tracker.transition("lamp", True, T + 90.0)
    results = tracker.collect(T + 200.0)
    assert result_for(results, "tv").delta_w == pytest.approx(100.0, abs=6.0)
    assert result_for(results, "lamp").delta_w == pytest.approx(50.0, abs=6.0)


def test_isolation_window_boundary_is_inclusive():
    tracker = Tracker()
    buffer = SampleBuffer()
    tracker.register("tv", buffer)
    tracker.register("lamp", buffer)
    fill(buffer, 900.0, 1400.0, lambda ts: 300.0)
    tracker.transition("tv", True, T)
    tracker.transition("lamp", True, T + 75.0)
    results = tracker.collect(T + 300.0)
    assert {r.reject for r in results} == {REJECT_OVERLAP}


def test_flapping_rejects_both_switches():
    tracker, buffer = one_device()
    fill(buffer, 900.0, 1300.0, lambda ts: 400.0 if T <= ts < T + 30 else 300.0)
    tracker.transition("tv", True, T)
    tracker.transition("tv", False, T + 30.0)
    results = tracker.collect(T + 200.0)
    assert [(r.direction, r.reject) for r in results] == [("on", REJECT_FLAPPING), ("off", REJECT_FLAPPING)]


def test_gap_without_readings_after_the_switch():
    tracker, buffer = one_device()
    fill(buffer, 900.0, T, lambda ts: 300.0)
    tracker.transition("tv", True, T)
    (result,) = tracker.collect(T + 80.0)
    assert result.reject == REJECT_GAP
    assert result.delta_w is None


def test_gap_with_too_few_readings_before():
    tracker, buffer = one_device()
    fill(buffer, 990.0, 1200.0, jump(300.0, 400.0))  # only 990 and 1000 before the switch
    tracker.transition("tv", True, T)
    (result,) = tracker.collect(T + 80.0)
    assert result.reject == REJECT_GAP


def test_reading_in_the_last_two_seconds_is_not_part_of_the_pre_window():
    tracker, buffer = one_device()
    for ts in (960.0, 980.0):
        buffer.add(ts, 300.0)
    buffer.add(T - 1.0, 300.0)  # inside [T - 2, T]: ignored, so only two readings before
    fill(buffer, 1010.0, 1200.0, lambda ts: 400.0)
    tracker.transition("tv", True, T)
    (result,) = tracker.collect(T + 80.0)
    assert result.reject == REJECT_GAP


def test_noisy_before():
    tracker, buffer = one_device()
    fill(buffer, 900.0, 1200.0, lambda ts: (250.0 if int(ts / 10) % 2 else 350.0) if ts < T else 400.0)
    tracker.transition("tv", True, T)
    (result,) = tracker.collect(T + 80.0)
    assert result.reject == REJECT_NOISY


def test_noisy_after():
    tracker, buffer = one_device()
    fill(buffer, 900.0, 1200.0, lambda ts: 300.0 if ts < T else (340.0 if int(ts / 10) % 2 else 460.0))
    tracker.transition("tv", True, T)
    (result,) = tracker.collect(T + 80.0)
    assert result.reject == REJECT_NOISY


def test_post_std_threshold_is_looser_than_pre():
    # Std of 40 W: too much before the switch (30), acceptable after it (45).
    tracker, buffer = one_device()
    fill(buffer, 900.0, 1200.0, lambda ts: 300.0 if ts < T else (360.0 if int(ts / 10) % 2 else 440.0))
    tracker.transition("tv", True, T)
    (result,) = tracker.collect(T + 80.0)
    assert result.reject is None


# --- Tracker: due times and bookkeeping ------------------------------------------------------


def test_next_due_and_collect_timing():
    tracker, buffer = one_device()
    assert tracker.next_due() is None
    assert tracker.collect(T) == []
    fill(buffer, 900.0, 1200.0, jump(300.0, 400.0))
    tracker.transition("tv", True, T)
    assert tracker.next_due() == T + 75.0  # max(15 + 60, 75)
    assert tracker.collect(T + 74.9) == []
    assert tracker.next_due() == T + 75.0
    assert len(tracker.collect(T + 75.0)) == 1
    assert tracker.next_due() is None
    assert tracker.collect(T + 500.0) == []


def test_next_due_uses_isolation_when_it_is_longer():
    tracker = Tracker(Settings(isolation_s=120.0))
    tracker.register("tv", SampleBuffer())
    tracker.transition("tv", True, T)
    assert tracker.next_due() == T + 120.0


def test_next_due_is_the_earliest_open_switch():
    tracker = Tracker()
    tracker.register("tv", SampleBuffer(), post_delay_s=60.0)
    tracker.register("lamp", SampleBuffer())
    tracker.transition("tv", True, T)
    tracker.transition("lamp", True, T + 10.0)
    assert tracker.next_due() == T + 85.0  # lamp: T + 10 + 75; tv: T + 120
    first = tracker.collect(T + 85.0)
    assert [r.device_id for r in first] == ["lamp"]
    assert tracker.next_due() == T + 120.0


def test_results_come_oldest_first():
    tracker = Tracker()
    buffer = SampleBuffer()
    tracker.register("tv", buffer)
    tracker.register("lamp", buffer)
    tracker.transition("lamp", True, T + 500.0)
    tracker.transition("tv", True, T)
    results = tracker.collect(T + 1000.0)
    assert [r.device_id for r in results] == ["tv", "lamp"]


def test_late_collect_still_sees_the_neighbour():
    tracker = Tracker()
    buffer = SampleBuffer()
    tracker.register("tv", buffer)
    tracker.register("lamp", buffer)
    tracker.register("fan", buffer)
    tracker.transition("tv", True, T)
    tracker.transition("lamp", True, T + 50.0)
    tracker.transition("fan", True, T + 3000.0)  # prunes old history, but tv and lamp are still open
    results = tracker.collect(T + 3000.0)
    assert result_for(results, "tv").reject == REJECT_OVERLAP
    assert result_for(results, "lamp").reject == REJECT_OVERLAP


def test_old_switches_are_forgotten():
    tracker = Tracker()
    tracker.register("tv", SampleBuffer())
    tracker.transition("tv", True, T)
    tracker.collect(T + 80.0)
    keep = 2 * 75.0 + 15.0 + 60.0
    tracker.collect(T + keep - 1.0)
    assert len(tracker._history) == 1
    tracker.collect(T + keep + 1.0)
    assert tracker._history == []


def test_unregistered_device_is_ignored():
    tracker = Tracker()
    tracker.transition("ghost", True, T)
    assert tracker.next_due() is None


def test_unregister_drops_open_switches():
    tracker, buffer = one_device()
    tracker.transition("tv", True, T)
    tracker.unregister("tv")
    assert tracker.next_due() is None
    assert tracker.collect(T + 1000.0) == []
    tracker.unregister("tv")  # twice is fine


def test_unregistered_device_still_spoils_overlapping_measurements():
    tracker = Tracker()
    buffer = SampleBuffer()
    tracker.register("tv", buffer)
    tracker.register("lamp", buffer)
    fill(buffer, 900.0, 1300.0, lambda ts: 300.0)
    tracker.transition("lamp", True, T - 20.0)
    tracker.transition("tv", True, T)
    tracker.unregister("lamp")
    (result,) = tracker.collect(T + 80.0)
    assert result.reject == REJECT_OVERLAP


# --- DeviceStats ------------------------------------------------------------------------------


def measurement(delta: float, direction: str = "on", ts: float = T) -> Result:
    return Result("tv", ts, direction, delta, None)


def stats_with(*deltas: float, settings: Settings | None = None) -> DeviceStats:
    stats = DeviceStats(settings)
    for i, delta in enumerate(deltas):
        stats.add(measurement(delta, ts=T + i))
    return stats


def test_stats_learning_below_min_measurements():
    stats = stats_with(100.0, 101.0, 99.0, 100.0)
    assert stats.count == 4
    assert stats.learned_w is None
    assert stats.spread_w is None
    assert stats.confidence == "learning"
    assert stats.on_w == 100.0  # direction medians do not wait for min_measurements


def test_stats_median_and_mad():
    stats = stats_with(80.0, 120.0, 100.0, 110.0, 90.0, 300.0)
    assert stats.count == 6
    assert stats.learned_w == 105.0
    # deviations from 105: 25, 15, 5, 5, 15, 195 -> median 15
    assert stats.spread_w == 15.0


def test_stats_rounding():
    stats = stats_with(100.04, 100.06, 100.11, 100.0, 100.08)
    assert stats.learned_w == 100.1
    assert stats.spread_w == 0.0


@pytest.mark.parametrize(
    ("deltas", "expected"),
    [
        ((100.0,) * 5, "high"),
        ((70.0, 85.0, 100.0, 115.0, 130.0), "high"),  # MAD 15 = 15 % of 100
        ((60.0, 80.0, 100.0, 120.0, 140.0), "medium"),  # MAD 20
        ((65.0, 65.0, 100.0, 135.0, 135.0), "medium"),  # MAD 35 = 35 % of 100
        ((20.0, 60.0, 100.0, 140.0, 180.0), "low"),  # MAD 40
        ((10.0, 15.0, 20.0, 25.0, 30.0), "high"),  # small load: floor of 5 W
        ((5.0, 12.0, 20.0, 28.0, 35.0), "medium"),  # MAD 8: floor of 10 W
        ((0.0, 9.0, 20.0, 31.0, 40.0), "low"),  # MAD 11
        ((-100.0,) * 5, "high"),  # sign does not matter
    ],
)
def test_stats_confidence(deltas, expected):
    assert stats_with(*deltas).confidence == expected


def test_stats_on_and_off_medians():
    stats = DeviceStats()
    assert stats.on_w is None
    assert stats.off_w is None
    for delta in (100.0, 104.0, 102.0):
        stats.add(measurement(delta, "on"))
    for delta in (90.0, 96.0):
        stats.add(measurement(delta, "off"))
    assert stats.on_w == 102.0
    assert stats.off_w == 93.0


def test_stats_rejections_are_counted_not_measured():
    stats = DeviceStats()
    stats.add(Result("tv", T, "on", None, REJECT_GAP))
    stats.add(Result("tv", T, "on", None, REJECT_GAP))
    stats.add(Result("tv", T, "off", None, REJECT_NOISY))
    assert stats.count == 0
    assert stats.as_dict()["rejected"] == {REJECT_GAP: 2, REJECT_NOISY: 1}
    assert stats.as_dict()["last_reject"] == REJECT_NOISY
    assert stats.last is None


def test_stats_last():
    stats = DeviceStats()
    stats.add(measurement(100.0, "on", ts=10.0))
    stats.add(measurement(95.5, "off", ts=20.0))
    assert stats.last == {"ts": 20.0, "delta_w": 95.5, "direction": "off"}


def test_stats_max_stored_drops_the_oldest():
    stats = DeviceStats(Settings(max_stored=5))
    for i in range(8):
        stats.add(measurement(float(i), ts=float(i)))
    assert stats.count == 5
    assert [m["delta_w"] for m in stats.as_dict()["measurements"]] == [3.0, 4.0, 5.0, 6.0, 7.0]


def test_stats_as_dict_round_trip_through_json():
    stats = stats_with(80.0, 120.0, 100.0, 110.0, 90.0)
    stats.add(measurement(105.0, "off", ts=2000.0))
    stats.add(Result("tv", T, "on", None, REJECT_OVERLAP))
    data = json.loads(json.dumps(stats.as_dict()))
    restored = DeviceStats(data=data)
    assert restored.as_dict() == stats.as_dict()
    assert restored.learned_w == stats.learned_w
    assert restored.spread_w == stats.spread_w
    assert restored.confidence == stats.confidence
    assert restored.last == stats.last


def test_stats_as_dict_shape():
    stats = DeviceStats()
    assert stats.as_dict() == {"measurements": [], "rejected": {}, "last_reject": None}
    stats.add(measurement(50.0, "on", ts=1.0))
    assert stats.as_dict()["measurements"] == [{"ts": 1.0, "delta_w": 50.0, "direction": "on"}]


def test_stats_as_dict_is_a_copy():
    stats = stats_with(100.0)
    data = stats.as_dict()
    data["measurements"].clear()
    data["rejected"]["gap"] = 9
    assert stats.count == 1
    assert stats.as_dict()["rejected"] == {}


def test_stats_loading_trims_to_max_stored_and_skips_broken_entries():
    data = {
        "measurements": [{"ts": float(i), "delta_w": float(i), "direction": "on"} for i in range(10)]
        + [{"ts": 1.0}, {"ts": "x", "delta_w": 1.0, "direction": "on"}],
        "rejected": {"gap": 3},
        "last_reject": "gap",
    }
    stats = DeviceStats(Settings(max_stored=4), data)
    assert [m["delta_w"] for m in stats.as_dict()["measurements"]] == [6.0, 7.0, 8.0, 9.0]
    assert stats.as_dict()["rejected"] == {"gap": 3}


def test_stats_reset():
    stats = stats_with(100.0, 100.0, 100.0, 100.0, 100.0)
    stats.add(Result("tv", T, "on", None, REJECT_FLAPPING))
    stats.reset()
    assert stats.count == 0
    assert stats.learned_w is None
    assert stats.confidence == "learning"
    assert stats.last is None
    assert stats.as_dict() == {"measurements": [], "rejected": {}, "last_reject": None}


def test_tracker_results_feed_stats():
    tracker, buffer = one_device()
    stats = DeviceStats()
    for i in range(6):
        switch = 1005.0 + 1000.0 * i
        is_on = i % 2 == 1  # off at 1005, on at 2005, ...
        before, after = (300.0, 400.0) if is_on else (400.0, 300.0)
        fill(buffer, switch - 105.0, switch + 195.0, jump(before, after, switch))
        tracker.transition("tv", is_on, switch)
        for result in tracker.collect(switch + 80.0):
            stats.add(result)
    assert stats.count == 6
    assert stats.learned_w == pytest.approx(100.0, abs=6.0)
    assert stats.confidence == "high"

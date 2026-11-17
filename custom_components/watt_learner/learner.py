"""Learning core: how many watts a device or device group draws.

Pure Python, no Home Assistant import. The hub feeds meter readings into a SampleBuffer and
reports on/off transitions to the Tracker. Once a transition's windows have passed, the Tracker
compares the meter level shortly before and shortly after the switch and returns a Result.
DeviceStats collects the results of one device and derives the learned value (median) from them.

All timestamps are Unix seconds.
"""

from __future__ import annotations

import bisect
import itertools
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median, pstdev

REJECT_OVERLAP = "overlap"  # another observed device switched within the isolation window
REJECT_FLAPPING = "flapping"  # the same device switched again within the window
REJECT_GAP = "gap"  # too few meter readings before or after
REJECT_NOISY = "noisy"  # base load before or after too unsteady

REJECT_REASONS = (REJECT_OVERLAP, REJECT_FLAPPING, REJECT_GAP, REJECT_NOISY)

DIRECTION_ON = "on"
DIRECTION_OFF = "off"

# The pre window ends this many seconds before the switch, so the switch itself is not in it.
PRE_GUARD_S = 2.0

# A switch is evaluated this many seconds after its windows have passed: switch events can arrive
# late, and so can the meter reading at the very end of the post window.
GRACE_S = 8.0

# At most this many open switches; when another one comes in, the oldest is dropped.
MAX_PENDING = 500

# A meter reading this far ahead of both the newest reading and the wall clock is a clock error.
# Accepting it would prune every real reading against it.
MAX_FUTURE_S = 300.0

_UNKNOWN_STATES = ("unavailable", "unknown")
_MEDIA_PLAYER_OFF = ("off", "standby")


@dataclass(frozen=True)
class Settings:
    pre_window_s: float = 60.0  # window before the switch: [t - pre_window_s, t - 2]
    post_delay_s: float = 15.0  # settling time after the switch
    post_window_s: float = 60.0  # window after: [t + post_delay_s, t + post_delay_s + post_window_s]
    isolation_s: float = 75.0  # no other switch in [t - isolation_s, t + isolation_s]
    max_pre_std: float = 30.0  # W, population standard deviation
    max_post_std: float = 45.0
    min_samples: int = 3  # meter readings per window
    min_measurements: int = 5  # from this many measurements on there is a learned value
    max_stored: int = 200  # oldest measurements drop out
    max_abs_w: float = 20000.0  # plausibility limit for meter readings


def member_is_on(domain: str, state: str | None) -> bool | None:
    """On/off state of one group member, None when its state is not known."""
    if state is None or state in _UNKNOWN_STATES:
        return None
    if domain == "media_player":
        return state not in _MEDIA_PLAYER_OFF
    if state == "on":
        return True
    if state == "off":
        return False
    return None


def group_state(members: Iterable[bool | None]) -> bool | None:
    """True as soon as one member is on, False only when every member is known to be off."""
    values = list(members)
    if any(value is True for value in values):
        return True
    if values and all(value is False for value in values):
        return False
    return None


class SampleBuffer:
    """Recent meter readings of one meter configuration, kept for ``keep_s`` seconds."""

    def __init__(self, keep_s: float = 600.0, max_abs_w: float = 20000.0) -> None:
        self.keep_s = keep_s
        self.max_abs_w = max_abs_w
        self._ts: list[float] = []
        self._watts: list[float] = []

    def __len__(self) -> int:
        return len(self._ts)

    def add(self, ts: float, watts: float) -> bool:
        """Store a reading. Implausible values are dropped and return False.

        Implausible: read errors beyond max_abs_w, NaN, inf, and timestamps more than MAX_FUTURE_S
        ahead of both the newest reading and the wall clock.
        """
        try:
            value = float(watts)
            stamp = float(ts)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or not math.isfinite(stamp) or abs(value) > self.max_abs_w:
            return False
        newest = self._ts[-1] if self._ts else None
        if (newest is None or stamp > newest + MAX_FUTURE_S) and stamp > time.time() + MAX_FUTURE_S:
            return False
        # Readings out of order are sorted in; pruning goes by the newest reading seen.
        index = bisect.bisect_right(self._ts, stamp)
        self._ts.insert(index, stamp)
        self._watts.insert(index, value)
        self._prune(self._ts[-1] - self.keep_s)
        return True

    def window(self, start: float, end: float) -> list[float]:
        """Readings with start <= ts <= end, oldest first."""
        lo = bisect.bisect_left(self._ts, start)
        hi = bisect.bisect_right(self._ts, end)
        return self._watts[lo:hi]

    def _prune(self, cutoff: float) -> None:
        drop = bisect.bisect_left(self._ts, cutoff)
        if drop:
            del self._ts[:drop]
            del self._watts[:drop]


@dataclass
class Result:
    device_id: str
    ts: float  # time of the switch
    direction: str  # "on" | "off"
    delta_w: float | None  # positive = the device draws this much; None when rejected
    reject: str | None  # one of the REJECT_* constants or None


@dataclass
class _Registration:
    buffer: SampleBuffer
    post_delay_s: float


@dataclass
class _Switch:
    seq: int
    device_id: str
    ts: float
    direction: str
    post_delay_s: float  # settling time at the moment of the switch
    due: float


class Tracker:
    """Holds the open switches of all devices and evaluates them once their windows have passed."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._devices: dict[str, _Registration] = {}
        self._pending: list[_Switch] = []
        self._history: list[_Switch] = []
        self._seq = itertools.count()

    def register(self, device_id: str, buffer: SampleBuffer, post_delay_s: float | None = None) -> None:
        """Observe a device. Registering again replaces buffer and settling time."""
        delay = self.settings.post_delay_s if post_delay_s is None else float(post_delay_s)
        self._devices[device_id] = _Registration(buffer=buffer, post_delay_s=delay)

    def unregister(self, device_id: str) -> None:
        """Stop observing a device and drop its open switches.

        Its switches stay in the isolation history until they expire: the meter saw them, so
        they still spoil the measurements of other devices around that time.
        """
        self._devices.pop(device_id, None)
        self._pending = [s for s in self._pending if s.device_id != device_id]

    def transition(self, device_id: str, is_on: bool, ts: float) -> None:
        """Report that a registered device switched on (True) or off (False) at ``ts``."""
        registration = self._devices.get(device_id)
        if registration is None:
            return
        cfg = self.settings
        delay = registration.post_delay_s
        switch = _Switch(
            seq=next(self._seq),
            device_id=device_id,
            ts=float(ts),
            direction=DIRECTION_ON if is_on else DIRECTION_OFF,
            post_delay_s=delay,
            due=float(ts) + max(delay + cfg.post_window_s, cfg.isolation_s) + GRACE_S,
        )
        self._pending.append(switch)
        if len(self._pending) > MAX_PENDING:
            # Stays in the history: the meter saw it, so it still spoils its neighbours.
            oldest = min(self._pending, key=lambda s: (s.ts, s.seq))
            self._pending = [s for s in self._pending if s.seq != oldest.seq]
        self._history.append(switch)
        self._prune_history(float(ts))

    def next_due(self) -> float | None:
        """Earliest time at which collect() returns something, None when nothing is open."""
        if not self._pending:
            return None
        return min(s.due for s in self._pending)

    def collect(self, now: float) -> list[Result]:
        """Evaluate and remove every switch whose windows have passed, oldest first."""
        due = sorted((s for s in self._pending if now >= s.due), key=lambda s: (s.ts, s.seq))
        if not due:
            self._prune_history(now)
            return []
        due_seqs = {s.seq for s in due}
        results = [self._evaluate(s) for s in due]
        self._pending = [s for s in self._pending if s.seq not in due_seqs]
        self._prune_history(now)
        return results

    def _evaluate(self, switch: _Switch) -> Result:
        cfg = self.settings
        registration = self._devices[switch.device_id]

        def rejected(reason: str) -> Result:
            return Result(switch.device_id, switch.ts, switch.direction, None, reason)

        lo = switch.ts - cfg.isolation_s
        hi = switch.ts + cfg.isolation_s
        neighbours = [s for s in self._history if s.seq != switch.seq and lo <= s.ts <= hi]
        if any(s.device_id != switch.device_id for s in neighbours):
            return rejected(REJECT_OVERLAP)
        if neighbours:
            return rejected(REJECT_FLAPPING)

        buffer = registration.buffer
        delay = switch.post_delay_s
        pre = buffer.window(switch.ts - cfg.pre_window_s, switch.ts - PRE_GUARD_S)
        post = buffer.window(switch.ts + delay, switch.ts + delay + cfg.post_window_s)
        needed = max(cfg.min_samples, 1)
        if len(pre) < needed or len(post) < needed:
            return rejected(REJECT_GAP)
        if pstdev(pre) > cfg.max_pre_std or pstdev(post) > cfg.max_post_std:
            return rejected(REJECT_NOISY)

        delta = median(post) - median(pre)
        if switch.direction == DIRECTION_OFF:
            delta = -delta
        return Result(switch.device_id, switch.ts, switch.direction, delta, None)

    def _keep_s(self) -> float:
        cfg = self.settings
        delays = [r.post_delay_s for r in self._devices.values()] or [cfg.post_delay_s]
        return 2 * cfg.isolation_s + max(delays) + cfg.post_window_s

    def _prune_history(self, now: float) -> None:
        cutoff = now - self._keep_s()
        if self._pending:
            # A switch that is still open needs its neighbours, however late collect() runs.
            cutoff = min(cutoff, min(s.ts for s in self._pending) - self.settings.isolation_s)
        self._history = [s for s in self._history if s.ts >= cutoff]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _whole_count(value: object) -> int | None:
    """A stored rejection count as int, None unless it is a whole number >= 0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        return None
    return number if number >= 0 else None


class DeviceStats:
    """Measurements and rejections of one device, with the learned value derived from them."""

    def __init__(self, settings: Settings | None = None, data: dict | None = None) -> None:
        self.settings = settings or Settings()
        self._measurements: list[dict] = []
        self._rejected: dict[str, int] = {}
        self._last_reject: str | None = None
        if data:
            self._load(data)

    def _load(self, data: dict) -> None:
        for item in data.get("measurements") or []:
            try:
                entry = {
                    "ts": float(item["ts"]),
                    "delta_w": float(item["delta_w"]),
                    "direction": item["direction"],
                }
            except (KeyError, TypeError, ValueError):
                continue
            if (
                entry["direction"] in (DIRECTION_ON, DIRECTION_OFF)
                and math.isfinite(entry["ts"])
                and math.isfinite(entry["delta_w"])
            ):
                self._measurements.append(entry)
        self._trim()
        rejected = data.get("rejected") or {}
        if isinstance(rejected, dict):
            for reason, count in rejected.items():
                number = _whole_count(count)
                if reason in REJECT_REASONS and number is not None:
                    self._rejected[reason] = number
        last_reject = data.get("last_reject")
        self._last_reject = str(last_reject) if last_reject is not None else None

    def _trim(self) -> None:
        overflow = len(self._measurements) - self.settings.max_stored
        if overflow > 0:
            del self._measurements[:overflow]

    def add(self, result: Result) -> None:
        """Book a measurement or a rejection."""
        if result.reject is not None:
            self._rejected[result.reject] = self._rejected.get(result.reject, 0) + 1
            self._last_reject = result.reject
            return
        if result.delta_w is None:
            return
        self._measurements.append(
            {"ts": float(result.ts), "delta_w": float(result.delta_w), "direction": result.direction}
        )
        self._trim()

    def reset(self) -> None:
        self._measurements.clear()
        self._rejected.clear()
        self._last_reject = None

    def as_dict(self) -> dict:
        return {
            "measurements": [dict(m) for m in self._measurements],
            "rejected": dict(self._rejected),
            "last_reject": self._last_reject,
        }

    @property
    def count(self) -> int:
        return len(self._measurements)

    @property
    def rejected(self) -> dict[str, int]:
        return dict(self._rejected)

    @property
    def last_reject(self) -> str | None:
        return self._last_reject

    def _deltas(self, direction: str | None = None) -> list[float]:
        return [m["delta_w"] for m in self._measurements if direction is None or m["direction"] == direction]

    @property
    def learned_w(self) -> float | None:
        """Median of all deltas, rounded to one decimal; None below min_measurements."""
        if self.count < self.settings.min_measurements:
            return None
        return _round(median(self._deltas()))

    @property
    def spread_w(self) -> float | None:
        """Median absolute deviation from the median, rounded to one decimal; None below min_measurements."""
        if self.count < self.settings.min_measurements:
            return None
        deltas = self._deltas()
        centre = median(deltas)
        return _round(median([abs(d - centre) for d in deltas]))

    @property
    def on_w(self) -> float | None:
        deltas = self._deltas(DIRECTION_ON)
        return _round(median(deltas)) if deltas else None

    @property
    def off_w(self) -> float | None:
        deltas = self._deltas(DIRECTION_OFF)
        return _round(median(deltas)) if deltas else None

    @property
    def confidence(self) -> str:
        learned = self.learned_w
        spread = self.spread_w
        if learned is None or spread is None:
            return "learning"
        if spread <= max(5.0, 0.15 * abs(learned)):
            return "high"
        if spread <= max(10.0, 0.35 * abs(learned)):
            return "medium"
        return "low"

    @property
    def last(self) -> dict | None:
        return dict(self._measurements[-1]) if self._measurements else None

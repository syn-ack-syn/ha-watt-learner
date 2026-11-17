"""Glue between Home Assistant and the learning core.

There is exactly one Hub per Home Assistant instance (``hass.data[DOMAIN]``). It owns the Tracker
and one SampleBuffer per meter configuration (meter plus sorted compensation sensors), feeds meter
readings into the buffers and reports the on/off switches of every entry's device group. Each config
entry has an EntryLearner with its DeviceStats and its Store.

Time base: Unix seconds throughout, from ``time.time()`` or from the state's own timestamps, never
monotonic time, so meter readings and switches line up.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback, split_entity_id
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import (
    CONF_COMPENSATION,
    CONF_MEMBERS,
    CONF_METER,
    CONF_POST_DELAY,
    DEFAULT_POST_DELAY_S,
    DOMAIN,
    SAVE_DELAY_S,
    STARTUP_GRACE_S,
    STORE_VERSION,
    TIMER_MARGIN_S,
)
from .learner import DeviceStats, SampleBuffer, Settings, Tracker, group_state, member_is_on

_LOGGER = logging.getLogger(__name__)

MeterKey = tuple[str, tuple[str, ...]]


def entry_config(entry: ConfigEntry) -> dict[str, Any]:
    """The entry's settings: options override data."""
    return {**entry.data, **entry.options}


def store_for(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STORE_VERSION, f"{DOMAIN}.{entry_id}")


def _number(value: str | None) -> float | None:
    """A finite float from a state string, None for unavailable, unknown or anything else."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class EntryLearner:
    """One config entry: its device group, its statistics and where they are stored."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, settings: Settings | None = None) -> None:
        config = entry_config(entry)
        self.hass = hass
        self.entry_id = entry.entry_id
        self.meter: str = config[CONF_METER]
        self.compensation: tuple[str, ...] = tuple(sorted(config.get(CONF_COMPENSATION) or []))
        self.members: list[str] = list(config.get(CONF_MEMBERS) or [])
        self.post_delay_s = float(config.get(CONF_POST_DELAY, DEFAULT_POST_DELAY_S))
        self.settings = settings or Settings()
        self.stats = DeviceStats(self.settings)
        self.store = store_for(hass, entry.entry_id)
        self.signal = f"{DOMAIN}_{entry.entry_id}_update"
        self.is_on: bool | None = None  # group state, None while unknown
        self.loaded_at = time.time()
        self._dirty = False

    @property
    def meter_key(self) -> MeterKey:
        return (self.meter, self.compensation)

    async def async_load(self) -> None:
        data = await self.store.async_load()
        self.stats = DeviceStats(self.settings, data if isinstance(data, dict) else None)

    @callback
    def async_notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    @callback
    def async_save_later(self) -> None:
        self._dirty = True
        self.store.async_delay_save(self.stats.as_dict, SAVE_DELAY_S)

    async def async_flush(self) -> None:
        """Write pending changes now (also cancels the delayed save and its listeners)."""
        if self._dirty:
            self._dirty = False
            await self.store.async_save(self.stats.as_dict())

    @callback
    def async_reset(self) -> None:
        self.stats.reset()
        self.async_save_later()
        self.async_notify()


@dataclass
class _Meter:
    buffer: SampleBuffer
    unsubs: list[CALLBACK_TYPE] = field(default_factory=list)
    users: set[str] = field(default_factory=set)


class Hub:
    """Meter buffers, the Tracker and the collect timer for all entries."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.settings = Settings()
        self.tracker = Tracker(self.settings)
        self._meters: dict[MeterKey, _Meter] = {}
        self._learners: dict[str, EntryLearner] = {}
        self._member_unsubs: dict[str, CALLBACK_TYPE] = {}
        self._timer: CALLBACK_TYPE | None = None
        self._unsub_started: CALLBACK_TYPE | None = None
        # Already running: Home Assistant started long ago, only the grace after loading an entry counts.
        self._started = hass.state is CoreState.running
        self._started_at: float | None = None
        if not self._started:
            self._unsub_started = async_at_started(hass, self._on_started)

    @property
    def empty(self) -> bool:
        return not self._learners

    # ---- entries --------------------------------------------------------------------------------

    @callback
    def async_add(self, learner: EntryLearner) -> None:
        learner.loaded_at = time.time()
        self._learners[learner.entry_id] = learner
        buffer = self._acquire_meter(learner)
        self.tracker.register(learner.entry_id, buffer, learner.post_delay_s)
        learner.is_on = self._group(learner)
        if learner.members:
            self._member_unsubs[learner.entry_id] = async_track_state_change_event(
                self.hass, learner.members, partial(self._on_member, learner)
            )

    @callback
    def async_remove(self, entry_id: str) -> None:
        learner = self._learners.pop(entry_id, None)
        unsub = self._member_unsubs.pop(entry_id, None)
        if unsub:
            unsub()
        self.tracker.unregister(entry_id)
        if learner is not None:
            self._release_meter(learner)
        self._schedule()

    @callback
    def async_shutdown(self) -> None:
        """Drop every listener and timer; the hub is not used afterwards."""
        for entry_id in list(self._learners):
            self.async_remove(entry_id)
        for meter in self._meters.values():
            for unsub in meter.unsubs:
                unsub()
        self._meters.clear()
        self._cancel_timer()
        if self._unsub_started:
            self._unsub_started()
            self._unsub_started = None

    # ---- meter ----------------------------------------------------------------------------------

    def _acquire_meter(self, learner: EntryLearner) -> SampleBuffer:
        key = learner.meter_key
        meter = self._meters.get(key)
        if meter is None:
            meter = _Meter(buffer=SampleBuffer(max_abs_w=self.settings.max_abs_w))
            on_reading = partial(self._on_meter, key)
            # A changed value fires state_changed, the same value reported again fires state_reported.
            # Both are readings; without the second a steady base load would leave gaps in the buffer.
            meter.unsubs.append(async_track_state_change_event(self.hass, [learner.meter], on_reading))
            meter.unsubs.append(async_track_state_report_event(self.hass, [learner.meter], on_reading))
            self._meters[key] = meter
        meter.users.add(learner.entry_id)
        return meter.buffer

    def _release_meter(self, learner: EntryLearner) -> None:
        key = learner.meter_key
        meter = self._meters.get(key)
        if meter is None:
            return
        meter.users.discard(learner.entry_id)
        if not meter.users:
            for unsub in meter.unsubs:
                unsub()
            del self._meters[key]

    @callback
    def _on_meter(self, key: MeterKey, event: Event[Mapping[str, Any]]) -> None:
        meter = self._meters.get(key)
        state = event.data.get("new_state")
        if meter is None or state is None:
            return
        value = _number(state.state)
        if value is None:
            return
        total = value
        for entity_id in key[1]:
            comp = self.hass.states.get(entity_id)
            comp_value = _number(comp.state) if comp is not None else None
            total += comp_value if comp_value is not None else 0.0  # an invalid compensation counts as 0
        meter.buffer.add(state.last_reported.timestamp(), total)

    # ---- members --------------------------------------------------------------------------------

    def _group(self, learner: EntryLearner) -> bool | None:
        values = []
        for entity_id in learner.members:
            state = self.hass.states.get(entity_id)
            values.append(member_is_on(split_entity_id(entity_id)[0], state.state if state else None))
        return group_state(values)

    @callback
    def _on_member(self, learner: EntryLearner, event: Event[Mapping[str, Any]]) -> None:
        if self._learners.get(learner.entry_id) is not learner:
            return
        old = learner.is_on
        new = self._group(learner)
        if new == old:
            return
        learner.is_on = new
        # Only a direct on <-> off change is a switch. With an unknown state in between the group
        # starts over from the next known state.
        if old is not None and new is not None:
            state = event.data.get("new_state")
            ts = state.last_updated.timestamp() if state is not None else time.time()
            if self._accepts(learner, ts):
                self.tracker.transition(learner.entry_id, new, ts)
                self._schedule()
        learner.async_notify()

    def _accepts(self, learner: EntryLearner, ts: float) -> bool:
        """False during the grace period after Home Assistant started or the entry was loaded."""
        if not self._started:
            return False
        since = learner.loaded_at if self._started_at is None else max(learner.loaded_at, self._started_at)
        return ts >= since + STARTUP_GRACE_S

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        self._unsub_started = None
        self._started = True
        self._started_at = time.time()

    # ---- collect timer --------------------------------------------------------------------------

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None

    def _schedule(self) -> None:
        """Set the timer to the tracker's earliest due time, or clear it when nothing is open."""
        self._cancel_timer()
        due = self.tracker.next_due()
        if due is None:
            return
        delay = max(due - time.time(), 0.0) + TIMER_MARGIN_S
        self._timer = async_call_later(self.hass, delay, self._on_timer)

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._timer = None
        changed: dict[str, EntryLearner] = {}
        for result in self.tracker.collect(time.time()):
            learner = self._learners.get(result.device_id)
            if learner is None:
                continue
            learner.stats.add(result)
            changed[learner.entry_id] = learner
            if result.reject is None:
                _LOGGER.debug("%s: %s measured %.1f W", learner.entry_id, result.direction, result.delta_w)
            else:
                _LOGGER.debug("%s: %s rejected (%s)", learner.entry_id, result.direction, result.reject)
        for learner in changed.values():
            learner.async_save_later()
            learner.async_notify()
        self._schedule()

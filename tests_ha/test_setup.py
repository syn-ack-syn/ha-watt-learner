"""Setup, unload and learning against a test Home Assistant with frozen time.

The `freezer` fixture moves `time.time()`, Home Assistant's clock and the event loop's clock together,
so the hub's Unix timestamps, the state timestamps and `async_call_later` stay in step. The meter
reports every 10 seconds, like the one in the target house.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME, EVENT_STATE_CHANGED, EVENT_STATE_REPORTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.watt_learner.const import (
    CONF_COMPENSATION,
    CONF_MEMBERS,
    CONF_METER,
    CONF_POST_DELAY,
    DOMAIN,
    STARTUP_GRACE_S,
)
from custom_components.watt_learner.diagnostics import async_get_config_entry_diagnostics

METER = "sensor.house_power"
SOLAR = "sensor.solarbank_power"
TV = "media_player.tv"
RECEIVER = "switch.receiver"
LAMP = "light.lamp"
POWER_ATTRS = {"device_class": "power", "unit_of_measurement": "W"}

AFTER_GRACE_S = STARTUP_GRACE_S + 10
# Long enough for a switch to become due (the windows plus a margin), without relying on the exact due time.
SETTLE_S = 150


class House:
    """The meter (every 10 s) and the group members, on a frozen clock."""

    def __init__(self, hass: HomeAssistant, freezer: FrozenDateTimeFactory, base: float = 200.0, noise: bool = True) -> None:
        self.hass = hass
        self.freezer = freezer
        self.base = base
        self.load = 0.0
        self.noise = noise
        self._step = 0

    async def run(self, seconds: float) -> None:
        for _ in range(int(seconds // 10)):
            self.freezer.tick(timedelta(seconds=10))
            self._step += 1
            value = self.base + self.load + (self._step % 2 if self.noise else 0)
            self.hass.states.async_set(METER, f"{value:g}", POWER_ATTRS)
            async_fire_time_changed(self.hass)
            await self.hass.async_block_till_done()

    async def switch(self, entity_id: str, state: str, load: float) -> None:
        self.hass.states.async_set(entity_id, state)
        self.load = load
        await self.hass.async_block_till_done()

    async def cycle(self, entity_id: str, watts: float) -> None:
        await self.switch(entity_id, "on", watts)
        await self.run(SETTLE_S)
        await self.switch(entity_id, "off", 0.0)
        await self.run(SETTLE_S)


def _seed_states(hass: HomeAssistant) -> None:
    hass.states.async_set(METER, "200", POWER_ATTRS)
    hass.states.async_set(SOLAR, "0", POWER_ATTRS)
    hass.states.async_set(TV, "off")
    hass.states.async_set(RECEIVER, "off")
    hass.states.async_set(LAMP, "off")


def _entry(
    hass: HomeAssistant, title: str = "TV", members: list[str] | None = None, compensation: list[str] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={
            CONF_NAME: title,
            CONF_METER: METER,
            CONF_COMPENSATION: compensation or [],
            CONF_MEMBERS: members or [TV, RECEIVER],
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _entity(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id, key
    return entity_id


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str, platform: str = "sensor") -> Any:
    state = hass.states.get(_entity(hass, entry, platform, key))
    assert state is not None
    return state


def _stored(entry_id: str, count: int, delta: float = 50.0) -> dict:
    return {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry_id}",
        "data": {
            "measurements": [
                {"ts": 1_700_000_000.0 + 600 * i, "delta_w": delta + i, "direction": "on" if i % 2 == 0 else "off"}
                for i in range(count)
            ],
            "rejected": {"gap": 2},
            "last_reject": "gap",
        },
    }


@pytest.fixture(autouse=True)
def seed(hass: HomeAssistant) -> None:
    _seed_states(hass)


# ---- setup and unload ---------------------------------------------------------------------------
async def test_setup_creates_the_entities(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry(hass))

    learned = _state(hass, entry, "learned_power")
    assert learned.state == "unknown"
    assert learned.attributes["unit_of_measurement"] == "W"
    assert learned.attributes["device_class"] == "power"
    assert learned.attributes["measurements"] == 0
    assert learned.attributes["confidence"] == "learning"
    assert learned.attributes["members"] == [TV, RECEIVER]
    assert learned.attributes["rejected"] == {}
    assert learned.attributes["last_measurement"] is None

    power = _state(hass, entry, "power")
    assert power.state == "unknown"  # nothing learned yet
    assert power.attributes["state_class"] == "measurement"
    assert _state(hass, entry, "measurements").state == "0"
    assert _state(hass, entry, "reset", "button") is not None

    registry = er.async_get(hass)
    assert registry.async_get(_entity(hass, entry, "sensor", "measurements")).entity_category == "diagnostic"
    assert registry.async_get(_entity(hass, entry, "button", "reset")).entity_category == "config"
    assert entry.entry_id in [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)]
    assert hass.data[DOMAIN] is not None


async def test_stored_statistics_are_restored(hass: HomeAssistant, hass_storage: dict) -> None:
    entry = _entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = _stored(entry.entry_id, 5)
    await _setup(hass, entry)

    learned = _state(hass, entry, "learned_power")
    assert float(learned.state) == 52.0  # median of 50..54
    assert learned.attributes["measurements"] == 5
    assert learned.attributes["rejected"] == {"gap": 2}
    assert learned.attributes["last_reject"] == "gap"
    assert learned.attributes["last_delta_w"] == 54.0
    assert learned.attributes["last_measurement"].startswith("2023-11-")
    # The group is off, so the device draws nothing now.
    assert float(_state(hass, entry, "power").state) == 0.0


async def test_unload_leaves_no_listeners_or_timers(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    before = hass.bus.async_listeners()
    entry = await _setup(hass, _entry(hass))
    hub = hass.data[DOMAIN]
    house = House(hass, freezer)
    await house.run(AFTER_GRACE_S)
    await house.switch(TV, "on", 100)  # an open switch, so the collect timer runs
    await house.run(20)
    assert hub.tracker.next_due() is not None
    assert hub._timer is not None
    learner = entry.runtime_data
    learner.async_reset()  # a delayed save, so the store has a timer and a final-write listener
    assert learner.store._delay_handle is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert DOMAIN not in hass.data
    assert hub._timer is None
    assert hub._meters == {}
    assert hub._member_unsubs == {}
    assert hub._unsub_started is None
    # The pending save was written at once: no delayed save, no final-write listener left behind.
    assert learner.store._delay_handle is None
    assert learner.store._unsub_final_write_listener is None
    after = hass.bus.async_listeners()
    for event in (EVENT_STATE_CHANGED, EVENT_STATE_REPORTED):
        assert after.get(event, 0) == before.get(event, 0), event


async def test_second_entry_shares_the_hub(hass: HomeAssistant) -> None:
    tv = await _setup(hass, _entry(hass))
    lamp = await _setup(hass, _entry(hass, "Lamp", [LAMP]))
    hub = hass.data[DOMAIN]
    assert len(hub._meters) == 1  # same meter configuration, one buffer

    assert await hass.config_entries.async_unload(tv.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN] is hub
    assert len(hub._meters) == 1

    assert await hass.config_entries.async_unload(lamp.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN not in hass.data
    assert hub._meters == {}


async def test_options_change_reloads_the_entry(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _entry(hass))
    first = entry.runtime_data

    hass.config_entries.async_update_entry(
        entry, options={CONF_METER: METER, CONF_COMPENSATION: [], CONF_MEMBERS: [TV], CONF_POST_DELAY: 30}
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    learner = entry.runtime_data
    assert learner is not first
    assert learner.post_delay_s == 30
    assert learner.members == [TV]
    assert hass.data[DOMAIN].tracker._devices[entry.entry_id].post_delay_s == 30
    assert _state(hass, entry, "learned_power").attributes["members"] == [TV]


async def test_removing_the_entry_deletes_its_statistics(hass: HomeAssistant, hass_storage: dict) -> None:
    entry = _entry(hass)
    key = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[key] = _stored(entry.entry_id, 3)
    await _setup(hass, entry)

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_reset_button_clears_the_statistics(hass: HomeAssistant, hass_storage: dict) -> None:
    entry = _entry(hass)
    key = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[key] = _stored(entry.entry_id, 6)
    await _setup(hass, entry)
    assert _state(hass, entry, "learned_power").state != "unknown"

    await hass.services.async_call(
        "button", "press", {"entity_id": _entity(hass, entry, "button", "reset")}, blocking=True
    )
    await hass.async_block_till_done()

    learned = _state(hass, entry, "learned_power")
    assert learned.state == "unknown"
    assert learned.attributes["measurements"] == 0
    assert learned.attributes["rejected"] == {}
    assert _state(hass, entry, "measurements").state == "0"

    # Unloading writes the pending save at once.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[key]["data"] == {"measurements": [], "rejected": {}, "last_reject": None}


async def test_diagnostics_contain_data_and_statistics(hass: HomeAssistant, hass_storage: dict) -> None:
    entry = _entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = _stored(entry.entry_id, 2)
    await _setup(hass, entry)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["data"][CONF_METER] == METER
    assert diag["options"] == {}
    assert len(diag["stats"]["measurements"]) == 2
    assert diag["stats"]["rejected"] == {"gap": 2}


# ---- learning -----------------------------------------------------------------------------------
async def test_five_switch_cycles_give_a_learned_value(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    entry = await _setup(hass, _entry(hass))
    house = House(hass, freezer)
    await house.run(AFTER_GRACE_S)

    for cycle in range(5):
        await house.cycle(TV, 100.0)
        if cycle == 1:  # four measurements: still learning
            learned = _state(hass, entry, "learned_power")
            assert learned.state == "unknown"
            assert learned.attributes["measurements"] == 4
            assert learned.attributes["confidence"] == "learning"

    learned = _state(hass, entry, "learned_power")
    assert float(learned.state) == pytest.approx(100.0, abs=1.0)
    assert learned.attributes["measurements"] == 10
    assert learned.attributes["on_w"] == pytest.approx(100.0, abs=1.0)
    assert learned.attributes["off_w"] == pytest.approx(100.0, abs=1.0)
    assert learned.attributes["confidence"] == "high"
    assert learned.attributes["rejected"] == {}
    assert learned.attributes["last_measurement"] is not None
    assert _state(hass, entry, "measurements").state == "10"

    # The current power follows the group: 0 while off, the learned value while on.
    assert float(_state(hass, entry, "power").state) == 0.0
    await house.switch(RECEIVER, "on", 100.0)
    assert float(_state(hass, entry, "power").state) == pytest.approx(100.0, abs=1.0)
    await house.switch(RECEIVER, "unavailable", 100.0)
    await house.switch(TV, "off", 100.0)
    assert _state(hass, entry, "power").state == "unknown"  # one member unknown, none on


async def test_unchanged_meter_value_still_counts(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """The meter repeats its value; Home Assistant reports that as state_reported, not state_changed."""
    entry = await _setup(hass, _entry(hass))
    house = House(hass, freezer, noise=False)
    await house.run(AFTER_GRACE_S)

    await house.switch(TV, "on", 100.0)
    await house.run(SETTLE_S)

    learned = _state(hass, entry, "learned_power")
    assert learned.attributes["measurements"] == 1
    assert learned.attributes["last_delta_w"] == 100.0
    assert learned.attributes["rejected"] == {}


async def test_no_switches_during_the_startup_grace(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await _setup(hass, _entry(hass))
    hub = hass.data[DOMAIN]
    house = House(hass, freezer)
    await house.run(30)

    await house.switch(TV, "on", 100.0)
    assert hub.tracker.next_due() is None

    await house.run(AFTER_GRACE_S)
    await house.switch(TV, "off", 0.0)
    assert hub.tracker.next_due() is not None


async def test_unknown_state_in_between_is_no_switch(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    entry = await _setup(hass, _entry(hass, members=[TV]))
    hub = hass.data[DOMAIN]
    house = House(hass, freezer)
    await house.run(AFTER_GRACE_S)

    await house.switch(TV, "unavailable", 0.0)
    await house.switch(TV, "on", 100.0)
    assert hub.tracker.next_due() is None
    assert entry.runtime_data.is_on is True

    await house.run(SETTLE_S)
    await house.switch(TV, "standby", 0.0)  # a media player in standby is off: a real switch
    assert hub.tracker.next_due() is not None


async def test_two_groups_switching_together_are_rejected(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    tv = await _setup(hass, _entry(hass, members=[TV]))
    lamp = await _setup(hass, _entry(hass, "Lamp", [LAMP]))
    house = House(hass, freezer)
    await house.run(AFTER_GRACE_S)

    await house.switch(TV, "on", 100.0)
    await house.run(20)
    await house.switch(LAMP, "on", 140.0)
    await house.run(SETTLE_S)

    for entry in (tv, lamp):
        learned = _state(hass, entry, "learned_power")
        assert learned.attributes["measurements"] == 0
        assert learned.attributes["rejected"] == {"overlap": 1}
        assert learned.attributes["last_reject"] == "overlap"


async def test_compensation_is_added_to_the_meter(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await _setup(hass, _entry(hass, compensation=[SOLAR]))
    buffer = next(iter(hass.data[DOMAIN]._meters.values())).buffer
    house = House(hass, freezer, base=120.0, noise=False)

    hass.states.async_set(SOLAR, "80", POWER_ATTRS)
    await house.run(30)
    hass.states.async_set(SOLAR, "unavailable")  # invalid compensation counts as 0
    await house.run(30)
    hass.states.async_set(METER, "40000", POWER_ATTRS)  # read error, dropped by the buffer
    await hass.async_block_till_done()

    assert buffer.window(0, 1e12) == [200.0, 200.0, 200.0, 120.0, 120.0, 120.0]

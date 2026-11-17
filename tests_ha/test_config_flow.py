"""Config flow and options flow against a test Home Assistant.

Every form and error the flow returns is checked against strings.json, so a missing translation key
fails here. The entry is not really set up in these tests (async_setup_entry is patched).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.watt_learner.const import (
    CONF_COMPENSATION,
    CONF_MEMBERS,
    CONF_METER,
    CONF_POST_DELAY,
    DOMAIN,
)

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "watt_learner"
STRINGS = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))

METER = "sensor.house_power"
SOLAR = "sensor.solarbank_power"
MEMBERS = ["media_player.tv", "switch.receiver"]


def checked(result, section: str = "config"):
    """The result, after making sure each of its keys has a text in strings.json."""
    texts = STRINGS[section]
    if result["type"] is FlowResultType.FORM:
        assert result["step_id"] in texts["step"], result["step_id"]
        fields = texts["step"][result["step_id"]]["data"]
        for key in result["data_schema"].schema:
            assert str(key) in fields, key
        for error in (result.get("errors") or {}).values():
            assert error in texts["error"], error
    elif result["type"] is FlowResultType.ABORT:
        assert result["reason"] in texts["abort"], result["reason"]
    return result


def suggested(result, key: str):
    for field in result["data_schema"].schema:
        if str(field) == key:
            return (field.description or {}).get("suggested_value")
    raise AssertionError(f"{key} not in the form")


@pytest.fixture(autouse=True)
def no_entry_setup() -> Iterator[AsyncMock]:
    with patch("custom_components.watt_learner.async_setup_entry", return_value=True) as setup_entry:
        yield setup_entry


def test_translations_have_the_same_keys() -> None:
    def keys(tree, prefix=""):
        out = set()
        for key, value in tree.items():
            out |= keys(value, f"{prefix}{key}.") if isinstance(value, dict) else {f"{prefix}{key}"}
        return out

    english = json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))
    german = json.loads((PKG / "translations" / "de.json").read_text(encoding="utf-8"))
    assert english == STRINGS
    assert keys(german) == keys(STRINGS)
    names = {key: german["entity"][platform][key]["name"] for platform in german["entity"] for key in german["entity"][platform]}
    assert names == {
        "learned_power": "Gelernte Leistung",
        "power": "Leistung",
        "measurements": "Messungen",
        "reset": "Zurücksetzen",
    }


async def test_user_flow_creates_the_entry(hass: HomeAssistant, no_entry_setup) -> None:
    result = checked(await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER}))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "TV Wohnzimmer", CONF_METER: METER, CONF_COMPENSATION: [SOLAR], CONF_MEMBERS: MEMBERS},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "TV Wohnzimmer"
    assert result["data"] == {
        CONF_NAME: "TV Wohnzimmer",
        CONF_METER: METER,
        CONF_COMPENSATION: [SOLAR],
        CONF_MEMBERS: MEMBERS,
    }
    assert no_entry_setup.call_count == 1


async def test_compensation_is_optional(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Lamp", CONF_METER: METER, CONF_MEMBERS: ["light.lamp"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_COMPENSATION] == []


async def test_no_members_shows_an_error(hass: HomeAssistant, no_entry_setup) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = checked(
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NAME: "Nothing", CONF_METER: METER, CONF_MEMBERS: []}
        )
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MEMBERS: "no_members"}
    assert suggested(result, CONF_NAME) == "Nothing"  # what was entered stays in the form
    assert no_entry_setup.call_count == 0


async def test_meter_and_compensation_come_from_an_existing_entry(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        title="Kitchen",
        data={CONF_NAME: "Kitchen", CONF_METER: "sensor.old_meter", CONF_COMPENSATION: [], CONF_MEMBERS: ["light.a"]},
        options={CONF_METER: METER, CONF_COMPENSATION: [SOLAR], CONF_MEMBERS: ["light.a"], CONF_POST_DELAY: 15},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    assert suggested(result, CONF_METER) == METER  # options override data
    assert suggested(result, CONF_COMPENSATION) == [SOLAR]
    assert suggested(result, CONF_MEMBERS) is None


async def test_first_entry_has_no_prefill(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert suggested(result, CONF_METER) is None
    assert suggested(result, CONF_COMPENSATION) is None


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TV",
        data={CONF_NAME: "TV", CONF_METER: METER, CONF_COMPENSATION: [], CONF_MEMBERS: MEMBERS},
    )
    entry.add_to_hass(hass)
    return entry


async def test_options_change_the_settling_time(hass: HomeAssistant) -> None:
    entry = _entry(hass)

    result = checked(await hass.config_entries.options.async_init(entry.entry_id), "options")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert suggested(result, CONF_METER) == METER
    assert suggested(result, CONF_MEMBERS) == MEMBERS
    assert suggested(result, CONF_POST_DELAY) == 15

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_METER: METER, CONF_COMPENSATION: [SOLAR], CONF_MEMBERS: MEMBERS, CONF_POST_DELAY: 30},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_METER: METER, CONF_COMPENSATION: [SOLAR], CONF_MEMBERS: MEMBERS, CONF_POST_DELAY: 30}
    assert entry.data[CONF_NAME] == "TV"  # data stays as it was


async def test_options_need_a_member(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = checked(
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_METER: METER, CONF_MEMBERS: [], CONF_POST_DELAY: 20}
        ),
        "options",
    )
    assert result["errors"] == {CONF_MEMBERS: "no_members"}
    assert entry.options == {}


@pytest.mark.parametrize("delay", [4, 181])
async def test_settling_time_outside_the_range_is_refused(hass: HomeAssistant, delay: int) -> None:
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(vol.Invalid):  # Home Assistant wraps it in InvalidData, a subclass
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_METER: METER, CONF_MEMBERS: MEMBERS, CONF_POST_DELAY: delay}
        )
    assert entry.options == {}

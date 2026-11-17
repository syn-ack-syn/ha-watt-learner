"""Config flow: one entry per device or device group; options change meter, members and settling time."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_COMPENSATION,
    CONF_MEMBERS,
    CONF_METER,
    CONF_POST_DELAY,
    DEFAULT_POST_DELAY_S,
    DOMAIN,
    MAX_POST_DELAY_S,
    MEMBER_DOMAINS,
    MIN_POST_DELAY_S,
)


def _power_sensor(multiple: bool = False) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power", multiple=multiple))


def _members_selector() -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=list(MEMBER_DOMAINS), multiple=True))


def _meter_fields() -> dict:
    return {
        vol.Required(CONF_METER): _power_sensor(),
        vol.Optional(CONF_COMPENSATION): _power_sensor(multiple=True),
        vol.Required(CONF_MEMBERS): _members_selector(),
    }


def _errors(user_input: dict[str, Any]) -> dict[str, str]:
    return {} if user_input.get(CONF_MEMBERS) else {CONF_MEMBERS: "no_members"}


def _meter_values(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_METER: user_input[CONF_METER],
        CONF_COMPENSATION: list(user_input.get(CONF_COMPENSATION) or []),
        CONF_MEMBERS: list(user_input[CONF_MEMBERS]),
    }


class WattLearnerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WattLearnerOptionsFlow:
        return WattLearnerOptionsFlow()

    def _suggested(self) -> dict[str, Any]:
        """Meter and compensation of an existing entry: most houses have one meter."""
        for entry in self._async_current_entries(include_ignore=False):
            config = {**entry.data, **entry.options}
            if config.get(CONF_METER):
                return {CONF_METER: config[CONF_METER], CONF_COMPENSATION: list(config.get(CONF_COMPENSATION) or [])}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _errors(user_input)
            if not errors:
                name = user_input[CONF_NAME]
                return self.async_create_entry(title=name, data={CONF_NAME: name, **_meter_values(user_input)})
        schema = vol.Schema({vol.Required(CONF_NAME): TextSelector(), **_meter_fields()})
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or self._suggested()),
            errors=errors,
        )


class WattLearnerOptionsFlow(OptionsFlow):
    """Meter, compensation, members and settling time. Saving reloads the entry (listener in __init__.py)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _errors(user_input)
            if not errors:
                return self.async_create_entry(
                    data={
                        **_meter_values(user_input),
                        CONF_POST_DELAY: user_input.get(CONF_POST_DELAY, DEFAULT_POST_DELAY_S),
                    }
                )
        entry = self.config_entry
        current = {**entry.data, **entry.options}
        schema = vol.Schema(
            {
                **_meter_fields(),
                vol.Required(CONF_POST_DELAY, default=DEFAULT_POST_DELAY_S): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_POST_DELAY_S,
                        max=MAX_POST_DELAY_S,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        suggested = user_input or {
            CONF_METER: current.get(CONF_METER),
            CONF_COMPENSATION: list(current.get(CONF_COMPENSATION) or []),
            CONF_MEMBERS: list(current.get(CONF_MEMBERS) or []),
            CONF_POST_DELAY: current.get(CONF_POST_DELAY, DEFAULT_POST_DELAY_S),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

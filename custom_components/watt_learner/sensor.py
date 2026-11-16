"""Sensors of one device group: learned power, current power and the number of measurements."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .hub import EntryLearner


class WattLearnerEntity(Entity):
    """Shared base: one device per entry, updated through the entry's dispatcher signal."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self._learner: EntryLearner = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._learner.signal, self.async_write_ha_state)
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(
        [
            LearnedPowerSensor(entry, "learned_power"),
            PowerSensor(entry, "power"),
            MeasurementsSensor(entry, "measurements"),
        ]
    )


class LearnedPowerSensor(WattLearnerEntity, SensorEntity):
    """The learned value: median of all measurements, unknown until enough are there."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    @property
    def native_value(self) -> float | None:
        return self._learner.stats.learned_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self._learner.stats
        last = stats.last
        return {
            "measurements": stats.count,
            "spread_w": stats.spread_w,
            "on_w": stats.on_w,
            "off_w": stats.off_w,
            "confidence": stats.confidence,
            "last_measurement": dt_util.utc_from_timestamp(last["ts"]).isoformat() if last else None,
            "last_delta_w": round(last["delta_w"], 1) if last else None,
            "rejected": stats.rejected,
            "last_reject": stats.last_reject,
            "members": list(self._learner.members),
        }


class PowerSensor(WattLearnerEntity, SensorEntity):
    """What the group draws now: the learned value while on, 0 while off."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        learned = self._learner.stats.learned_w
        is_on = self._learner.is_on
        if learned is None or is_on is None:
            return None
        return max(learned, 0.0) if is_on else 0.0


class MeasurementsSensor(WattLearnerEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int:
        return self._learner.stats.count

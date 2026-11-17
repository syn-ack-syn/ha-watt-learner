"""Watt Learner: learns in the background how many watts a device or device group draws."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .hub import EntryLearner, Hub, store_for

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]


def _hub(hass: HomeAssistant) -> Hub:
    hub = hass.data.get(DOMAIN)
    if hub is None:
        hub = hass.data[DOMAIN] = Hub(hass)
    return hub


def _release(hass: HomeAssistant, entry_id: str) -> None:
    """Take the entry out of the hub; the last entry takes the hub down."""
    hub: Hub | None = hass.data.get(DOMAIN)
    if hub is None:
        return
    hub.async_remove(entry_id)
    if hub.empty:
        hub.async_shutdown()
        hass.data.pop(DOMAIN, None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    learner = EntryLearner(hass, entry)
    await learner.async_load()
    entry.runtime_data = learner
    _hub(hass).async_add(learner)  # only now: a failed load must not leave an empty hub behind
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        _release(hass, entry.entry_id)
        raise

    # Saved options reload the entry. Only an options change counts, not a new title.
    options_at_setup = dict(entry.options)

    async def _options_updated(hass: HomeAssistant, updated: ConfigEntry) -> None:
        if dict(updated.options) != options_at_setup:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    _release(hass, entry.entry_id)
    learner: EntryLearner = entry.runtime_data
    await learner.async_flush()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the entry's statistics."""
    await store_for(hass, entry.entry_id).async_remove()

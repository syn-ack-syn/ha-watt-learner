"""Diagnostics download: entry data and options plus the learned statistics."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    learner = getattr(entry, "runtime_data", None)  # None while the entry is not loaded
    return {
        "data": dict(entry.data),
        "options": dict(entry.options),
        "stats": learner.stats.as_dict() if learner else None,
    }

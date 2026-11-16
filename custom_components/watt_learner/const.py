"""Constants of the Watt Learner integration."""

from __future__ import annotations

DOMAIN = "watt_learner"

CONF_METER = "meter"
CONF_COMPENSATION = "compensation"
CONF_MEMBERS = "members"
CONF_POST_DELAY = "post_delay_s"

DEFAULT_POST_DELAY_S = 15
MIN_POST_DELAY_S = 5
MAX_POST_DELAY_S = 180

# Domains whose entities can be members of a device group.
MEMBER_DOMAINS = ["light", "switch", "fan", "media_player", "input_boolean"]

# No switches are reported this long after Home Assistant started or the entry was loaded: devices
# come up, restore their state and would otherwise produce bogus measurements.
STARTUP_GRACE_S = 120.0

# The collect timer fires this much after the earliest due time, so the switch is surely due.
TIMER_MARGIN_S = 0.5

STORE_VERSION = 1
SAVE_DELAY_S = 30

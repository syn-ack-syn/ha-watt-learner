"""Setup for the tests against Home Assistant.

Run: `UV_PROJECT_ENVIRONMENT=.venv-ha uv run --no-default-groups --group ha pytest tests_ha`, never
together with tests/ in one run. Unlike tests/, these tests import the integration as
`custom_components.watt_learner` inside a real (test) Home Assistant from
pytest-homeassistant-custom-component. Its pytest plugin is installed with the `ha` group and loads
through its entry point, so it is not listed here again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # custom_components/ lives in the repo root


def pytest_configure(config: pytest.Config) -> None:
    # Home Assistant's fixtures are plain async fixtures; pytest-asyncio runs them only in auto mode.
    if config.getoption("asyncio_mode", None) is None:
        config.option.asyncio_mode = "auto"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/watt_learner."""
    return

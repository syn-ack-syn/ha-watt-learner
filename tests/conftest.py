"""Shared test setup.

The integration package's __init__.py imports Home Assistant. The module under test (learner)
does not, so the package directory is registered here as a bare package named ``watt_learner``
without running its __init__.py. No Home Assistant install is needed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "custom_components" / "watt_learner"

if "watt_learner" not in sys.modules:
    _pkg = types.ModuleType("watt_learner")
    _pkg.__path__ = [str(PKG_DIR)]
    _pkg.__file__ = str(PKG_DIR / "__init__.py")
    sys.modules["watt_learner"] = _pkg

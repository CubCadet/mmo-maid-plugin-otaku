"""Regression-suite scaffolding.

Mirrors tests/conftest.py — loads __main__.py as `plugin_main`. Kept separate
so the regression suite can be run in isolation: `pytest tests/regression -q`.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ["OTAKU_SKIP_RUN"] = "1"

_MAIN_PY = Path(__file__).resolve().parents[2] / "__main__.py"
if "plugin_main" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("plugin_main", _MAIN_PY)
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["plugin_main"] = _module
    _spec.loader.exec_module(_module)

import pytest
from plugin_main import _cache_clear as _plugin_cache_clear


@pytest.fixture(autouse=True)
def _clear_anilist_cache():
    """The in-process AniList cache is module-level; reset it per test."""
    _plugin_cache_clear()
    yield
    _plugin_cache_clear()

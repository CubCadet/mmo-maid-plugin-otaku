"""Test scaffolding — load __main__.py as `plugin_main` so tests can import it.

pytest reserves the `__main__` module name, so we can't `import __main__`. This
conftest loads the plugin's entry file by path under a renamed module, which is
the pattern the SDK templates use.

The skill's non-negotiable rule #1 requires `plugin.run()` to be the
unconditional last line of __main__.py. We set OTAKU_SKIP_RUN=1 *before*
loading the module so the RPC loop doesn't block the test runner on stdin.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ["OTAKU_SKIP_RUN"] = "1"

_MAIN_PY = Path(__file__).resolve().parent.parent / "__main__.py"
_spec = importlib.util.spec_from_file_location("plugin_main", _MAIN_PY)
_module = importlib.util.module_from_spec(_spec)
sys.modules["plugin_main"] = _module
_spec.loader.exec_module(_module)

import pytest


@pytest.fixture(autouse=True)
def _clear_anilist_cache():
    """The in-process AniList cache is module-level; reset it per test."""
    _module._cache_clear()
    yield
    _module._cache_clear()

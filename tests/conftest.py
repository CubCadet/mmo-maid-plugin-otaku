"""Test scaffolding — load __main__.py as `plugin_main` so tests can import it.

pytest reserves the `__main__` module name, so we can't `import __main__`. This
conftest loads the plugin's entry file by path under a renamed module, which is
the pattern the SDK templates use.

The plugin guards `plugin.run()` with `if __name__ == "__main__":`, so importing
the module here doesn't block the test runner on stdin.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MAIN_PY = Path(__file__).resolve().parent.parent / "__main__.py"
_spec = importlib.util.spec_from_file_location("plugin_main", _MAIN_PY)
_module = importlib.util.module_from_spec(_spec)
sys.modules["plugin_main"] = _module
_spec.loader.exec_module(_module)

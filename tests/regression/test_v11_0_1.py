"""v11.0.1 regression contract — production cron is manifest-backed.

Pooled YourBot plugins do not run decorator-only background threads. The
platform dispatches a cron task only when manifest.json declares the same
function name and cron spec as @plugin.cron. Keep the two sources exact.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _manifest_cron_pairs() -> set[tuple[str, str]]:
    manifest = json.loads((_REPO / "manifest.json").read_text(encoding="utf-8"))
    return {
        (entry["name"], entry["spec"])
        for entry in manifest.get("cron", [])
    }


def _decorated_cron_pairs() -> set[tuple[str, str]]:
    tree = ast.parse((_REPO / "__main__.py").read_text(encoding="utf-8"))
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            target = decorator.func
            if not (
                isinstance(target, ast.Attribute)
                and target.attr == "cron"
                and isinstance(target.value, ast.Name)
                and target.value.id == "plugin"
                and len(decorator.args) == 1
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ):
                continue
            pairs.add((node.name, decorator.args[0].value))
    return pairs


def test_manifest_cron_matches_decorators_exactly():
    expected = {("cron_airing_check", "5 * * * *")}
    assert _manifest_cron_pairs() == _decorated_cron_pairs() == expected

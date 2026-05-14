"""Regression contract for otaku v3.1.0 — friend comparison.

IMMUTABLE — what shipped at v3.1.0:
- /compare user:<user> slash command (user required, type 6).
- Comparing against yourself short-circuits before any SQL.
- _compare_users(my_rows, their_rows) returns a dict with the four sections:
    shared_favorites, divergent_ratings, completion_recs, my_total/their_total/shared_total.
- Divergent threshold: rating gap ≥ 4 (i.e. ≥ 2 points on the 1–10 scale, since
  rating is stored as score × 2).
- Each section capped at COMPARE_LIST_LIMIT = 5 entries.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _slash(name: str, options: dict | None = None, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name=name,
        options=[{"name": k, "value": v} for k, v in (options or {}).items()],
        **extra,
    )


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_compare():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "compare" in names


def test_compare_user_option_required_type_user():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "compare")
    user = next(o for o in cmd["options"] if o["name"] == "user")
    assert user["type"] == 6  # USER
    assert user["required"] is True


# ── Algorithm constants frozen ──────────────────────────────────────────────


def test_compare_list_limit_is_five():
    assert p.COMPARE_LIST_LIMIT == 5


# ── _compare_users contract ────────────────────────────────────────────────


def test_compare_users_shared_favorites_require_both_flags():
    my = {
        1: {"status": "completed", "is_favorite": True,  "rating": None},
        2: {"status": "completed", "is_favorite": False, "rating": None},
    }
    them = {
        1: {"status": "completed", "is_favorite": True,  "rating": None},
        2: {"status": "completed", "is_favorite": True,  "rating": None},
    }
    result = p._compare_users(my, them)
    # 2 isn't a shared favorite — only the caller's side is unfavorited.
    assert result["shared_favorites"] == [1]


def test_compare_users_divergent_threshold_is_four_stored_units():
    my = {
        1: {"status": "completed", "is_favorite": False, "rating": 10},  # 5.0
        2: {"status": "completed", "is_favorite": False, "rating": 10},  # 5.0
    }
    them = {
        1: {"status": "completed", "is_favorite": False, "rating": 13},  # 6.5 — gap 3, below threshold
        2: {"status": "completed", "is_favorite": False, "rating": 14},  # 7.0 — gap 4, meets threshold
    }
    result = p._compare_users(my, them)
    assert result["divergent_ratings"] == [(2, 10, 14)]


def test_compare_users_completion_recs_filtered_to_completed_status():
    my = {1: {"status": "completed", "is_favorite": False, "rating": None}}
    them = {
        2: {"status": "completed", "is_favorite": False, "rating": None},
        3: {"status": "watching",   "is_favorite": False, "rating": None},  # not a rec
    }
    result = p._compare_users(my, them)
    assert result["completion_recs"] == [2]


def test_compare_self_short_circuits_no_sql():
    ctx = MockContext()
    p.cmd_compare(ctx, _slash("compare", {"user": "u"}, user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert not ctx.sql.executed  # never even hit SQL

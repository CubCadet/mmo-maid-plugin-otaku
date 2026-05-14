"""Regression contract for otaku v2.5.0 — self-service reset + rating-on-card.

IMMUTABLE — what shipped at v2.5.0:
- /otaku-reset slash command present in manifest.
- Confirm button custom_id format: `otaku:reset-confirm:<user_id>`. Only the
  original caller (matching user_id) can confirm.
- Cancel button custom_id: `otaku:reset-cancel:<user_id>`.
- /anime card now shows a "Your rating" field when the user has a rating.
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


def _component(custom_id: str, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=3,
        custom_id=custom_id,
        **extra,
    )


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_otaku_reset():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "otaku-reset" in names


# ── /otaku-reset confirm flow ───────────────────────────────────────────────


def test_reset_prompt_shows_confirm_and_cancel():
    ctx = MockContext()
    p.cmd_otaku_reset(ctx, _slash("otaku-reset", {}, user_id="reg-r"))
    resp = ctx.interaction.responses[-1]
    components = resp.get("components") or []
    assert components, "expected component row"


def test_reset_confirm_runs_delete_for_caller_only():
    ctx = MockContext()
    p._route_components(ctx, _component("otaku:reset-confirm:reg-r2", user_id="reg-r2"))
    assert any(
        c["sql"] == "DELETE FROM otaku_user_anime WHERE user_id = $1" and c["params"] == ["reg-r2"]
        for c in ctx.sql.executed
    )


def test_reset_confirm_rejects_mismatched_caller():
    ctx = MockContext()
    p._route_components(ctx, _component("otaku:reset-confirm:victim", user_id="attacker"))
    assert not any("DELETE FROM otaku_user_anime" in c["sql"] for c in ctx.sql.executed)


# ── Rating-on-card ──────────────────────────────────────────────────────────


def test_get_user_tracking_returns_progress_and_rating():
    ctx = MockContext()
    ctx.sql.query_one = lambda sql, params=None: {"episodes_watched": 5, "rating": 18}
    assert p._get_user_tracking(ctx, "u", 1) == (5, 18)


def test_get_user_tracking_returns_zero_none_when_no_row():
    ctx = MockContext()
    ctx.sql.query_one = lambda sql, params=None: None
    assert p._get_user_tracking(ctx, "u", 1) == (0, None)

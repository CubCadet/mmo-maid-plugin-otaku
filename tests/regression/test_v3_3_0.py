"""Regression contract for otaku v3.3.0 — server leaderboard.

IMMUTABLE — what shipped at v3.3.0:
- /leaderboard [metric] slash command with `metric` ∈ {completed, score, hours}.
- Default metric is `completed`.
- Score board requires ≥ LEADERBOARD_SCORE_MIN_RATED (3) rated rows to qualify.
- Top 10 entries on each board (LEADERBOARD_TOP_N = 10).
- Hours use the 24-min/episode heuristic (STATS_MINUTES_PER_EPISODE).
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


def test_manifest_includes_leaderboard():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "leaderboard" in names


def test_leaderboard_metric_choices_frozen():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "leaderboard")
    metric = next(o for o in cmd["options"] if o["name"] == "metric")
    values = {c["value"] for c in metric.get("choices", [])}
    assert {"completed", "score", "hours"} == values


def test_leaderboard_constants_frozen():
    assert p.LEADERBOARD_TOP_N == 10
    assert p.LEADERBOARD_SCORE_MIN_RATED == 3


def test_leaderboard_completed_query_filters_status():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        captured["sql"] = sql
        return [{"user_id": "a", "n": 1}]

    ctx.sql.query = _q
    p.cmd_leaderboard(ctx, _slash("leaderboard", {}, user_id="u"))
    # regression-fix (v8.0.0): the WHERE clause now begins with media_type='anime'
    # so the leaderboard isn't polluted by manga rows. The intent — "completed
    # leaderboard filters on status='completed'" — survives.
    assert "status = 'completed'" in captured["sql"]


def test_leaderboard_score_query_uses_having_threshold():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [{"user_id": "a", "avg_rating": 18.0, "rated": 5}]

    ctx.sql.query = _q
    p.cmd_leaderboard(ctx, _slash("leaderboard", {"metric": "score"}, user_id="u"))
    assert "HAVING COUNT(*) >= %s" in captured["sql"]
    # First param is the min-rated threshold.
    assert captured["params"][0] == p.LEADERBOARD_SCORE_MIN_RATED


def test_leaderboard_hours_uses_24min_heuristic():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [{"user_id": "a", "episodes": 60}]
    p.cmd_leaderboard(ctx, _slash("leaderboard", {"metric": "hours"}, user_id="u"))
    # 60 eps × 24 min ÷ 60 = 24.0 hours
    body = ctx.interaction.followups[-1]["embeds"][0]["description"]
    assert "24.0 hours" in body

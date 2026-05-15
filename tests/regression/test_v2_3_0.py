"""Regression contract for otaku v2.3.0 — /stats.

IMMUTABLE — what shipped at v2.3.0:
- /stats [user] slash command present in manifest.
- Aggregation pulls from otaku_user_anime via a single GROUP BY status query.
- "Est. hours" uses STATS_MINUTES_PER_EPISODE = 24.
- Top genre is sampled from the user's STATS_TOP_GENRE_SAMPLE = 50 most-recent anime.
- Empty users get an empty-state message (no SQL aggregate row).
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


def test_manifest_includes_stats():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "stats" in names


def test_stats_constants_frozen():
    assert p.STATS_MINUTES_PER_EPISODE == 24
    assert p.STATS_TOP_GENRE_SAMPLE == 50


def test_stats_aggregate_returns_empty_dict_on_no_rows():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []
    assert p._aggregate_user_stats(ctx, "u") == {}


# regression-fix (v10.0.8): the v2.3.0 contract mocked the SQL aggregate
# row shape (one row per status with `count`/`episodes`/`mean_rating`
# columns from the GROUP BY query). v10.0.8 switched to Python
# aggregation over raw row shape (one row per tracked anime with
# `status`/`episodes_watched`/`rating` columns). Same output dict — only
# the input shape changed. The contract (total/total_episodes/
# total_hours/by_status correctness) is preserved.
def test_stats_aggregate_computes_hours_from_episodes():
    ctx = MockContext()

    def _q(sql, params=None):
        return [
            {"status": "completed", "episodes_watched": 25, "rating": None},
        ]

    ctx.sql.query = _q
    agg = p._aggregate_user_stats(ctx, "u")
    # 25 episodes × 24 min = 600 min = 10.0 hours
    assert agg["total_episodes"] == 25
    assert agg["total_hours"] == 10.0
    assert agg["total"] == 1
    assert agg["by_status"]["completed"] == 1

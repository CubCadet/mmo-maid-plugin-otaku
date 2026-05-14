"""Regression contract for otaku v5.0.0 — manifest-mode dashboard.

IMMUTABLE — what shipped at v5.0.0:
- dashboard_manifest.json with two pages (Overview + Settings).
- Overview page has six widgets: four stat_cards, one chart, one table.
- Settings page has one form for the announce channel.
- Python handlers registered via @plugin.on_dashboard:
    get_total_tracked, get_active_users_30d, get_total_episodes,
    get_total_subscriptions, get_status_distribution, get_top_tracked,
    get_settings, save_settings
- Handlers respect the SDK shape contracts (stat_card / chart / table / form).
- save_settings writes the announce channel to NOTIFY_CHANNEL_KV; empty
  string clears it.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext


def _dashboard_manifest() -> dict:
    return json.loads(
        (Path(__file__).resolve().parents[2] / "dashboard_manifest.json").read_text()
    )


# ── Manifest shape ──────────────────────────────────────────────────────────


def test_dashboard_manifest_has_two_pages():
    pages = _dashboard_manifest().get("pages") or []
    page_ids = {pg.get("id") for pg in pages}
    assert {"overview", "settings"}.issubset(page_ids)


def test_overview_page_widgets_frozen():
    pages = _dashboard_manifest()["pages"]
    overview = next(pg for pg in pages if pg["id"] == "overview")
    widget_methods = {w["rpc_method"] for w in overview["widgets"]}
    assert {
        "get_total_tracked",
        "get_active_users_30d",
        "get_total_episodes",
        "get_total_subscriptions",
        "get_status_distribution",
        "get_top_tracked",
    }.issubset(widget_methods)


def test_settings_page_form_wires_get_and_save():
    pages = _dashboard_manifest()["pages"]
    settings = next(pg for pg in pages if pg["id"] == "settings")
    form = settings["widgets"][0]
    assert form["type"] == "form"
    assert form["rpc_method"] == "get_settings"
    assert form["save_method"] == "save_settings"
    fields = {f["name"]: f for f in form["fields"]}
    assert "announce_channel_id" in fields
    assert fields["announce_channel_id"]["type"] == "channel"


# ── Handler shape contracts ─────────────────────────────────────────────────


def test_stat_card_handlers_return_value_change_keys():
    """stat_card widgets must return {value, change}."""
    ctx = MockContext()
    ctx.sql.scalar = lambda sql, params=None: 7
    for handler in (
        p.dash_total_tracked,
        p.dash_active_users_30d,
        p.dash_total_episodes,
        p.dash_total_subscriptions,
    ):
        result = handler(ctx, {})
        assert set(result.keys()) == {"value", "change"}
        assert isinstance(result["value"], int)


def test_status_distribution_returns_chart_shape():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [{"status": "watching", "n": 3}]
    result = p.dash_status_distribution(ctx, {})
    assert "labels" in result and isinstance(result["labels"], list)
    assert "series" in result and isinstance(result["series"], list)
    assert result["series"][0].keys() >= {"name", "data"}
    assert len(result["labels"]) == len(result["series"][0]["data"])


def test_top_tracked_returns_table_shape():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []
    result = p.dash_top_tracked(ctx, {})
    assert "rows" in result and "total" in result
    assert isinstance(result["rows"], list)


def test_top_tracked_row_columns_match_dashboard_manifest():
    """Each table row's keys must include the columns declared in the manifest."""
    pages = _dashboard_manifest()["pages"]
    overview = next(pg for pg in pages if pg["id"] == "overview")
    table = next(w for w in overview["widgets"] if w["type"] == "table")
    declared = {c["key"] for c in table.get("columns") or []}

    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [
        {"media_id": 1, "trackers": 5, "favorites": 2},
    ]
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Page": {"media": [{
            "id": 1, "title": {"romaji": "X", "english": ""},
            "description": "", "coverImage": {"large": ""},
            "bannerImage": None, "averageScore": 0, "popularity": 0,
            "format": "TV", "episodes": 12, "status": "F",
            "season": "SUMMER", "seasonYear": 2024, "genres": [],
            "siteUrl": "https://anilist.co/anime/1",
        }]}}}),
    )
    result = p.dash_top_tracked(ctx, {})
    assert declared.issubset(set(result["rows"][0].keys()))


def test_get_settings_returns_values_dict():
    ctx = MockContext()
    result = p.dash_get_settings(ctx, {})
    assert "values" in result
    assert "announce_channel_id" in result["values"]


def test_save_settings_returns_ok_true():
    ctx = MockContext()
    result = p.dash_save_settings(ctx, {"values": {"announce_channel_id": "ch"}})
    assert result == {"ok": True}


def test_save_settings_writes_notify_channel_kv():
    ctx = MockContext()
    p.dash_save_settings(ctx, {"values": {"announce_channel_id": "abc"}})
    assert ctx.kv.get(p.NOTIFY_CHANNEL_KV) == "abc"

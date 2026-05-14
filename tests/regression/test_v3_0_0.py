"""Regression contract for otaku v3.0.0 — server watchlists.

IMMUTABLE — what shipped at v3.0.0:
- /server-watchlist slash command with three subcommands: view, add, remove.
- `add` and `remove` are admin-gated via `_caller_is_admin` (introduced in v2.6.0).
- `view` is public; pagination buttons use `otaku:swl:<page>` custom_ids.
- SQL table `otaku_server_watchlist (media_id PK, added_by, added_at, note)`.
- _bootstrap_schema runs CREATE TABLE IF NOT EXISTS for the new table.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


def _swl(subname: str, sub_opts: dict | None = None, **extra) -> dict:
    sub_options = [
        {"name": k, "value": v, "type": 3}
        for k, v in (sub_opts or {}).items()
    ]
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name="server-watchlist",
        options=[{"name": subname, "type": 1, "options": sub_options}],
        **extra,
    )


def _component(custom_id: str, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=3,
        custom_id=custom_id,
        **extra,
    )


SAMPLE = {
    "id": 555,
    "title": {"romaji": "Test Show", "english": ""},
    "description": "",
    "coverImage": {"large": ""},
    "bannerImage": None,
    "averageScore": 75,
    "popularity": 100,
    "format": "TV",
    "episodes": 12,
    "status": "FINISHED",
    "season": "SUMMER",
    "seasonYear": 2024,
    "genres": ["Action"],
    "siteUrl": "https://anilist.co/anime/555",
}


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_server_watchlist():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "server-watchlist" in names


def test_server_watchlist_subcommands_frozen():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "server-watchlist")
    subs = {o["name"]: o for o in cmd.get("options", [])}
    assert {"view", "add", "remove"}.issubset(subs.keys())
    for sub in ("view", "add", "remove"):
        assert subs[sub]["type"] == 1  # SUB_COMMAND
    # `add` and `remove` both require an `anime` string option.
    for sub in ("add", "remove"):
        opts = {o["name"]: o for o in subs[sub].get("options", [])}
        assert opts["anime"]["type"] == 3
        assert opts["anime"]["required"] is True


# ── Schema ──────────────────────────────────────────────────────────────────


def test_bootstrap_schema_creates_otaku_server_watchlist():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    assert any(
        "CREATE TABLE IF NOT EXISTS otaku_server_watchlist" in c["sql"]
        for c in ctx.sql.executed
    )


# ── Admin gating on add/remove ──────────────────────────────────────────────


def test_swl_add_denied_for_non_admin():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"data": {"Media": SAMPLE}}))
    p.cmd_server_watchlist(ctx, _swl("add", {"anime": "x"}, user_id="rando"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert not any("INSERT INTO otaku_server_watchlist" in c["sql"] for c in ctx.sql.executed)


def test_swl_add_admin_inserts_with_added_by_set():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "owner"}
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"data": {"Media": SAMPLE}}))
    ctx.sql.query_one = lambda sql, params=None: None  # not on watchlist yet

    p.cmd_server_watchlist(ctx, _swl("add", {"anime": "x"}, user_id="owner"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_server_watchlist" in c["sql"]]
    assert inserts
    assert inserts[-1]["params"][0] == 555  # media_id
    assert inserts[-1]["params"][1] == "owner"  # added_by


def test_swl_remove_admin_deletes():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "owner"}
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"data": {"Media": SAMPLE}}))
    ctx.sql.query_one = lambda sql, params=None: {"1": 1}  # is on watchlist

    p.cmd_server_watchlist(ctx, _swl("remove", {"anime": "x"}, user_id="owner"))

    deletes = [c for c in ctx.sql.executed if "DELETE FROM otaku_server_watchlist" in c["sql"]]
    assert deletes and deletes[-1]["params"] == [555]


# ── /server-watchlist view + pagination contract ────────────────────────────


def test_swl_view_is_public_not_ephemeral():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [
        {"media_id": 555, "added_by": "boss", "note": "pick"},
    ]
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"data": {"Page": {"media": [SAMPLE]}}}))

    p.cmd_server_watchlist(ctx, _swl("view", {}, user_id="anyone"))

    follow = ctx.interaction.followups[-1]
    # Either explicitly False or absent — the renderer doesn't pass ephemeral=True
    assert follow.get("ephemeral") in (False, None)


def test_swl_pagination_custom_id_format():
    """Pagination button format frozen at v3.0.0: otaku:swl:<page>."""
    cid = "otaku:swl:3"
    parts = cid.split(":")
    assert parts == ["otaku", "swl", "3"]


def test_swl_view_pagination_offset_and_limit():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        captured["params"] = params
        return [{"media_id": 555, "added_by": "boss", "note": None}]

    ctx.sql.query = _q
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"data": {"Page": {"media": [SAMPLE]}}}))

    p._route_components(ctx, _component("otaku:swl:2", user_id="anyone"))

    # Page 2: limit=PER_PAGE+1=6, offset=PER_PAGE=5
    assert captured["params"] == [p.PER_PAGE + 1, p.PER_PAGE]

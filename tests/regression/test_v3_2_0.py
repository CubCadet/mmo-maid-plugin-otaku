"""Regression contract for otaku v3.2.0 — watch parties.

IMMUTABLE — what shipped at v3.2.0:
- /wp slash command with subcommands: create, join, status, progress.
- Two new SQL tables: otaku_watch_parties (party_id SERIAL PK, media_id,
  created_by, created_at, status) and otaku_watch_party_members
  (party_id, user_id, episodes_watched, joined_at; composite PK).
- `[Join party]` button uses `otaku:wp-join:<party_id>` custom_id.
- A progress update where all members hit the same episode fires a public
  follow-up "everyone reached episode N" announcement.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _slash(name: str, options: list | None = None, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name=name,
        options=options or [],
        **extra,
    )


def _wp(subname: str, sub_opts: dict | None = None, **extra) -> dict:
    sub_options = [{"name": k, "value": v} for k, v in (sub_opts or {}).items()]
    return _slash("wp", [{"name": subname, "type": 1, "options": sub_options}], **extra)


def _component(custom_id: str, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=3,
        custom_id=custom_id,
        **extra,
    )


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


SAMPLE_MEDIA = {
    "id": 901,
    "title": {"romaji": "Long Show", "english": ""},
    "description": "",
    "coverImage": {"large": ""},
    "bannerImage": None,
    "averageScore": 0,
    "popularity": 0,
    "format": "TV",
    "episodes": 24,
    "status": "RELEASING",
    "season": "SUMMER",
    "seasonYear": 2024,
    "genres": [],
    "siteUrl": "https://anilist.co/anime/901",
}


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_wp_with_four_subcommands():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "wp")
    sub_names = {o["name"] for o in cmd.get("options", []) if o.get("type") == 1}
    assert {"create", "join", "status", "progress"}.issubset(sub_names)


def test_wp_create_requires_anime_option():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "wp")
    sub = next(o for o in cmd["options"] if o["name"] == "create")
    anime = next(o for o in sub["options"] if o["name"] == "anime")
    assert anime["type"] == 3 and anime["required"] is True


def test_wp_progress_requires_id_and_episode():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "wp")
    sub = next(o for o in cmd["options"] if o["name"] == "progress")
    opt_names = {o["name"]: o for o in sub["options"]}
    assert opt_names["id"]["type"] == 4 and opt_names["id"]["required"] is True
    assert opt_names["episode"]["type"] == 4 and opt_names["episode"]["required"] is True


# ── Schema ──────────────────────────────────────────────────────────────────


def test_bootstrap_creates_watch_party_tables():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("CREATE TABLE IF NOT EXISTS otaku_watch_parties" in s for s in sqls)
    assert any("CREATE TABLE IF NOT EXISTS otaku_watch_party_members" in s for s in sqls)


# ── /wp create ──────────────────────────────────────────────────────────────


def test_wp_create_inserts_party_and_auto_joins_creator():
    ctx = MockContext()
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Media": SAMPLE_MEDIA}}),
    )

    def _qo(sql, params=None):
        ctx.sql.executed.append({"sql": sql, "params": params})
        return {"party_id": 99}

    ctx.sql.query_one = _qo

    p.cmd_wp(ctx, _wp("create", {"anime": "any"}, user_id="creator"))

    assert any("INSERT INTO otaku_watch_parties" in c["sql"] for c in ctx.sql.executed)
    assert any("INSERT INTO otaku_watch_party_members" in c["sql"] for c in ctx.sql.executed)


def test_wp_create_returns_embed_with_join_button_using_party_id():
    ctx = MockContext()
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Media": SAMPLE_MEDIA}}),
    )

    def _qo(sql, params=None):
        ctx.sql.executed.append({"sql": sql, "params": params})
        return {"party_id": 7}

    ctx.sql.query_one = _qo

    p.cmd_wp(ctx, _wp("create", {"anime": "any"}, user_id="creator"))

    follow = ctx.interaction.followups[-1]
    btn = follow["components"][0].children[0]
    assert btn.to_dict()["custom_id"] == "otaku:wp-join:7"


# ── /wp join + button ───────────────────────────────────────────────────────


def test_wp_join_button_inserts_member():
    ctx = MockContext()
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Media": SAMPLE_MEDIA}}),
    )
    calls = {"n": 0}

    def _qo(sql, params=None):
        calls["n"] += 1
        ctx.sql.executed.append({"sql": sql, "params": params})
        if calls["n"] == 1:
            return {"party_id": 12, "media_id": SAMPLE_MEDIA["id"],
                    "created_by": "host", "status": "active"}
        return None

    ctx.sql.query_one = _qo

    p._route_components(ctx, _component("otaku:wp-join:12", user_id="late"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_watch_party_members" in c["sql"]]
    assert inserts and inserts[-1]["params"][:2] == [12, "late"]


# ── /wp progress ────────────────────────────────────────────────────────────


def test_wp_progress_updates_member_row():
    ctx = MockContext()
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Media": SAMPLE_MEDIA}}),
    )
    calls = {"n": 0}

    def _qo(sql, params=None):
        calls["n"] += 1
        ctx.sql.executed.append({"sql": sql, "params": params})
        if calls["n"] == 1:
            return {"party_id": 5, "media_id": SAMPLE_MEDIA["id"], "created_by": "h", "status": "active"}
        return {"episodes_watched": 0}

    ctx.sql.query_one = _qo
    ctx.sql.query = lambda sql, params=None: [{"episodes_watched": 3}]

    p.cmd_wp(ctx, _wp("progress", {"id": 5, "episode": 3}, user_id="solo"))

    updates = [c for c in ctx.sql.executed if "UPDATE otaku_watch_party_members" in c["sql"]]
    assert updates and updates[-1]["params"] == [3, 5, "solo"]


def test_wp_progress_sync_announce_when_all_at_same_episode():
    ctx = MockContext()
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Media": SAMPLE_MEDIA}}),
    )
    calls = {"n": 0}

    def _qo(sql, params=None):
        calls["n"] += 1
        ctx.sql.executed.append({"sql": sql, "params": params})
        if calls["n"] == 1:
            return {"party_id": 5, "media_id": SAMPLE_MEDIA["id"], "created_by": "h", "status": "active"}
        return {"episodes_watched": 2}

    ctx.sql.query_one = _qo
    # After this update, all three members are at episode 3.
    ctx.sql.query = lambda sql, params=None: [
        {"episodes_watched": 3},
        {"episodes_watched": 3},
        {"episodes_watched": 3},
    ]

    p.cmd_wp(ctx, _wp("progress", {"id": 5, "episode": 3}, user_id="late"))

    # Second follow-up is the sync announcement, public.
    last = ctx.interaction.followups[-1]
    assert last.get("ephemeral") in (False, None)
    assert "episode 3" in (last.get("content") or "")

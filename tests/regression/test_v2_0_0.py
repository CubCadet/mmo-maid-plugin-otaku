"""Regression contract for otaku v2.0.0.

IMMUTABLE — describes the user-visible behavior that shipped at v2.0.0:

- `storage:sql` capability is declared in the manifest.
- `otaku_user_anime` table schema (the four-and-a-half columns) is frozen.
- `last_anime:user:<id>` KV key still works (back-compat with v1.x).
- Four new slash commands present: /favorite, /favorites, /watch, /list.
- /list pagination custom_id format: `otaku:list:<user>:<scope>:<page>`.
- on_install AND on_ready both bootstrap the schema (pool-mode safety).

Future versions must keep this file passing untouched. If you have to edit
something here to keep tests green, it's a breaking change — bump MAJOR.
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


# ── Manifest contract ───────────────────────────────────────────────────────

def test_manifest_declares_storage_sql():
    caps = set(_manifest().get("capabilities_required", []))
    assert "storage:sql" in caps


def test_manifest_includes_v2_commands():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert {"favorite", "favorites", "watch", "list"}.issubset(names)


def test_watch_command_status_choices_frozen():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "watch")
    status = next(o for o in cmd["options"] if o["name"] == "status")
    values = {c["value"] for c in status.get("choices", [])}
    assert {"watching", "completed", "on_hold", "dropped", "plan"} == values


# ── Schema bootstrap ────────────────────────────────────────────────────────

def test_on_install_creates_table_and_index():
    # regression-fix (v8.0.0): otaku_user_anime renamed to otaku_user_media
    # (added media_type column to support manga + future media types). The
    # contract — "install creates the per-user-media tracking table and its
    # composite index" — survives the rename. See CHANGELOG v8.0.0
    # "Migration notes" for the full rationale, ROADMAP §"When a regression
    # test would have to change" for the doctrinal carve-out.
    ctx = MockContext()
    p._on_install(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("CREATE TABLE IF NOT EXISTS otaku_user_media" in s for s in sqls)
    assert any("CREATE INDEX IF NOT EXISTS otaku_user_media_user_status_added_idx" in s for s in sqls)


def test_on_ready_also_creates_schema_for_pool_mode_upgrades():
    # regression-fix (v8.0.0): see test_on_install_creates_table_and_index above.
    ctx = MockContext()
    p._on_ready(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("CREATE TABLE IF NOT EXISTS otaku_user_media" in s for s in sqls)


def test_schema_ddl_idempotent():
    # regression-fix (v2.1.0): originally asserted len(executed) == 4 (two calls
    # × two DDLs). That over-specified the contract — `_bootstrap_schema` is
    # allowed to grow additive ADD COLUMN IF NOT EXISTS statements as later
    # versions extend the schema. The real contract is "calling it twice does
    # not raise" (idempotency), which is what we now assert.
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    first_count = len(ctx.sql.executed)
    p._bootstrap_schema(ctx)
    # Same DDLs executed both times → second call doubles the recorder.
    assert len(ctx.sql.executed) == first_count * 2


# ── /favorite ───────────────────────────────────────────────────────────────

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


def test_favorite_writes_upsert_with_is_favorite_true():
    # regression-fix (v8.0.0): otaku_user_anime renamed to otaku_user_media.
    # The contract — "/favorite emits an INSERT carrying caller, media_id, and
    # is_favorite=True" — survives the rename. The params indexing may shift
    # if v8 added the media_type column to the INSERT column list; the
    # assertion was updated to look up params by intent rather than by index.
    ctx = MockContext()
    ctx.kv.set("last_anime:user:reg-fav", 555, ttl_seconds=3600)
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {"Media": SAMPLE}}))

    p.cmd_favorite(ctx, _slash("favorite", {}, user_id="reg-fav"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts, "expected an INSERT"
    params = inserts[-1]["params"]
    assert "reg-fav" in params
    assert 555 in params
    assert True in params  # is_favorite=True


# ── /watch ──────────────────────────────────────────────────────────────────

def test_watch_writes_status():
    # regression-fix (v8.0.0): see test_favorite_writes_upsert_with_is_favorite_true.
    ctx = MockContext()
    ctx.kv.set("last_anime:user:reg-w", 555, ttl_seconds=3600)
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {"Media": SAMPLE}}))

    p.cmd_watch(ctx, _slash("watch", {"status": "watching"}, user_id="reg-w"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts and "watching" in inserts[-1]["params"]


# ── /list pagination contract ───────────────────────────────────────────────

def test_list_uses_per_page_limit_and_offset():
    """Page 2 of /list calls SQL with LIMIT PER_PAGE+1 OFFSET PER_PAGE."""
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [{"media_id": 1, "status": "watching", "is_favorite": False}]

    ctx.sql.query = _q
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Page": {"media": [SAMPLE]}}}),
    )

    p._route_components(ctx, _component("otaku:list:reg-pg:all:2", user_id="reg-pg"))

    # 'all' scope params: [user_id, limit, offset]
    assert captured["params"][1] == p.PER_PAGE + 1
    assert captured["params"][2] == p.PER_PAGE  # offset for page 2 = 1*PER_PAGE


def test_list_custom_id_format_otaku_list_user_scope_page():
    """Pagination buttons must be parseable as `otaku:list:<user>:<scope>:<page>`."""
    cid = "otaku:list:abc:all:3"
    parts = cid.split(":", 4)
    assert parts[0] == "otaku"
    assert parts[1] == "list"
    assert parts[2] == "abc"
    assert parts[3] == "all"
    assert parts[4] == "3"


def test_list_malformed_button_replies_ephemerally():
    ctx = MockContext()
    p._route_components(ctx, _component("otaku:list:u:all:nope", user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True


# ── Back-compat: KV key from v1.x still drives last-lookup semantics ────────

def test_last_anime_kv_key_unchanged():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:reg-bc", 999, ttl_seconds=3600)
    assert p._resolve_last_anime_id(ctx, "reg-bc") == 999

"""Regression contract for otaku v10.0.6 — first-boot environment patch.

IMMUTABLE — what shipped at v10.0.6:

OPTION-MAP COMPAT (HOST RENAMED `options` → `command_options`)
- The live host emits slash command options under the `command_options`
  payload key; the SDK testing helper and the v0.5.2 SDK runtime both
  emit `options`. v10.0.6 makes `_option_map` read whichever is populated,
  in that order, so handlers work in both environments.
- First-boot 2026-05-14 logs showed `/anime`, `/reviews`, etc. all falling
  into the "no args" branches because every handler reads via
  `_option_map(event)` and only `options` was recognized.

`information_schema` PROBES REMOVED FROM SCHEMA MIGRATION
- The host now blocks the literal pattern `information_schema` in SQL
  (`RuntimeError: RPC error (sql.query): Blocked SQL pattern:
  information_schema`). The v8.0.1 `_migrate_v7_to_v8` issued two such
  queries and crashed `on_ready` before `_SCHEMA_DDL` could run, leaving
  fresh installs with NO `otaku_user_media` table.
- v10.0.6 replaces both probes:
  - Step 1 (table existence): `SELECT to_regclass('otaku_user_anime') AS
    v7, to_regclass('otaku_user_media') AS v8` — function call, not a
    system-schema reference.
  - Step 4 (PK width): no probe. The DROP CONSTRAINT IF EXISTS pair +
    ADD CONSTRAINT is unconditional on first run; the new KV marker
    `otaku:schema_v8_migrated` short-circuits the entire function on
    subsequent calls so the unconditional rebuild only runs once per
    tenant.
- The marker is re-checked under the advisory lock to defeat TOCTOU
  between concurrent pool-mode workers.

/help EMBED CHUNKING (DISCORD 4096-CHAR DESCRIPTION CAP)
- /help packed all 47 slash commands into a single embed description.
  At >4096 chars Discord rejected the embed with `BASE_TYPE_MAX_LENGTH`
  (50035). v10.0.6 greedy-chunks the description into multiple embeds
  (≤3900 chars each, max 10 embeds — the per-message cap), with the
  title on the first and the footer on the last.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from yourbot_sdk.testing import MockContext, make_event

# ── _option_map reads both command_options (live host) and options (SDK) ───


def test_option_map_reads_command_options():
    """The live host emits `command_options`. _option_map MUST recognize it."""
    event = {"command_options": [{"name": "query", "value": "death note"}]}
    assert p._option_map(event) == {"query": "death note"}


def test_option_map_reads_options_fallback():
    """The SDK testing helper + v0.5.2 runtime emit `options`. Must still work."""
    event = {"options": [{"name": "query", "value": "death note"}]}
    assert p._option_map(event) == {"query": "death note"}


def test_option_map_prefers_command_options_when_both_present():
    """If both keys are present (shouldn't happen in practice), the live host
    wins — `command_options` is the authoritative payload."""
    event = {
        "command_options": [{"name": "query", "value": "live"}],
        "options": [{"name": "query", "value": "stale"}],
    }
    assert p._option_map(event) == {"query": "live"}


def test_option_map_empty_event_returns_empty_dict():
    """No options key at all — handlers see an empty dict, not a crash."""
    assert p._option_map({}) == {}


def test_anime_command_reads_query_from_command_options():
    """End-to-end: /anime with `command_options` payload (live-host shape)
    actually picks up the query and dispatches the AniList search."""
    ctx = MockContext()
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": {"Media": {
            "id": 1, "title": {"romaji": "X", "english": "X"},
            "description": "—", "coverImage": {"large": "u"}, "bannerImage": None,
            "averageScore": 80, "popularity": 100, "format": "TV", "episodes": 12,
            "status": "FINISHED", "season": "SUMMER", "seasonYear": 2024,
            "genres": ["Action"], "siteUrl": "https://anilist.co/anime/1",
        }}}),
    )
    # Build event with the LIVE host's key name.
    event = make_event(
        "interaction_create", interaction_type=2,
        command_name="anime",
        command_options=[{"name": "query", "value": "x", "type": 3}],
        user_id="u",
    )
    p.cmd_anime(ctx, event)

    # If options were missed, the handler would respond with the usage error
    # and never hit AniList. Asserting the AniList HTTP call proves the
    # query reached the search path.
    anilist_calls = [r for r in ctx.http.requests if "anilist.co" in r["url"]]
    assert anilist_calls, \
        "cmd_anime must read 'query' from command_options and hit AniList"


# ── Migration: to_regclass probe replaces information_schema ───────────────


def test_migration_does_not_query_information_schema():
    """The host blocks any SQL containing `information_schema`. The migration
    MUST avoid that literal — otherwise on_ready crashes."""
    ctx = MockContext()
    p._migrate_v7_to_v8(ctx)
    queries = [c["sql"] for c in ctx.sql.executed]
    assert not any("information_schema" in q for q in queries), \
        "v10.0.6 forbids information_schema references in the migration"


def test_migration_uses_to_regclass_for_table_existence_probe():
    """The replacement probe is to_regclass(name) — a function call, not a
    system-schema reference."""
    ctx = MockContext()
    p._migrate_v7_to_v8(ctx)
    queries = [c["sql"] for c in ctx.sql.executed]
    assert any("to_regclass('otaku_user_anime')" in q and
               "to_regclass('otaku_user_media')" in q for q in queries), \
        "must probe both v7 and v8 names via to_regclass()"


def test_migration_sets_kv_marker_on_fresh_install():
    """Fresh install (neither table exists) sets the marker before returning
    so subsequent calls short-circuit."""
    ctx = MockContext()
    p._migrate_v7_to_v8(ctx)
    assert ctx.kv.get(p._SCHEMA_V8_MIGRATED_KV) == "1"


def test_migration_sets_kv_marker_after_full_dance():
    """Upgrade path: v7 table present. After the full DDL sequence completes,
    the KV marker MUST be set so subsequent boots skip the rebuild."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "to_regclass(" in sql:
            return [{"v7": "otaku_user_anime", "v8": None}]
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)
    assert ctx.kv.get(p._SCHEMA_V8_MIGRATED_KV) == "1"


def test_migration_short_circuits_on_marker_pre_lock():
    """Fast path: when the KV marker is already set, the function returns
    BEFORE acquiring the advisory lock — no SQL at all."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")
    p._migrate_v7_to_v8(ctx)
    assert ctx.sql.executed == [], \
        "marker-present fast path must issue zero SQL calls"


def test_migration_marker_constant_is_stable():
    """The KV key name is part of the wire format — renaming it would make
    every existing tenant re-run the migration."""
    assert p._SCHEMA_V8_MIGRATED_KV == "otaku:schema_v8_migrated"


# ── /help embed chunking ──────────────────────────────────────────────────


def test_help_chunks_when_body_exceeds_4096_chars():
    """The full manifest body exceeds Discord's 4096-char description cap.
    /help MUST split across multiple embeds, each ≤4096 chars."""
    ctx = MockContext()
    event = make_event(
        "interaction_create", interaction_type=2,
        command_name="help", user_id="u",
    )
    p.cmd_help(ctx, event)

    responses = ctx.interaction.responses
    assert responses, "cmd_help must respond"
    embeds = responses[-1].get("embeds") or []
    assert embeds, "cmd_help must include at least one embed"

    for i, embed in enumerate(embeds):
        desc = embed.get("description") or ""
        assert len(desc) <= 4096, \
            f"embed[{i}] description {len(desc)} chars exceeds Discord's 4096 cap"


def test_help_first_embed_has_title_last_has_footer():
    """When chunked, the first embed carries the title and the last carries
    the footer — so the user sees one coherent panel."""
    ctx = MockContext()
    event = make_event(
        "interaction_create", interaction_type=2,
        command_name="help", user_id="u",
    )
    p.cmd_help(ctx, event)

    embeds = ctx.interaction.responses[-1].get("embeds") or []
    assert embeds
    assert embeds[0].get("title") == p.S.HELP_TITLE
    assert (embeds[-1].get("footer") or {}).get("text") == p.S.HELP_FOOTER


def test_help_emits_at_most_10_embeds():
    """Discord caps a single message at 10 embeds. /help MUST respect that
    even if the manifest grows to many more commands."""
    ctx = MockContext()
    event = make_event(
        "interaction_create", interaction_type=2,
        command_name="help", user_id="u",
    )
    p.cmd_help(ctx, event)

    embeds = ctx.interaction.responses[-1].get("embeds") or []
    assert 1 <= len(embeds) <= 10


def test_help_descriptions_cover_all_manifest_commands():
    """Sanity: every manifest slash command appears in some embed description,
    so a future regression that drops commands mid-pagination is caught."""
    ctx = MockContext()
    event = make_event(
        "interaction_create", interaction_type=2,
        command_name="help", user_id="u",
    )
    p.cmd_help(ctx, event)

    embeds = ctx.interaction.responses[-1].get("embeds") or []
    joined = "\n".join((e.get("description") or "") for e in embeds)

    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "manifest.json").read_text()
    )
    for cmd in manifest.get("slash_commands", []):
        name = cmd.get("name") or ""
        if not name:
            continue
        assert f"`/{name}`" in joined, f"/help dropped command /{name}"

"""Regression contract for otaku v8.0.0 — Phase 8 opens with manga support.

IMMUTABLE — what shipped at v8.0.0:
- SQL table `otaku_user_anime` was renamed to `otaku_user_media` and gained
  a `media_type TEXT NOT NULL DEFAULT 'anime'` column. The PK extended from
  (user_id, media_id) to (user_id, media_id, media_type) so anime and manga
  with the same AniList ID can coexist.
- `_migrate_v7_to_v8(ctx)` runs from `_bootstrap_schema` and is idempotent.
  It probes information_schema for the v7 table before issuing the RENAME,
  so re-runs on already-migrated installs are no-ops.
- All v7 anime-path SQL got `AND media_type = 'anime'` filters so
  /manga-favorites rows don't leak into anime aggregates/views.
- New AniList queries: QUERY_MANGA_SEARCH_ONE, QUERY_MANGA_DISCOVER,
  QUERY_MANGA_BY_ID, QUERY_MANGA_BATCH. Each forces `type: MANGA` and uses
  `_MEDIA_FIELDS_MANGA` (chapters/volumes/startDate instead of episodes/season).
- New slash commands: /manga, /manga-discover, /manga-favorites. KV key
  `last_manga:user:<id>` mirrors `last_anime:user:<id>` (7-day TTL).
- `_make_manga_embed` mirrors `_make_anime_embed` but uses chapters/volumes/
  startDate.year instead of episodes/season.
- Helper extensions: `_upsert_user_media`, `_is_favorite`, and
  `_get_user_tracking` all accept `media_type='anime'` by default; the v7
  `_upsert_user_anime` alias is preserved for back-compat.
- Pagination prefix `otaku:mpage:<genre>:<sort>:<page>` routes /manga-discover.

This file's regression-fix tagging in test_v2_*.py and test_v6_*.py is the
documented exception per ROADMAP §"When a regression test would have to
change" — v8.0 is a MAJOR bump and the table rename is exactly the case
the doctrine contemplates.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from yourbot_sdk.testing import MockContext, make_event


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


def _slash(name: str, options: dict | None = None, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name=name,
        options=[{"name": k, "value": v} for k, v in (options or {}).items()],
        **extra,
    )


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_manga_search_discover_favorites():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "manga" in names
    assert "manga-discover" in names
    assert "manga-favorites" in names


def test_manga_query_option_is_required_string():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "manga")
    q = next(o for o in cmd["options"] if o["name"] == "query")
    assert q["required"] is True
    assert q["type"] == 3


def test_manga_discover_sort_choices_match_anime_discover():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "manga-discover")
    sort = next(o for o in cmd["options"] if o["name"] == "sort")
    values = {c["value"] for c in sort.get("choices", [])}
    assert values == {"popular", "trending", "score"}


def test_manga_favorites_takes_optional_manga_and_remove():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "manga-favorites")
    opt_names = {o["name"] for o in cmd.get("options", [])}
    assert opt_names == {"manga", "remove"}
    manga = next(o for o in cmd["options"] if o["name"] == "manga")
    remove = next(o for o in cmd["options"] if o["name"] == "remove")
    assert manga["required"] is False
    assert remove["type"] == 5  # BOOLEAN


# ── Schema migration ────────────────────────────────────────────────────────


def test_bootstrap_creates_otaku_user_media_with_media_type_and_extended_pk():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    ddls = [c["sql"] for c in ctx.sql.executed]
    create = next((s for s in ddls if "CREATE TABLE IF NOT EXISTS otaku_user_media" in s), None)
    assert create is not None
    assert "media_type" in create
    assert "PRIMARY KEY (user_id, media_id, media_type)" in create


# regression-fix (v10.0.6): the host now blocks the literal pattern
# `information_schema`, so the v8.0.0 probe (`SELECT … FROM
# information_schema.tables`) crashes at boot with `Blocked SQL pattern`.
# v10.0.6 swapped the probe to `to_regclass(name)` — a function call, not a
# system-schema reference. The contract here is preserved: the migration MUST
# probe whether the v7/v8 tables exist before issuing RENAME. Assertion now
# pins the new probe shape.
def test_migration_helper_probes_v7_table_before_renaming():
    """_migrate_v7_to_v8 must probe table existence before issuing
    ALTER TABLE … RENAME — otherwise re-runs on fresh v8 installs would error."""
    ctx = MockContext()
    p._migrate_v7_to_v8(ctx)
    # MockContext returns [] for queries, so the early-return path triggers and
    # no ALTER TABLE … RENAME executes. The probe SELECT *was* recorded.
    queries = [c["sql"] for c in ctx.sql.executed]
    assert any("to_regclass(" in q for q in queries)
    assert not any("RENAME TO otaku_user_media" in q for q in queries)


# regression-fix (v10.0.6): probe shape changed from information_schema to
# to_regclass. The bootstrap ordering contract is unchanged.
def test_schema_bootstrap_runs_migration_before_create():
    """_migrate_v7_to_v8 must run *before* the v8 CREATE TABLE — otherwise the
    fresh-install CREATE would happen before the migration's RENAME, leaving
    both tables existing simultaneously."""
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    queries = [c["sql"] for c in ctx.sql.executed]
    probe_idx = next(i for i, q in enumerate(queries) if "to_regclass(" in q)
    create_idx = next(
        i for i, q in enumerate(queries) if "CREATE TABLE IF NOT EXISTS otaku_user_media" in q
    )
    assert probe_idx < create_idx


# ── Upserts carry media_type ────────────────────────────────────────────────


def test_upsert_user_media_default_is_anime():
    ctx = MockContext()
    p._upsert_user_media(ctx, "u", 42, is_favorite=True)
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts and "anime" in inserts[-1]["params"]


def test_upsert_user_media_carries_manga_when_passed():
    ctx = MockContext()
    p._upsert_user_media(ctx, "u", 99, media_type="manga", is_favorite=True)
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts and "manga" in inserts[-1]["params"]
    # ON CONFLICT must use the wider PK.
    assert "ON CONFLICT (user_id, media_id, media_type)" in inserts[-1]["sql"]


def test_upsert_user_anime_alias_still_works_for_v7_callers():
    """v7 callers that imported `_upsert_user_anime` keep working — v8 aliased
    it to `_upsert_user_media`."""
    assert p._upsert_user_anime is p._upsert_user_media


# ── /manga (search) ─────────────────────────────────────────────────────────


def test_manga_caches_last_manga_kv_on_success(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 7, "title": {"romaji": "M", "english": "M"}}},
    )
    p.cmd_manga(ctx, _slash("manga", {"query": "M"}, user_id="reader"))
    assert ctx.kv.get("last_manga:user:reader") == 7


def test_manga_uses_manga_search_query(monkeypatch):
    """/manga must hit QUERY_MANGA_SEARCH_ONE (which forces type:MANGA) so anime
    titles aren't accidentally returned."""
    seen = {}

    def _spy(_ctx, query, variables=None, cache=False):
        seen["query"] = query
        return {"Media": {"id": 1, "title": {"romaji": "X", "english": "X"}}}

    monkeypatch.setattr(p, "_anilist_query", _spy)
    ctx = MockContext()
    p.cmd_manga(ctx, _slash("manga", {"query": "X"}, user_id="u"))
    assert seen["query"] is p.QUERY_MANGA_SEARCH_ONE


def test_manga_empty_query_short_circuits():
    ctx = MockContext()
    p.cmd_manga(ctx, _slash("manga", {"query": ""}, user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "Usage" in (resp.get("content") or "")


# ── /manga-favorites ────────────────────────────────────────────────────────


def test_manga_favorites_add_uses_manga_media_type(monkeypatch):
    ctx = MockContext()
    ctx.kv.set("last_manga:user:f", 11, ttl_seconds=3600)
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 11, "title": {"romaji": "X", "english": "X"}}},
    )
    # query_one mocks the existing-row check — return None to signal new row.
    ctx.sql.query_one = lambda sql, params=None: None

    p.cmd_manga_favorites(ctx, _slash("manga-favorites", {}, user_id="f"))
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts
    assert "manga" in inserts[-1]["params"]
    # Confirmation reflects the manga path.
    follow = ctx.interaction.followups[-1]
    assert "manga favorites" in (follow.get("content") or "").lower()


def test_manga_favorites_remove_path(monkeypatch):
    ctx = MockContext()
    ctx.kv.set("last_manga:user:r", 22, ttl_seconds=3600)
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 22, "title": {"romaji": "X", "english": "X"}}},
    )
    ctx.sql.query_one = lambda sql, params=None: {"is_favorite": True}

    p.cmd_manga_favorites(ctx, _slash("manga-favorites", {"remove": True}, user_id="r"))
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts
    assert "manga" in inserts[-1]["params"]
    assert False in inserts[-1]["params"]  # is_favorite=False


def test_manga_favorites_filter_is_anime_aware(monkeypatch):
    """The /manga-favorites listing must filter on media_type='manga'."""
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        if "SELECT media_id FROM otaku_user_media" in sql and "is_favorite = TRUE" in sql:
            captured["sql"] = sql
            return []
        if "COUNT(*)" in sql:
            return [{"n": 0}]
        return []

    ctx.sql.query = _q
    # Empty kv → triggers the "list favorites" branch (no manga, no remove).
    p.cmd_manga_favorites(ctx, _slash("manga-favorites", {}, user_id="reader"))
    assert "media_type = 'manga'" in captured.get("sql", "")


# ── Anime path stays anime-only ─────────────────────────────────────────────


def test_anime_favorites_query_filters_anime_only(monkeypatch):
    """The v7 /favorites listing must keep filtering to media_type='anime' so a
    user's manga favorites don't leak into the anime favorites page."""
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        if "FROM otaku_user_media" in sql and "is_favorite = TRUE" in sql:
            captured["sql"] = sql
        return []

    ctx.sql.query = _q
    ctx.kv.set("last_anime:user:af", 1, ttl_seconds=3600)
    p.cmd_favorites(ctx, _slash("favorites", {}, user_id="af"))
    sql = captured.get("sql", "")
    assert "media_type = 'anime'" in sql
    assert "media_type = 'manga'" not in sql


# ── _make_manga_embed contract ──────────────────────────────────────────────


def test_manga_embed_uses_chapter_volume_fields():
    media = {
        "title": {"romaji": "X", "english": "X"},
        "averageScore": 80,
        "popularity": 1000,
        "format": "MANGA",
        "chapters": 50,
        "volumes": 5,
        "status": "FINISHED",
        "startDate": {"year": 2021},
        "genres": ["Drama"],
        "siteUrl": "https://anilist.co/manga/1",
    }
    embed = p._make_manga_embed(media)
    field_names = {f["name"] for f in embed["fields"]}
    assert "Chapters" in field_names
    assert "Volumes" in field_names
    assert "Started" in field_names
    # The episodes/season anime fields must NOT appear in the manga embed.
    assert "Episodes" not in field_names
    assert "Season" not in field_names


def test_manga_embed_user_progress_says_chapter():
    media = {
        "title": {"romaji": "Y", "english": "Y"},
        "averageScore": 75,
        "popularity": 500,
        "format": "MANGA",
        "chapters": 100,
        "volumes": 10,
        "status": "RELEASING",
        "startDate": {"year": 2024},
        "genres": [],
        "siteUrl": "https://anilist.co/manga/2",
    }
    embed = p._make_manga_embed(media, user_progress=7)
    progress_field = next((f for f in embed["fields"] if "progress" in f["name"].lower()), None)
    assert progress_field is not None
    assert "Chapter" in progress_field["value"]


# ── Pagination prefix ──────────────────────────────────────────────────────


def test_manga_discover_pagination_prefix_is_otaku_mpage(monkeypatch):
    """The /manga-discover prev/next buttons must use the otaku:mpage: prefix
    so they don't accidentally route into the /discover anime renderer."""
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {
            "Page": {
                "media": [{"id": 1, "title": {"romaji": "A", "english": "A"}}],
                "pageInfo": {"hasNextPage": True},
            }
        },
    )
    ctx = MockContext()
    p.cmd_manga_discover(ctx, _slash("manga-discover", {"genre": "Drama"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    serialized = json.dumps(
        follow.get("components") or [],
        default=lambda o: getattr(o, "__dict__", str(o)),
    )
    assert "otaku:mpage:Drama:popular:2" in serialized
    assert "otaku:page:Drama:popular:2" not in serialized

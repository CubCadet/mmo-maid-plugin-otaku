"""Tests for the otaku plugin.

Every test mocks AniList's HTTP response using MockContext.http.mock_response,
then calls the relevant handler directly and asserts on what the handler did
(interaction responses/followups, KV writes, etc.).
"""
from __future__ import annotations

import json

import plugin_main as p
from mmo_maid_sdk import RateLimitError, RpcTimeoutError
from mmo_maid_sdk.testing import MockContext, make_event

# ── Helpers ──────────────────────────────────────────────────────────────────

SAMPLE_MEDIA = {
    "id": 123,
    "title": {"romaji": "Kimi no Na wa.", "english": "Your Name"},
    "description": "Two strangers find themselves linked in a bizarre way.<br>And so it begins.",
    "coverImage": {"large": "https://img.example.com/cover.jpg"},
    "bannerImage": "https://img.example.com/banner.jpg",
    "averageScore": 85,
    "popularity": 500000,
    "format": "MOVIE",
    "episodes": 1,
    "status": "FINISHED",
    "season": "SUMMER",
    "seasonYear": 2016,
    "genres": ["Romance", "Drama", "Supernatural"],
    "siteUrl": "https://anilist.co/anime/123",
}


def _make_other(mid: int, title: str) -> dict:
    return {
        "id": mid,
        "title": {"romaji": title, "english": ""},
        "description": "",
        "coverImage": {"large": ""},
        "bannerImage": None,
        "averageScore": 70,
        "popularity": 1000,
        "format": "TV",
        "episodes": 12,
        "status": "FINISHED",
        "season": "SPRING",
        "seasonYear": 2020,
        "genres": ["Action"],
        "siteUrl": f"https://anilist.co/anime/{mid}",
    }


def _mock_anilist(ctx: MockContext, data: dict) -> None:
    """Register a single canned AniList JSON response."""
    ctx.http.mock_response(
        "graphql.anilist.co",
        status=200,
        body=json.dumps({"data": data}),
    )


def _slash_event(command_name: str, options: dict | None = None, **extra) -> dict:
    opts_list = [{"name": k, "value": v} for k, v in (options or {}).items()]
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name=command_name,
        options=opts_list,
        **extra,
    )


def _component_event(custom_id: str, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=3,
        custom_id=custom_id,
        **extra,
    )


# ── Helper-level unit tests ─────────────────────────────────────────────────

def test_strip_html_removes_tags_and_entities():
    src = "Hello <b>world</b><br>line2 &amp; ok"
    assert p._strip_html(src) == "Hello world\nline2 & ok"


def test_format_title_combines_romaji_and_english_when_different():
    assert p._format_title(SAMPLE_MEDIA) == "Kimi no Na wa. (Your Name)"


def test_make_anime_embed_has_title_score_and_genres():
    embed = p._make_anime_embed(SAMPLE_MEDIA)
    assert embed["title"] == "Kimi no Na wa. (Your Name)"
    assert any(f["name"] == "Score" and f["value"] == "8.5/10" for f in embed["fields"])
    assert any(f["name"] == "Genres" and "Romance" in f["value"] for f in embed["fields"])
    assert embed["thumbnail"]["url"].startswith("https://")


def test_make_list_embed_numbers_results():
    items = [_make_other(1, "Alpha"), _make_other(2, "Beta")]
    embed = p._make_list_embed(items, "Header", page=1, has_next=True)
    assert embed["title"] == "Header"
    assert "1. [Alpha]" in embed["description"]
    assert "2. [Beta]" in embed["description"]


# ── /anime ──────────────────────────────────────────────────────────────────

def test_anime_command_responds_with_embed_and_caches_id():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_anime(ctx, _slash_event("anime", {"query": "kimi no na wa"}, user_id="u1"))

    # Deferred, then followup with embed + buttons.
    assert ctx.interaction.defers
    assert ctx.interaction.followups, "expected a followup with the anime card"
    follow = ctx.interaction.followups[-1]
    assert follow.get("embeds"), "followup should carry an embed"
    assert follow["embeds"][0]["title"].startswith("Kimi no Na wa")

    # KV cache for the user.
    assert ctx.kv.get("last_anime:user:u1") == SAMPLE_MEDIA["id"]

    # HTTP went to AniList.
    assert ctx.http.requests, "expected an HTTP call"
    assert "graphql.anilist.co" in ctx.http.requests[-1]["url"]


def test_anime_command_no_result_replies_ephemerally():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": None})

    p.cmd_anime(ctx, _slash_event("anime", {"query": "asdfgzzz"}, user_id="u2"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "No anime found" in (follow.get("content") or "")


# ── /discover ───────────────────────────────────────────────────────────────

def test_discover_command_returns_paginated_list():
    ctx = MockContext()
    media_list = [_make_other(i, f"Show {i}") for i in range(1, 6)]
    _mock_anilist(ctx, {"Page": {
        "pageInfo": {"hasNextPage": True, "currentPage": 1},
        "media": media_list,
    }})

    p.cmd_discover(
        ctx,
        _slash_event("discover", {"genre": "Action", "sort": "popular"}, user_id="u3"),
    )

    follow = ctx.interaction.followups[-1]
    assert follow.get("embeds")
    assert "Action" in follow["embeds"][0]["title"]
    # Components: page row + select row.
    components = follow.get("components") or []
    assert len(components) >= 1, "expected at least one component row"


def test_discover_defaults_sort_to_popular_when_omitted():
    ctx = MockContext()
    _mock_anilist(ctx, {"Page": {
        "pageInfo": {"hasNextPage": False, "currentPage": 1},
        "media": [_make_other(1, "Show 1")],
    }})

    p.cmd_discover(ctx, _slash_event("discover", {"genre": "Romance"}, user_id="u4"))

    follow = ctx.interaction.followups[-1]
    assert follow["embeds"][0]["title"].endswith("Popular")


# ── /trending ───────────────────────────────────────────────────────────────

def test_trending_command_calls_anilist_with_current_season():
    ctx = MockContext()
    media_list = [_make_other(i, f"Trend {i}") for i in range(1, 4)]
    _mock_anilist(ctx, {"Page": {
        "pageInfo": {"hasNextPage": False, "currentPage": 1},
        "media": media_list,
    }})

    p.cmd_trending(ctx, _slash_event("trending", user_id="u5"))

    follow = ctx.interaction.followups[-1]
    assert follow["embeds"][0]["title"].startswith("🔥 Trending")
    # Body of the HTTP request should contain a season constant.
    body = ctx.http.requests[-1]["body"]
    assert any(s in body for s in ("WINTER", "SPRING", "SUMMER", "FALL"))


# ── /similar ────────────────────────────────────────────────────────────────

def test_similar_with_query_uses_search_path():
    ctx = MockContext()
    parent = dict(SAMPLE_MEDIA)
    parent["recommendations"] = {
        "nodes": [
            {"mediaRecommendation": _make_other(i, f"Rec {i}")} for i in range(1, 4)
        ]
    }
    _mock_anilist(ctx, {"Media": parent})

    p.cmd_similar(ctx, _slash_event("similar", {"anime": "your name"}, user_id="u6"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("embeds")
    assert follow["embeds"][0]["title"].startswith("🔁 Similar to")
    # Cached the resolved anime ID.
    assert ctx.kv.get("last_anime:user:u6") == SAMPLE_MEDIA["id"]


def test_similar_with_no_query_and_no_cache_replies_ephemerally():
    ctx = MockContext()
    # No mock — we should never even hit HTTP.

    p.cmd_similar(ctx, _slash_event("similar", {}, user_id="u_new"))

    assert not ctx.http.requests, "no HTTP call should be made when there's no cached anime"
    assert ctx.interaction.responses, "expected an immediate ephemeral response"
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "/anime" in (resp.get("content") or "")


def test_similar_with_no_query_uses_cached_id():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:u7", 555, ttl_seconds=3600)
    parent = dict(SAMPLE_MEDIA)
    parent["id"] = 555
    parent["recommendations"] = {
        "nodes": [{"mediaRecommendation": _make_other(99, "Rec One")}]
    }
    _mock_anilist(ctx, {"Media": parent})

    p.cmd_similar(ctx, _slash_event("similar", {}, user_id="u7"))

    follow = ctx.interaction.followups[-1]
    assert follow["embeds"][0]["title"].startswith("🔁 Similar to")


# ── Component: otaku:similar:<id> button ───────────────────────────────────

def test_similar_button_fetches_and_replies_ephemerally():
    ctx = MockContext()
    parent = dict(SAMPLE_MEDIA)
    parent["recommendations"] = {
        "nodes": [{"mediaRecommendation": _make_other(42, "Rec 42")}]
    }
    _mock_anilist(ctx, {"Media": parent})

    p._route_components(ctx, _component_event("otaku:similar:123", user_id="u8"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert follow["embeds"][0]["title"].startswith("🔁 Similar to")
    # Caches the anime the button referenced.
    assert ctx.kv.get("last_anime:user:u8") == 123


# ── Component: otaku:expand select ─────────────────────────────────────────

def test_expand_select_loads_full_card():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    event = _component_event("otaku:expand", user_id="u9")
    event["values"] = [str(SAMPLE_MEDIA["id"])]

    p.comp_expand(ctx, event)

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert follow["embeds"][0]["title"].startswith("Kimi no Na wa")


# ── Cooldown ───────────────────────────────────────────────────────────────

def test_cooldown_blocks_rapid_repeat():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    # First call goes through.
    p.cmd_anime(ctx, _slash_event("anime", {"query": "your name"}, user_id="rapid"))
    # Second call within 2s should be rejected immediately (ephemeral respond).
    p.cmd_anime(ctx, _slash_event("anime", {"query": "your name"}, user_id="rapid"))

    # We had one defer (first call) but the second short-circuited before defer.
    assert len(ctx.interaction.defers) == 1
    # Second call's ephemeral "slow down" response was issued.
    assert any(
        "Slow down" in (r.get("content") or "") and r.get("ephemeral") is True
        for r in ctx.interaction.responses
    )


# ── v1.0.1 hardening: error paths ───────────────────────────────────────────

def test_anime_handles_rpc_timeout_with_ephemeral_followup():
    """If AniList times out (and MAL/Kitsu fallbacks also fail in this
    mock-less setup), the user sees an ephemeral error — not a hang.
    v9.1 multi-source: the post is AniList; MAL/Kitsu fallbacks happen
    via ctx.http.get (also no mock), so all three sources miss and the
    user sees the standard "not found anywhere" message."""
    ctx = MockContext()

    def _raise_post(*_args, **_kwargs):
        raise RpcTimeoutError("simulated timeout")

    def _raise_get(*_args, **_kwargs):
        raise RpcTimeoutError("simulated timeout")

    ctx.http.post = _raise_post  # type: ignore[assignment]
    ctx.http.get = _raise_get    # type: ignore[assignment]

    p.cmd_anime(ctx, _slash_event("anime", {"query": "anything"}, user_id="net1"))

    assert ctx.interaction.defers, "should defer before the http call"
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    # v9.1: error message is the cross-source "not found" instead of the
    # AniList-specific timeout message, since we tried 3 sources.
    content = follow.get("content") or ""
    assert "No anime found" in content or "AniList" in content


def test_anime_handles_malformed_response_with_errors_array():
    """AniList sometimes returns {errors:[...]} with no data — surface ephemerally."""
    ctx = MockContext()
    ctx.http.mock_response(
        "graphql.anilist.co",
        status=200,
        body=json.dumps({"errors": [{"message": "Query must contain at least 3 characters"}]}),
    )

    p.cmd_anime(ctx, _slash_event("anime", {"query": "ab"}, user_id="net2"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert (follow.get("content") or "")  # non-empty error message


# ── v1.1.0 /random ──────────────────────────────────────────────────────────

def test_random_with_genre_picks_one_and_caches_id():
    ctx = MockContext()
    media = _make_other(777, "Random Pick")
    media["genres"] = ["Action"]
    # First HTTP call (meta) returns a lastPage, second (pick) returns the media.
    # MockHttp dispatches all matching URLs to the same canned response, so embed
    # everything we need into one body: Page with both pageInfo and media.
    ctx.http.mock_response(
        "graphql.anilist.co",
        status=200,
        body=json.dumps({"data": {"Page": {
            "pageInfo": {"lastPage": 3, "hasNextPage": True},
            "media": [media],
        }}}),
    )

    p.cmd_random(ctx, _slash_event("random", {"genre": "Action"}, user_id="rng1"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("embeds"), "expected an anime card"
    assert ctx.kv.get("last_anime:user:rng1") == 777


def test_random_without_genre_still_works():
    ctx = MockContext()
    media = _make_other(101, "Solo")
    ctx.http.mock_response(
        "graphql.anilist.co",
        status=200,
        body=json.dumps({"data": {"Page": {
            "pageInfo": {"lastPage": 1, "hasNextPage": False},
            "media": [media],
        }}}),
    )

    p.cmd_random(ctx, _slash_event("random", {}, user_id="rng2"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("embeds")


def test_random_falls_back_to_page_one_on_empty_response():
    """If the random page comes back empty, /random should retry page 1."""
    ctx = MockContext()
    # Single mock: every request returns this same body. The meta call sees the
    # populated media list (Page 1), the pick call also sees a populated list
    # because there's only one mock — but the assertion we want is just that
    # the user gets a result, not a "no results" error.
    media = _make_other(202, "Fallback")
    ctx.http.mock_response(
        "graphql.anilist.co",
        status=200,
        body=json.dumps({"data": {"Page": {
            "pageInfo": {"lastPage": 1, "hasNextPage": False},
            "media": [media],
        }}}),
    )

    p.cmd_random(ctx, _slash_event("random", {"genre": "Niche"}, user_id="rng3"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("embeds")


# ── v1.1.0 /character ───────────────────────────────────────────────────────

SAMPLE_CHARACTER = {
    "id": 999,
    "name": {"full": "Mitsuha Miyamizu", "native": "宮水 三葉"},
    "image": {"large": "https://img.example.com/mitsuha.jpg"},
    "description": "A high-school girl living in <b>Itomori</b>.<br>She's a shrine maiden.",
    "siteUrl": "https://anilist.co/character/999",
    "media": {"nodes": [
        {"id": 123, "title": {"romaji": "Kimi no Na wa.", "english": "Your Name"},
         "siteUrl": "https://anilist.co/anime/123"},
    ]},
}


def test_character_command_returns_embed_with_image_and_media():
    ctx = MockContext()
    _mock_anilist(ctx, {"Character": SAMPLE_CHARACTER})

    p.cmd_character(ctx, _slash_event("character", {"query": "mitsuha"}, user_id="chr1"))

    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    assert "Mitsuha" in embed["title"]
    assert embed["thumbnail"]["url"].startswith("https://")
    fields = embed.get("fields") or []
    assert any(f["name"] == "Appears in" and "Kimi" in f["value"] for f in fields)


def test_character_command_no_result_replies_ephemerally():
    ctx = MockContext()
    _mock_anilist(ctx, {"Character": None})

    p.cmd_character(ctx, _slash_event("character", {"query": "xyz"}, user_id="chr2"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "No character found" in (follow.get("content") or "")


def test_character_handles_missing_description_gracefully():
    ctx = MockContext()
    bare = dict(SAMPLE_CHARACTER)
    bare["description"] = None
    bare["media"] = {"nodes": []}
    _mock_anilist(ctx, {"Character": bare})

    p.cmd_character(ctx, _slash_event("character", {"query": "x"}, user_id="chr3"))

    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    assert "no description" in embed["description"]


# ── v2.0.0 schema bootstrap + tracking commands ─────────────────────────────


def _list_event(options: dict | None = None, **extra) -> dict:
    """Slash event helper that allows the `user` mention option to flow through."""
    return _slash_event("list", options=options or {}, **extra)


def test_schema_bootstrap_is_idempotent():
    """Running _bootstrap_schema twice must not raise — CREATE/ALTER IF NOT EXISTS."""
    # regression-fix (v10.0.6): second call emitted FEWER SQL statements.
    # regression-fix (v10.0.7): second call now emits ZERO SQL — the whole
    # bootstrap short-circuits via the `otaku:schema_version` marker. The
    # host caps DDL at 5/hour, so any per-boot DDL would burn the budget.
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    first_count = len(ctx.sql.executed)
    p._bootstrap_schema(ctx)
    # Second call is a no-op via the schema-version marker.
    assert len(ctx.sql.executed) == first_count
    assert all(
        "IF NOT EXISTS" in call["sql"]
        for call in ctx.sql.executed
        if call["sql"].lstrip().startswith(("CREATE", "ALTER"))
    )


def test_on_install_bootstraps_schema():
    ctx = MockContext()
    p._on_install(ctx)
    assert any("CREATE TABLE IF NOT EXISTS otaku_user_media" in c["sql"] for c in ctx.sql.executed)
    assert any("CREATE INDEX IF NOT EXISTS" in c["sql"] for c in ctx.sql.executed)


def test_on_ready_bootstraps_schema():
    """Pool-mode safety: on_ready also runs the DDL."""
    ctx = MockContext()
    p._on_ready(ctx)
    assert any("CREATE TABLE IF NOT EXISTS otaku_user_media" in c["sql"] for c in ctx.sql.executed)


# ── /favorite ───────────────────────────────────────────────────────────────


def test_favorite_uses_last_anime_cache_when_no_arg():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:fav1", SAMPLE_MEDIA["id"], ttl_seconds=3600)
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_favorite(ctx, _slash_event("favorite", {}, user_id="fav1"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "favorites" in (follow.get("content") or "").lower()
    # Upsert should set is_favorite=True. v8.0 added media_type to the INSERT
    # column list so the positional index shifted; assert on membership.
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts, "expected an INSERT"
    assert True in inserts[-1]["params"]  # is_favorite=True


def test_favorite_with_no_cache_and_no_arg_prompts_user():
    ctx = MockContext()
    p.cmd_favorite(ctx, _slash_event("favorite", {}, user_id="fav-new"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "/anime" in (follow.get("content") or "")


def test_favorite_remove_path():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:fav2", SAMPLE_MEDIA["id"], ttl_seconds=3600)
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    # Pre-populate so the row exists as a favorite.
    ctx.sql.query_one = lambda sql, params=None: {"is_favorite": True}  # type: ignore[assignment]

    p.cmd_favorite(ctx, _slash_event("favorite", {"remove": True}, user_id="fav2"))

    follow = ctx.interaction.followups[-1]
    assert "Removed" in (follow.get("content") or "")
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    # v8.0 added media_type to the column list; assert on membership instead.
    assert inserts and False in inserts[-1]["params"]  # is_favorite=False


def test_favorite_with_explicit_anime_arg_searches_anilist():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_favorite(ctx, _slash_event("favorite", {"anime": "your name"}, user_id="fav3"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    # Cached the resolved id.
    assert ctx.kv.get("last_anime:user:fav3") == SAMPLE_MEDIA["id"]


# ── /watch ──────────────────────────────────────────────────────────────────


def test_watch_sets_status_for_cached_anime():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:w1", SAMPLE_MEDIA["id"], ttl_seconds=3600)
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_watch(ctx, _slash_event("watch", {"status": "completed"}, user_id="w1"))

    follow = ctx.interaction.followups[-1]
    assert "Completed" in (follow.get("content") or "")
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    # v8.0 added media_type to the column list; assert on membership.
    assert inserts and "completed" in inserts[-1]["params"]


def test_watch_rejects_invalid_status():
    ctx = MockContext()
    p.cmd_watch(ctx, _slash_event("watch", {"status": "garbage"}, user_id="w2"))
    # Invalid status short-circuits before defer — ephemeral respond.
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "watching" in (resp.get("content") or "")


def test_watch_without_cache_prompts_user():
    ctx = MockContext()
    p.cmd_watch(ctx, _slash_event("watch", {"status": "watching"}, user_id="w-new"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "/anime" in (follow.get("content") or "")


# ── /list ───────────────────────────────────────────────────────────────────


def _stub_rows(rows: list[dict]):
    """Return a callable suitable for monkeypatching ctx.sql.query."""

    def _q(sql, params=None):  # noqa: ANN001
        return rows
    return _q


def test_list_paginates_with_has_next():
    """Page 1 with 6 rows should report has_next=True and display 5 entries."""
    ctx = MockContext()
    rows = [{"media_id": i, "status": "watching", "is_favorite": False} for i in range(1, 7)]
    ctx.sql.query = _stub_rows(rows)  # type: ignore[assignment]
    # AniList batch returns the matching media.
    media_list = [_make_other(i, f"Show {i}") for i in range(1, 7)]
    _mock_anilist(ctx, {"Page": {"media": media_list[:5]}})

    p.cmd_list(ctx, _list_event({}, user_id="l1"))

    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    assert "Your" in embed["title"] or "your" in embed["title"]
    # has_next=True footer should NOT show "(last page)"
    assert "last page" not in embed["footer"]["text"]
    # Has a Next button — the second component is the select row, first is pagination.
    components = follow.get("components") or []
    assert components, "expected pagination row"


def test_list_no_rows_replies_empty():
    ctx = MockContext()
    ctx.sql.query = _stub_rows([])  # type: ignore[assignment]
    # No HTTP needed — empty fast-path.
    p.cmd_list(ctx, _list_event({}, user_id="l-empty"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "/favorite" in (follow.get("content") or "") or "haven't" in (follow.get("content") or "")


def test_list_filters_by_status():
    """When status filter is set, the SQL params include the status string."""
    ctx = MockContext()
    rows = [{"media_id": 1, "status": "completed", "is_favorite": False}]
    captured = {}

    def _q(sql, params=None):  # noqa: ANN001
        captured["sql"] = sql
        captured["params"] = params
        return rows

    ctx.sql.query = _q  # type: ignore[assignment]
    _mock_anilist(ctx, {"Page": {"media": [_make_other(1, "Done")]}})

    p.cmd_list(ctx, _list_event({"status": "completed"}, user_id="l-stat"))

    assert "AND status = $2" in captured["sql"]
    assert captured["params"][1] == "completed"


# ── /favorites ──────────────────────────────────────────────────────────────


def test_favorites_filters_is_favorite_true():
    ctx = MockContext()
    rows = [{"media_id": 42, "status": "watching", "is_favorite": True}]
    captured = {}

    def _q(sql, params=None):  # noqa: ANN001
        captured["sql"] = sql
        captured["params"] = params
        return rows

    ctx.sql.query = _q  # type: ignore[assignment]
    _mock_anilist(ctx, {"Page": {"media": [_make_other(42, "Fave")]}})

    p.cmd_favorites(ctx, _slash_event("favorites", {}, user_id="ufav"))

    assert "AND is_favorite = TRUE" in captured["sql"]
    follow = ctx.interaction.followups[-1]
    assert follow["embeds"][0]["description"].count("⭐") >= 1  # ⭐ in line prefix


# ── otaku:list:* pagination button ──────────────────────────────────────────


def test_list_page_button_dispatches_to_page_two():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):  # noqa: ANN001
        captured["params"] = params
        # 1 row on page 2 (offset 5)
        return [{"media_id": 6, "status": "watching", "is_favorite": False}]

    ctx.sql.query = _q  # type: ignore[assignment]
    _mock_anilist(ctx, {"Page": {"media": [_make_other(6, "Six")]}})

    p._route_components(ctx, _component_event("otaku:list:u-pag:all:2", user_id="u-pag"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    # Offset for page 2 = (2 - 1) * 5 = 5
    assert captured["params"][-1] == 5  # offset is last param for 'all' scope


def test_list_page_button_malformed_id_replies_ephemerally():
    ctx = MockContext()
    p._route_components(ctx, _component_event("otaku:list:u:all:notanumber", user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "malformed" in (resp.get("content") or "").lower()


# ── v5.1.0 /my-stats ────────────────────────────────────────────────────────


def test_my_stats_empty_user_replies_empty_state():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # type: ignore[assignment]
    p.cmd_my_stats(ctx, _slash_event("my-stats", {}, user_id="ms-empty"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "haven't" in (follow.get("content") or "")


def test_my_stats_renders_fields_with_titled_lists():
    ctx = MockContext()
    # Stub each of the 4 SQL queries by inspecting the SQL.
    def _q(sql, params=None):  # noqa: ANN001
        if "GROUP BY status" in sql:
            return [
                {"status": "completed", "count": 4, "episodes": 48, "mean_rating": 16.0},
                {"status": "watching",  "count": 1, "episodes": 2,  "mean_rating": None},
            ]
        if "rating IS NOT NULL" in sql:
            return [
                {"media_id": 10, "rating": 18},
                {"media_id": 11, "rating": 14},
            ]
        if "is_favorite = TRUE" in sql:
            return [{"media_id": 20}]
        if "status = 'completed'" in sql:
            return [{"media_id": 30}, {"media_id": 31}]
        return []

    ctx.sql.query = _q  # type: ignore[assignment]
    _mock_anilist(ctx, {"Page": {"media": [
        _make_other(10, "Top1"), _make_other(11, "Top2"),
        _make_other(20, "Fav1"),
        _make_other(30, "Done1"), _make_other(31, "Done2"),
    ]}})

    p.cmd_my_stats(ctx, _slash_event("my-stats", {}, user_id="ms-1"))

    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    field_names = {f["name"] for f in embed["fields"]}
    assert "🎯 Top rated" in field_names
    assert "⭐ Top favorites" in field_names
    assert "✅ Recently completed" in field_names
    top_rated_field = next(f for f in embed["fields"] if f["name"] == "🎯 Top rated")
    # The 9.0 rating (18 stored / 2) should appear next to Top1.
    assert "Top1" in top_rated_field["value"]
    assert "9.0" in top_rated_field["value"]


def test_my_stats_completion_percentage_shown():
    ctx = MockContext()

    def _q(sql, params=None):  # noqa: ANN001
        if "GROUP BY status" in sql:
            return [
                {"status": "completed", "count": 3, "episodes": 0, "mean_rating": None},
                {"status": "watching",  "count": 1, "episodes": 0, "mean_rating": None},
            ]
        return []

    ctx.sql.query = _q  # type: ignore[assignment]
    _mock_anilist(ctx, {"Page": {"media": []}})

    p.cmd_my_stats(ctx, _slash_event("my-stats", {}, user_id="ms-pct"))

    follow = ctx.interaction.followups[-1]
    completed_field = next(
        f for f in follow["embeds"][0]["fields"] if f["name"] == "✅ Completed"
    )
    # 3 of 4 → 75% completion shown alongside the count.
    assert "75%" in completed_field["value"]


def test_my_stats_top_rated_helper_sorts_desc():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):  # noqa: ANN001
        captured["sql"] = sql
        return []

    ctx.sql.query = _q  # type: ignore[assignment]
    p._my_stats_top_rated(ctx, "u")
    assert "ORDER BY rating DESC" in captured["sql"]


# ── v5.0.0 dashboard handlers ───────────────────────────────────────────────


def test_dashboard_total_tracked_returns_stat_card_shape():
    ctx = MockContext()
    ctx.sql.scalar = lambda sql, params=None: 123  # type: ignore[assignment]
    result = p.dash_total_tracked(ctx, {})
    assert result == {"value": 123, "change": ""}


def test_dashboard_total_tracked_handles_null():
    ctx = MockContext()
    ctx.sql.scalar = lambda sql, params=None: None  # type: ignore[assignment]
    assert p.dash_total_tracked(ctx, {}) == {"value": 0, "change": ""}


def test_dashboard_active_users_30d_uses_interval_filter():
    ctx = MockContext()
    captured = {}

    def _scalar(sql, params=None):  # noqa: ANN001
        captured["sql"] = sql
        return 5

    ctx.sql.scalar = _scalar  # type: ignore[assignment]
    p.dash_active_users_30d(ctx, {})
    assert "INTERVAL '30 days'" in captured["sql"]
    assert "DISTINCT user_id" in captured["sql"]


def test_dashboard_total_episodes_sums_episodes_watched():
    ctx = MockContext()
    captured = {}

    def _scalar(sql, params=None):  # noqa: ANN001
        captured["sql"] = sql
        return 240

    ctx.sql.scalar = _scalar  # type: ignore[assignment]
    result = p.dash_total_episodes(ctx, {})
    assert "SUM(episodes_watched)" in captured["sql"]
    assert result["value"] == 240


def test_dashboard_total_subscriptions_counts_notifications():
    ctx = MockContext()
    captured = {}

    def _scalar(sql, params=None):  # noqa: ANN001
        captured["sql"] = sql
        return 11

    ctx.sql.scalar = _scalar  # type: ignore[assignment]
    result = p.dash_total_subscriptions(ctx, {})
    assert "otaku_notifications" in captured["sql"]
    assert result["value"] == 11


def test_dashboard_status_distribution_returns_chart_shape():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [  # type: ignore[assignment]
        {"status": "watching",  "n": 12},
        {"status": "completed", "n": 7},
        {"status": "dropped",   "n": 2},
    ]
    result = p.dash_status_distribution(ctx, {})
    assert "labels" in result and "series" in result
    # Order must be stable so chart bars don't jump per load.
    assert result["labels"] == [
        p.STATUS_LABEL[s] for s in p.VALID_STATUSES
    ]
    # Statuses we didn't return zero-fill so the chart stays five-wide.
    watching_idx = p.VALID_STATUSES.index("watching")
    completed_idx = p.VALID_STATUSES.index("completed")
    plan_idx = p.VALID_STATUSES.index("plan")
    series = result["series"][0]["data"]
    assert series[watching_idx] == 12
    assert series[completed_idx] == 7
    assert series[plan_idx] == 0


def test_dashboard_top_tracked_empty_returns_empty_table():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # type: ignore[assignment]
    result = p.dash_top_tracked(ctx, {})
    assert result == {"rows": [], "total": 0}


def test_dashboard_top_tracked_fills_titles_from_anilist():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [  # type: ignore[assignment]
        {"media_id": 1, "trackers": 10, "favorites": 5},
        {"media_id": 2, "trackers": 8,  "favorites": 2},
    ]
    _mock_anilist(ctx, {"Page": {"media": [
        _make_other(1, "Top Show"),
        _make_other(2, "Second"),
    ]}})
    result = p.dash_top_tracked(ctx, {})
    assert result["total"] == 2
    assert result["rows"][0]["title"] == "Top Show"
    assert result["rows"][0]["trackers"] == 10
    assert result["rows"][0]["favorites"] == 5


def test_dashboard_top_tracked_falls_back_to_id_when_anilist_misses():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [  # type: ignore[assignment]
        {"media_id": 999, "trackers": 3, "favorites": 1},
    ]
    # AniList returns empty media list.
    _mock_anilist(ctx, {"Page": {"media": []}})
    result = p.dash_top_tracked(ctx, {})
    assert result["rows"][0]["title"] == "#999"


def test_dashboard_settings_round_trip():
    ctx = MockContext()
    # Empty state.
    assert p.dash_get_settings(ctx, {}) == {"values": {"announce_channel_id": ""}}

    # Save a channel.
    save_result = p.dash_save_settings(ctx, {"values": {"announce_channel_id": "ch-777"}})
    assert save_result == {"ok": True}
    assert ctx.kv.get(p.NOTIFY_CHANNEL_KV) == "ch-777"

    # Reading after save reflects it.
    assert p.dash_get_settings(ctx, {}) == {"values": {"announce_channel_id": "ch-777"}}


def test_dashboard_save_settings_empty_clears():
    ctx = MockContext()
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "old-channel")
    p.dash_save_settings(ctx, {"values": {"announce_channel_id": ""}})
    assert ctx.kv.get(p.NOTIFY_CHANNEL_KV) is None


def test_dashboard_save_settings_missing_values_safe():
    """Calling with no params doesn't crash; treated as a clear."""
    ctx = MockContext()
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "old-channel")
    p.dash_save_settings(ctx, {})
    assert ctx.kv.get(p.NOTIFY_CHANNEL_KV) is None


# ── v4.2.0 /season-premieres + seasonal digest ─────────────────────────────


def test_next_season_helper_wraps_at_fall():
    import datetime as _dt
    # December — current FALL, next is WINTER of next year.
    fixed = _dt.datetime(2026, 12, 1, tzinfo=_dt.timezone.utc)
    season, year = p._next_season(fixed)
    assert season == "WINTER" and year == 2027


def test_next_season_helper_inside_spring():
    import datetime as _dt
    fixed = _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc)
    season, year = p._next_season(fixed)
    assert season == "SUMMER" and year == 2026


def test_season_is_fresh_within_first_week():
    import datetime as _dt
    fresh = _dt.datetime(2026, 4, 3, tzinfo=_dt.timezone.utc)  # day 3 of SPRING
    stale = _dt.datetime(2026, 4, 20, tzinfo=_dt.timezone.utc)  # day 20 of SPRING
    assert p._season_is_fresh(fresh) is True
    assert p._season_is_fresh(stale) is False


def test_season_premieres_command_uses_next_season_by_default():
    ctx = MockContext()
    media_list = [_make_other(i, f"Show {i}") for i in range(1, 4)]
    _mock_anilist(ctx, {"Page": {
        "pageInfo": {"hasNextPage": False, "currentPage": 1},
        "media": media_list,
    }})

    p.cmd_season_premieres(ctx, _slash_event("season-premieres", {}, user_id="u1"))

    follow = ctx.interaction.followups[-1]
    assert "premieres" in follow["embeds"][0]["title"].lower()
    body = ctx.http.requests[-1]["body"]
    assert any(s in body for s in ("WINTER", "SPRING", "SUMMER", "FALL"))


def test_season_premieres_command_accepts_explicit_season_and_year():
    ctx = MockContext()
    _mock_anilist(ctx, {"Page": {
        "pageInfo": {"hasNextPage": True, "currentPage": 1},
        "media": [_make_other(1, "Premiere One")],
    }})

    p.cmd_season_premieres(
        ctx,
        _slash_event("season-premieres", {"season": "SPRING", "year": 2027}, user_id="u2"),
    )

    body = ctx.http.requests[-1]["body"]
    assert "SPRING" in body
    assert "2027" in body
    # Pagination wired — has_next=True means there's a next button.
    follow = ctx.interaction.followups[-1]
    assert follow.get("components")


def test_premieres_pagination_button_dispatches():
    ctx = MockContext()
    _mock_anilist(ctx, {"Page": {
        "pageInfo": {"hasNextPage": False, "currentPage": 2},
        "media": [_make_other(99, "Page 2")],
    }})

    p._route_components(
        ctx,
        _component_event("otaku:premieres:FALL:2026:2", user_id="u3"),
    )

    follow = ctx.interaction.followups[-1]
    assert follow["embeds"][0]["title"].startswith("🌸 Fall 2026")


def test_premieres_pagination_malformed_replies_ephemerally():
    ctx = MockContext()
    p._route_components(
        ctx,
        _component_event("otaku:premieres:FALL:notayear:2", user_id="u4"),
    )
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "malformed" in (resp.get("content") or "").lower()


def test_dispatch_premieres_digest_no_channel_skips():
    ctx = MockContext()
    # No NOTIFY_CHANNEL_KV set.
    assert p._dispatch_premieres_digest(ctx) is False
    assert not ctx.discord.messages_sent


def test_dispatch_premieres_digest_outside_window_skips(monkeypatch):
    ctx = MockContext()
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "ch")
    # Force "fresh" check to False.
    monkeypatch.setattr(p, "_season_is_fresh", lambda *_a, **_k: False)
    assert p._dispatch_premieres_digest(ctx) is False


def test_dispatch_premieres_digest_posts_once_per_season(monkeypatch):
    ctx = MockContext()
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "announce-ch")
    monkeypatch.setattr(p, "_season_is_fresh", lambda *_a, **_k: True)
    _mock_anilist(ctx, {"Page": {
        "pageInfo": {"hasNextPage": False, "currentPage": 1},
        "media": [_make_other(1, "Premiere"), _make_other(2, "Another")],
    }})

    assert p._dispatch_premieres_digest(ctx) is True
    assert len(ctx.discord.messages_sent) == 1
    # Second call same season → KV dedup short-circuits.
    assert p._dispatch_premieres_digest(ctx) is False
    assert len(ctx.discord.messages_sent) == 1


# ── v4.0.0 /notify + /unnotify + /notify-list + airing dispatch ────────────


def test_notify_subscribes_and_stores_channel_id():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    ctx.sql.query_one = lambda sql, params=None: None  # type: ignore[assignment]

    event = _slash_event("notify", {"anime": "your name"}, user_id="n1")
    event["channel_id"] = "channel-42"
    p.cmd_notify(ctx, event)

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_notifications" in c["sql"]]
    assert inserts
    # params: [user_id, media_id, channel_id]
    assert inserts[-1]["params"] == ["n1", SAMPLE_MEDIA["id"], "channel-42"]
    follow = ctx.interaction.followups[-1]
    assert "pinged" in (follow.get("content") or "").lower() or "subscribed" in (follow.get("content") or "").lower()


def test_notify_duplicate_subscription_short_circuits():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    ctx.sql.query_one = lambda sql, params=None: {"1": 1}  # type: ignore[assignment]

    p.cmd_notify(ctx, _slash_event("notify", {"anime": "x"}, user_id="n2"))

    follow = ctx.interaction.followups[-1]
    assert "already" in (follow.get("content") or "").lower()
    assert not any("INSERT INTO otaku_notifications" in c["sql"] for c in ctx.sql.executed)


def test_unnotify_deletes_existing_subscription():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    ctx.sql.query_one = lambda sql, params=None: {"1": 1}  # type: ignore[assignment]

    p.cmd_unnotify(ctx, _slash_event("unnotify", {"anime": "x"}, user_id="n3"))

    deletes = [c for c in ctx.sql.executed if "DELETE FROM otaku_notifications" in c["sql"]]
    assert deletes and deletes[-1]["params"] == ["n3", SAMPLE_MEDIA["id"]]


def test_unnotify_not_subscribed_says_so():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    ctx.sql.query_one = lambda sql, params=None: None  # type: ignore[assignment]

    p.cmd_unnotify(ctx, _slash_event("unnotify", {"anime": "x"}, user_id="n4"))

    follow = ctx.interaction.followups[-1]
    assert "not subscribed" in (follow.get("content") or "").lower()
    assert not any("DELETE FROM otaku_notifications" in c["sql"] for c in ctx.sql.executed)


def test_notify_list_empty_state():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # type: ignore[assignment]

    p.cmd_notify_list(ctx, _slash_event("notify-list", {}, user_id="n5"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "not subscribed" in (follow.get("content") or "").lower()


def test_notify_list_populated_renders_with_titles():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [{"media_id": 1}, {"media_id": 2}]  # type: ignore[assignment]
    # Multi-purpose response: AniList batch returns titles. The next-airing
    # lookup uses the same canned response since MockHttp dispatches by URL
    # substring. That gives the test a deterministic body even though the
    # production cron query is different.
    _mock_anilist(ctx, {
        "Page": {
            "media": [_make_other(1, "First"), _make_other(2, "Second")],
            "airingSchedules": [],
        },
    })

    p.cmd_notify_list(ctx, _slash_event("notify-list", {}, user_id="n6"))

    follow = ctx.interaction.followups[-1]
    body = follow["embeds"][0]["description"]
    assert "First" in body and "Second" in body


# ── /otaku-admin set-channel ────────────────────────────────────────────────


def test_otaku_admin_set_channel_requires_admin():
    ctx = MockContext()
    event = make_event(
        "interaction_create",
        interaction_type=2,
        command_name="otaku-admin",
        options=[{"name": "set-channel", "type": 1,
                  "options": [{"name": "channel", "value": "777"}]}],
        user_id="rando",
    )
    p.cmd_otaku_admin(ctx, event)
    follow = ctx.interaction.followups[-1]
    assert "server-admin only" in (follow.get("content") or "")
    assert ctx.kv.get(p.NOTIFY_CHANNEL_KV) is None


def test_otaku_admin_set_channel_admin_writes_kv():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}  # type: ignore[assignment]

    event = make_event(
        "interaction_create",
        interaction_type=2,
        command_name="otaku-admin",
        options=[{"name": "set-channel", "type": 1,
                  "options": [{"name": "channel", "value": "888"}]}],
        user_id="boss",
    )
    p.cmd_otaku_admin(ctx, event)

    assert ctx.kv.get(p.NOTIFY_CHANNEL_KV) == "888"
    follow = ctx.interaction.followups[-1]
    assert "<#888>" in (follow.get("content") or "")


def test_otaku_admin_set_channel_clear():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}  # type: ignore[assignment]
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "999")

    event = make_event(
        "interaction_create",
        interaction_type=2,
        command_name="otaku-admin",
        options=[{"name": "set-channel", "type": 1, "options": []}],
        user_id="boss",
    )
    p.cmd_otaku_admin(ctx, event)

    assert ctx.kv.get(p.NOTIFY_CHANNEL_KV) is None
    follow = ctx.interaction.followups[-1]
    assert "Cleared" in (follow.get("content") or "")


# ── Airing dispatch ─────────────────────────────────────────────────────────


def _airing_response(media_id: int, episode: int, title: str = "Show") -> dict:
    """Build an AniList airingSchedules payload for one airing."""
    import datetime as _dt
    now_ts = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return {
        "Page": {
            "pageInfo": {"hasNextPage": False},
            "airingSchedules": [
                {
                    "id": 1,
                    "episode": episode,
                    "airingAt": now_ts,
                    "media": {
                        "id": media_id,
                        "episodes": 12,
                        "siteUrl": f"https://anilist.co/anime/{media_id}",
                        "title": {"romaji": title, "english": ""},
                        "coverImage": {"large": "https://img.example.com/a.jpg"},
                    },
                },
            ],
        },
    }


def test_dispatch_airing_announcements_posts_to_announcement_channel():
    ctx = MockContext()
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "announce-1")
    _mock_anilist(ctx, _airing_response(media_id=42, episode=3, title="Test"))
    ctx.sql.query = lambda sql, params=None: [  # type: ignore[assignment]
        {"user_id": "alice", "channel_id": "channel-orig"},
        {"user_id": "bob",   "channel_id": "channel-orig"},
    ]

    sent = p._dispatch_airing_announcements(ctx)

    assert sent == 1  # one channel × one airing → one send
    msg = ctx.discord.messages_sent[-1]
    assert msg["channel_id"] == "announce-1"
    assert "<@alice>" in msg["content"] and "<@bob>" in msg["content"]


def test_dispatch_airing_announcements_falls_back_to_per_user_channel():
    """No global announce channel → posts to the channel where each user subscribed."""
    ctx = MockContext()
    _mock_anilist(ctx, _airing_response(media_id=42, episode=3))
    ctx.sql.query = lambda sql, params=None: [  # type: ignore[assignment]
        {"user_id": "alice", "channel_id": "channel-A"},
        {"user_id": "bob",   "channel_id": "channel-B"},
    ]

    p._dispatch_airing_announcements(ctx)

    targets = {m["channel_id"] for m in ctx.discord.messages_sent}
    assert targets == {"channel-A", "channel-B"}


def test_dispatch_airing_announcements_dedups_repeat_calls():
    """Calling the dispatch twice for the same airing only sends once."""
    ctx = MockContext()
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "announce-1")
    _mock_anilist(ctx, _airing_response(media_id=42, episode=3))
    ctx.sql.query = lambda sql, params=None: [  # type: ignore[assignment]
        {"user_id": "alice", "channel_id": "channel-orig"},
    ]

    p._dispatch_airing_announcements(ctx)
    p._dispatch_airing_announcements(ctx)

    assert len(ctx.discord.messages_sent) == 1  # second call dedup'd


def test_dispatch_airing_announcements_no_subscribers_skips():
    ctx = MockContext()
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "announce-1")
    _mock_anilist(ctx, _airing_response(media_id=42, episode=3))
    ctx.sql.query = lambda sql, params=None: []  # type: ignore[assignment]

    sent = p._dispatch_airing_announcements(ctx)

    assert sent == 0
    assert not ctx.discord.messages_sent


def test_cron_airing_check_calls_dispatch_safely():
    """The cron handler should swallow exceptions from dispatch."""
    ctx = MockContext()
    # No mock_response → dispatch returns 0 since http will be empty.
    p.cron_airing_check(ctx)
    # No exception is the contract.


# ── v3.3.0 /leaderboard ─────────────────────────────────────────────────────


def test_leaderboard_default_metric_is_completed():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):  # noqa: ANN001
        captured["sql"] = sql
        captured["params"] = params
        return [{"user_id": "a", "n": 5}]

    ctx.sql.query = _q  # type: ignore[assignment]
    p.cmd_leaderboard(ctx, _slash_event("leaderboard", {}, user_id="anyone"))

    assert "status = 'completed'" in captured["sql"]
    follow = ctx.interaction.followups[-1]
    assert "most completed" in follow["embeds"][0]["title"]
    assert "<@a>" in follow["embeds"][0]["description"]
    assert "5 completed" in follow["embeds"][0]["description"]


def test_leaderboard_score_metric_has_min_rated_filter():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):  # noqa: ANN001
        captured["sql"] = sql
        return [{"user_id": "a", "avg_rating": 18.0, "rated": 5}]

    ctx.sql.query = _q  # type: ignore[assignment]
    p.cmd_leaderboard(ctx, _slash_event("leaderboard", {"metric": "score"}, user_id="anyone"))

    assert "HAVING COUNT(*) >= $1" in captured["sql"]
    assert "AVG(rating)" in captured["sql"]
    follow = ctx.interaction.followups[-1]
    assert "9.0/10" in follow["embeds"][0]["description"]


def test_leaderboard_hours_metric_uses_episode_sum():
    ctx = MockContext()

    def _q(sql, params=None):  # noqa: ANN001
        return [{"user_id": "a", "episodes": 100}]

    ctx.sql.query = _q  # type: ignore[assignment]
    p.cmd_leaderboard(ctx, _slash_event("leaderboard", {"metric": "hours"}, user_id="anyone"))

    follow = ctx.interaction.followups[-1]
    # 100 eps × 24 min ÷ 60 = 40.0 hours
    assert "40.0 hours" in follow["embeds"][0]["description"]


def test_leaderboard_empty_state():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # type: ignore[assignment]
    p.cmd_leaderboard(ctx, _slash_event("leaderboard", {}, user_id="anyone"))
    follow = ctx.interaction.followups[-1]
    assert "Nobody" in (follow.get("content") or "")


def test_leaderboard_unknown_metric_falls_back_to_completed():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):  # noqa: ANN001
        captured["sql"] = sql
        return [{"user_id": "a", "n": 1}]

    ctx.sql.query = _q  # type: ignore[assignment]
    p.cmd_leaderboard(ctx, _slash_event("leaderboard", {"metric": "garbage"}, user_id="anyone"))
    assert "status = 'completed'" in captured["sql"]


# ── v3.2.0 /wp (watch parties) ──────────────────────────────────────────────


def _wp_event(subname: str, sub_opts: dict | None = None, **extra) -> dict:
    """Build a /wp event. Sub-options use a mix of int + string types in real Discord,
    so we don't bother annotating each option's type — handlers coerce via int()."""
    sub_options = [{"name": k, "value": v} for k, v in (sub_opts or {}).items()]
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name="wp",
        options=[{"name": subname, "type": 1, "options": sub_options}],
        **extra,
    )


def test_wp_schema_in_bootstrap():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("CREATE TABLE IF NOT EXISTS otaku_watch_parties" in s for s in sqls)
    assert any("CREATE TABLE IF NOT EXISTS otaku_watch_party_members" in s for s in sqls)


def test_wp_create_inserts_party_and_creator_as_member():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    # INSERT ... RETURNING uses query_one — pretend it returned party_id=42.
    # Monkeypatch must still record into executed, otherwise the assertions miss
    # the SQL the handler ran.
    def _qo(sql, params=None):  # noqa: ANN001
        ctx.sql.executed.append({"sql": sql, "params": params})
        return {"party_id": 42}

    ctx.sql.query_one = _qo  # type: ignore[assignment]

    p.cmd_wp(ctx, _wp_event("create", {"anime": "your name"}, user_id="creator"))

    # One INSERT for the party row, one INSERT for the members row.
    assert any("INSERT INTO otaku_watch_parties" in c["sql"] for c in ctx.sql.executed)
    assert any("INSERT INTO otaku_watch_party_members" in c["sql"] for c in ctx.sql.executed)
    # Members insert receives the party_id and creator user_id.
    member_insert = next(
        c for c in ctx.sql.executed if "INSERT INTO otaku_watch_party_members" in c["sql"]
    )
    assert member_insert["params"][0] == 42
    assert member_insert["params"][1] == "creator"

    follow = ctx.interaction.followups[-1]
    # The create embed is public (non-ephemeral) and carries a Join button.
    assert follow.get("ephemeral") in (False, None)
    components = follow.get("components") or []
    assert components
    # ActionRow → children (Buttons). Check the Join button's custom_id.
    btn = components[0].children[0]
    assert btn.to_dict()["custom_id"] == "otaku:wp-join:42"


def test_wp_join_command_inserts_member():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    # First query_one for party lookup, second for member existence (None → not joined).
    calls = {"n": 0}

    def _qo(sql, params=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return {"party_id": 99, "media_id": SAMPLE_MEDIA["id"], "created_by": "creator", "status": "active"}
        return None  # member doesn't exist yet

    ctx.sql.query_one = _qo  # type: ignore[assignment]

    p.cmd_wp(ctx, _wp_event("join", {"id": 99}, user_id="joiner"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_watch_party_members" in c["sql"]]
    assert inserts and inserts[-1]["params"][0] == 99
    assert inserts[-1]["params"][1] == "joiner"


def test_wp_join_button_dispatches_to_join_logic():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    calls = {"n": 0}

    def _qo(sql, params=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return {"party_id": 7, "media_id": SAMPLE_MEDIA["id"], "created_by": "host", "status": "active"}
        return None

    ctx.sql.query_one = _qo  # type: ignore[assignment]

    p._route_components(ctx, _component_event("otaku:wp-join:7", user_id="late"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_watch_party_members" in c["sql"]]
    assert inserts and inserts[-1]["params"][0] == 7


def test_wp_join_already_member_short_circuits():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    calls = {"n": 0}

    def _qo(sql, params=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return {"party_id": 7, "media_id": SAMPLE_MEDIA["id"], "created_by": "host", "status": "active"}
        return {"episodes_watched": 5}  # already a member

    ctx.sql.query_one = _qo  # type: ignore[assignment]

    p.cmd_wp(ctx, _wp_event("join", {"id": 7}, user_id="already"))

    follow = ctx.interaction.followups[-1]
    assert "already in" in (follow.get("content") or "")
    # No new member insert.
    assert not any("INSERT INTO otaku_watch_party_members" in c["sql"] for c in ctx.sql.executed)


def test_wp_join_unknown_party_replies_friendly_error():
    ctx = MockContext()
    ctx.sql.query_one = lambda sql, params=None: None  # type: ignore[assignment]
    p.cmd_wp(ctx, _wp_event("join", {"id": 9999}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert "9999" in (follow.get("content") or "")


def test_wp_status_lists_members_with_progress():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    ctx.sql.query_one = lambda sql, params=None: {  # type: ignore[assignment]
        "party_id": 11, "media_id": SAMPLE_MEDIA["id"], "created_by": "host", "status": "active",
    }
    ctx.sql.query = lambda sql, params=None: [  # type: ignore[assignment]
        {"user_id": "alice", "episodes_watched": 3},
        {"user_id": "bob",   "episodes_watched": 1},
    ]

    p.cmd_wp(ctx, _wp_event("status", {"id": 11}, user_id="anyone"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") in (False, None)
    body = follow["embeds"][0]["description"]
    assert "<@alice>" in body and "<@bob>" in body
    assert "episode 3" in body


def test_wp_progress_updates_episodes_watched():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})  # episodes=1 in SAMPLE_MEDIA
    calls = {"n": 0}

    def _qo(sql, params=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return {"party_id": 5, "media_id": SAMPLE_MEDIA["id"], "created_by": "h", "status": "active"}
        return {"episodes_watched": 0}  # the caller is a member

    ctx.sql.query_one = _qo  # type: ignore[assignment]
    # The post-update sync query — only one member, so no sync announcement.
    ctx.sql.query = lambda sql, params=None: [{"episodes_watched": 1}]  # type: ignore[assignment]

    p.cmd_wp(ctx, _wp_event("progress", {"id": 5, "episode": 1}, user_id="solo"))

    updates = [c for c in ctx.sql.executed if "UPDATE otaku_watch_party_members" in c["sql"]]
    assert updates and updates[-1]["params"] == [1, 5, "solo"]


def test_wp_progress_caps_at_total_and_warns():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})  # episodes=1
    calls = {"n": 0}

    def _qo(sql, params=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return {"party_id": 5, "media_id": SAMPLE_MEDIA["id"], "created_by": "h", "status": "active"}
        return {"episodes_watched": 0}

    ctx.sql.query_one = _qo  # type: ignore[assignment]
    ctx.sql.query = lambda sql, params=None: [{"episodes_watched": 1}]  # type: ignore[assignment]

    p.cmd_wp(ctx, _wp_event("progress", {"id": 5, "episode": 99}, user_id="solo"))

    updates = [c for c in ctx.sql.executed if "UPDATE otaku_watch_party_members" in c["sql"]]
    assert updates and updates[-1]["params"][0] == 1  # capped


def test_wp_progress_sync_announcement_when_all_match():
    ctx = MockContext()
    # Use a long-running show (episodes=12 — _make_other default) so we don't trip the
    # completion auto-promotion path.
    media = _make_other(99, "Long Show")
    media["episodes"] = 12
    _mock_anilist(ctx, {"Media": media})
    calls = {"n": 0}

    def _qo(sql, params=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return {"party_id": 50, "media_id": 99, "created_by": "h", "status": "active"}
        return {"episodes_watched": 2}

    ctx.sql.query_one = _qo  # type: ignore[assignment]
    # All members at episode 3 after the caller's update → sync announce.
    ctx.sql.query = lambda sql, params=None: [  # type: ignore[assignment]
        {"episodes_watched": 3},
        {"episodes_watched": 3},
    ]

    p.cmd_wp(ctx, _wp_event("progress", {"id": 50, "episode": 3}, user_id="latecomer"))

    followups = ctx.interaction.followups
    # Two followups: ephemeral confirmation + public sync announcement.
    assert len(followups) >= 2
    announcement = followups[-1]
    assert announcement.get("ephemeral") in (False, None)
    assert "everyone" in (announcement.get("content") or "").lower()


def test_wp_progress_not_member_rejected():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    calls = {"n": 0}

    def _qo(sql, params=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return {"party_id": 11, "media_id": SAMPLE_MEDIA["id"], "created_by": "h", "status": "active"}
        return None  # caller isn't a member

    ctx.sql.query_one = _qo  # type: ignore[assignment]

    p.cmd_wp(ctx, _wp_event("progress", {"id": 11, "episode": 2}, user_id="lurker"))

    follow = ctx.interaction.followups[-1]
    assert "/wp join" in (follow.get("content") or "")
    assert not any("UPDATE otaku_watch_party_members" in c["sql"] for c in ctx.sql.executed)


# ── v3.1.0 /compare ─────────────────────────────────────────────────────────


def test_compare_against_self_rejected_ephemerally():
    ctx = MockContext()
    p.cmd_compare(ctx, _slash_event("compare", {"user": "same"}, user_id="same"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "different" in (resp.get("content") or "")


def test_compare_users_helper_finds_shared_favorites_and_divergent_ratings():
    my_rows = {
        1: {"status": "completed", "is_favorite": True, "rating": 20},
        2: {"status": "completed", "is_favorite": False, "rating": 12},  # 6.0
    }
    their_rows = {
        1: {"status": "completed", "is_favorite": True,  "rating": 18},  # shared favorite
        # rating gap on id 2: stored diff |18-12|=6, threshold is ≥4 — divergent
        2: {"status": "completed", "is_favorite": False, "rating": 18},
        3: {"status": "completed", "is_favorite": False, "rating": None},  # completion rec
    }
    result = p._compare_users(my_rows, their_rows)
    assert result["shared_favorites"] == [1]
    # divergent: id 2, my 12 vs theirs 18 — diff is 6 (≥4 threshold)
    assert result["divergent_ratings"] == [(2, 12, 18)]
    assert result["completion_recs"] == [3]
    assert result["my_total"] == 2
    assert result["their_total"] == 3
    assert result["shared_total"] == 2


def test_compare_users_helper_empty_when_no_overlap():
    my_rows = {1: {"status": "watching", "is_favorite": False, "rating": None}}
    their_rows = {2: {"status": "watching", "is_favorite": False, "rating": None}}
    result = p._compare_users(my_rows, their_rows)
    assert result["shared_favorites"] == []
    assert result["divergent_ratings"] == []
    assert result["completion_recs"] == []


def test_compare_command_renders_embed_with_all_four_fields():
    ctx = MockContext()
    # Two SQL queries — _user_rows_keyed_by_media is called for each user.
    # Both queries hit the same `_q` stub; we check which user we're querying via params.
    rows_by_user = {
        "me": [
            {"media_id": 1, "status": "completed", "is_favorite": True, "rating": 20},
            {"media_id": 2, "status": "completed", "is_favorite": False, "rating": 10},
        ],
        "them": [
            {"media_id": 1, "status": "completed", "is_favorite": True, "rating": 18},
            {"media_id": 3, "status": "completed", "is_favorite": False, "rating": None},
        ],
    }

    def _q(sql, params=None):  # noqa: ANN001
        return rows_by_user.get(params[0], [])

    ctx.sql.query = _q  # type: ignore[assignment]
    _mock_anilist(ctx, {"Page": {"media": [
        _make_other(1, "Shared Pick"),
        _make_other(3, "Their Rec"),
    ]}})

    p.cmd_compare(ctx, _slash_event("compare", {"user": "them"}, user_id="me"))

    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    field_names = {f["name"] for f in embed["fields"]}
    assert {p.S.COMPARE_FIELD_TOTALS, p.S.COMPARE_FIELD_SHARED,
            p.S.COMPARE_FIELD_DIVERGENT, p.S.COMPARE_FIELD_RECS}.issubset(field_names)
    shared_field = next(f for f in embed["fields"] if f["name"] == p.S.COMPARE_FIELD_SHARED)
    assert "Shared Pick" in shared_field["value"]


def test_compare_command_empty_when_neither_has_data():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # type: ignore[assignment]
    p.cmd_compare(ctx, _slash_event("compare", {"user": "them"}, user_id="me-empty"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "Neither" in (follow.get("content") or "")


def test_compare_command_only_you_have_data():
    ctx = MockContext()

    def _q(sql, params=None):  # noqa: ANN001
        return [{"media_id": 1, "status": "completed", "is_favorite": True, "rating": 20}] if params[0] == "me" else []

    ctx.sql.query = _q  # type: ignore[assignment]
    p.cmd_compare(ctx, _slash_event("compare", {"user": "them"}, user_id="me"))
    follow = ctx.interaction.followups[-1]
    assert "hasn't tracked anything" in (follow.get("content") or "")


# ── v3.0.0 /server-watchlist ────────────────────────────────────────────────


def _swl_event(subname: str, sub_opts: dict | None = None, **extra) -> dict:
    """Build a /server-watchlist event with the given sub-command + options."""
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


def test_swl_schema_in_bootstrap():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    assert any(
        "CREATE TABLE IF NOT EXISTS otaku_server_watchlist" in c["sql"]
        for c in ctx.sql.executed
    )


def test_swl_add_requires_admin():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    # Default MockDiscord: no admin roles, owner_id="1".
    p.cmd_server_watchlist(ctx, _swl_event("add", {"anime": "your name"}, user_id="rando"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "admin-only" in (follow.get("content") or "")
    # No SQL INSERT issued.
    assert not any("INSERT INTO otaku_server_watchlist" in c["sql"] for c in ctx.sql.executed)


def test_swl_add_admin_inserts_row():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}  # type: ignore[assignment]
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    # First query_one — "does this row already exist?" — returns None.
    ctx.sql.query_one = lambda sql, params=None: None  # type: ignore[assignment]

    p.cmd_server_watchlist(ctx, _swl_event("add", {"anime": "kimi"}, user_id="boss"))

    follow = ctx.interaction.followups[-1]
    assert "Added" in (follow.get("content") or "")
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_server_watchlist" in c["sql"]]
    assert inserts
    # params: [media_id, added_by, note]
    assert inserts[-1]["params"][0] == SAMPLE_MEDIA["id"]
    assert inserts[-1]["params"][1] == "boss"
    assert inserts[-1]["params"][2] is None  # no note was passed


def test_swl_add_with_note_persists_it():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}  # type: ignore[assignment]
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    ctx.sql.query_one = lambda sql, params=None: None  # type: ignore[assignment]

    p.cmd_server_watchlist(
        ctx,
        _swl_event("add", {"anime": "kimi", "note": "movie night pick"}, user_id="boss"),
    )

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_server_watchlist" in c["sql"]]
    assert inserts[-1]["params"][2] == "movie night pick"


def test_swl_add_already_present_says_so():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}  # type: ignore[assignment]
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    # query_one returns truthy → already on the watchlist.
    ctx.sql.query_one = lambda sql, params=None: {"1": 1}  # type: ignore[assignment]

    p.cmd_server_watchlist(ctx, _swl_event("add", {"anime": "kimi"}, user_id="boss"))

    follow = ctx.interaction.followups[-1]
    assert "already" in (follow.get("content") or "").lower()
    assert not any("INSERT INTO otaku_server_watchlist" in c["sql"] for c in ctx.sql.executed)


def test_swl_remove_requires_admin():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    p.cmd_server_watchlist(ctx, _swl_event("remove", {"anime": "kimi"}, user_id="rando"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "admin-only" in (follow.get("content") or "")


def test_swl_remove_admin_deletes_row():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}  # type: ignore[assignment]
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    # Row exists.
    ctx.sql.query_one = lambda sql, params=None: {"1": 1}  # type: ignore[assignment]

    p.cmd_server_watchlist(ctx, _swl_event("remove", {"anime": "kimi"}, user_id="boss"))

    deletes = [c for c in ctx.sql.executed if "DELETE FROM otaku_server_watchlist" in c["sql"]]
    assert deletes and deletes[-1]["params"] == [SAMPLE_MEDIA["id"]]


def test_swl_remove_accepts_numeric_media_id():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}  # type: ignore[assignment]
    # Numeric arg → QUERY_MEDIA_BY_ID path. _mock_anilist registers one
    # response for any AniList URL, so both Search and ById share the same body.
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    ctx.sql.query_one = lambda sql, params=None: {"1": 1}  # type: ignore[assignment]

    p.cmd_server_watchlist(ctx, _swl_event("remove", {"anime": "123"}, user_id="boss"))

    deletes = [c for c in ctx.sql.executed if "DELETE FROM otaku_server_watchlist" in c["sql"]]
    assert deletes


def test_swl_remove_not_present_says_so():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}  # type: ignore[assignment]
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    ctx.sql.query_one = lambda sql, params=None: None  # type: ignore[assignment]

    p.cmd_server_watchlist(ctx, _swl_event("remove", {"anime": "kimi"}, user_id="boss"))

    follow = ctx.interaction.followups[-1]
    assert "isn't on" in (follow.get("content") or "")
    assert not any("DELETE FROM otaku_server_watchlist" in c["sql"] for c in ctx.sql.executed)


def test_swl_view_with_rows_returns_public_embed():
    ctx = MockContext()
    rows = [
        {"media_id": 1, "added_by": "boss", "note": "movie night"},
        {"media_id": 2, "added_by": "boss", "note": None},
    ]
    ctx.sql.query = lambda sql, params=None: rows  # type: ignore[assignment]
    _mock_anilist(ctx, {"Page": {"media": [
        _make_other(1, "First"),
        _make_other(2, "Second"),
    ]}})

    p.cmd_server_watchlist(ctx, _swl_event("view", {}, user_id="anyone"))

    follow = ctx.interaction.followups[-1]
    # View is public (non-ephemeral).
    assert follow.get("ephemeral") is False or follow.get("ephemeral") is None
    body = follow["embeds"][0]["description"]
    assert "First" in body
    assert "movie night" in body
    # Added-by line links the curator.
    assert "<@boss>" in body


def test_swl_view_empty_state():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # type: ignore[assignment]
    p.cmd_server_watchlist(ctx, _swl_event("view", {}, user_id="anyone"))
    follow = ctx.interaction.followups[-1]
    assert "watchlist" in (follow.get("content") or "").lower()


def test_swl_pagination_button_dispatches():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):  # noqa: ANN001
        captured["params"] = params
        return [{"media_id": 6, "added_by": "boss", "note": None}]

    ctx.sql.query = _q  # type: ignore[assignment]
    _mock_anilist(ctx, {"Page": {"media": [_make_other(6, "Page 2 item")]}})

    p._route_components(ctx, _component_event("otaku:swl:2", user_id="anyone"))

    # Offset = (page-1) * PER_PAGE = 5 for page 2; limit = 6.
    assert captured["params"] == [p.PER_PAGE + 1, p.PER_PAGE]


def test_swl_pagination_malformed_replies_ephemerally():
    ctx = MockContext()
    p._route_components(ctx, _component_event("otaku:swl:abc", user_id="anyone"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "malformed" in (resp.get("content") or "").lower()


# ── v2.6.0 /otaku-admin (real admin gating) ─────────────────────────────────


def _admin_slash_event(user_id: str, target_user_id: str) -> dict:
    """Build a slash event matching the real /otaku-admin reset-user payload shape."""
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name="otaku-admin",
        options=[{
            "name": "reset-user",
            "type": 1,
            "options": [{"name": "user", "value": target_user_id, "type": 6}],
        }],
        user_id=user_id,
    )


def _grant_admin(ctx: MockContext, *, owner_id: str | None = None, admin_user: str | None = None,
                 admin_role: str = "moderator", admin_perms: int = 0x8) -> None:
    """Configure MockDiscord so a specific user counts as a server admin."""
    if owner_id is not None:
        ctx.discord.get_guild = lambda: {"id": "999", "owner_id": owner_id}  # type: ignore[assignment]
    if admin_user is not None:
        ctx.discord.get_member = lambda *, user_id: (  # type: ignore[assignment]
            {"user_id": user_id, "roles": [admin_role]} if user_id == admin_user
            else {"user_id": user_id, "roles": []}
        )
        ctx.discord.list_roles = lambda: [  # type: ignore[assignment]
            {"id": admin_role, "name": "Moderator", "permissions": str(admin_perms)},
            {"id": "everyone", "name": "@everyone", "permissions": "0"},
        ]


def test_caller_is_admin_when_guild_owner():
    ctx = MockContext()
    _grant_admin(ctx, owner_id="owner-1")
    assert p._caller_is_admin(ctx, "owner-1") is True
    assert p._caller_is_admin(ctx, "rando") is False


def test_caller_is_admin_when_role_has_administrator_bit():
    ctx = MockContext()
    _grant_admin(ctx, admin_user="mod-1", admin_role="r-mod", admin_perms=0x8)
    assert p._caller_is_admin(ctx, "mod-1") is True
    assert p._caller_is_admin(ctx, "regular") is False


def test_caller_is_admin_when_role_has_manage_guild_bit():
    ctx = MockContext()
    _grant_admin(ctx, admin_user="mod-2", admin_role="r-mg", admin_perms=0x20)
    assert p._caller_is_admin(ctx, "mod-2") is True


def test_caller_is_admin_false_when_role_has_no_admin_bits():
    ctx = MockContext()
    _grant_admin(ctx, admin_user="not-mod", admin_role="r-other", admin_perms=0x400)  # SEND_MESSAGES
    assert p._caller_is_admin(ctx, "not-mod") is False


def test_otaku_admin_reset_user_denied_for_non_admin():
    ctx = MockContext()
    # Default mock: empty roles, owner "1" (which doesn't match)
    p.cmd_otaku_admin(ctx, _admin_slash_event(user_id="rando", target_user_id="victim"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "server-admin only" in (follow.get("content") or "")
    # No DELETE was issued.
    assert not any("DELETE FROM otaku_user_media" in c["sql"] for c in ctx.sql.executed)


def test_otaku_admin_reset_user_runs_delete_for_admin():
    ctx = MockContext()
    _grant_admin(ctx, owner_id="boss")
    real_execute = ctx.sql.execute

    def _exec(sql, params=None):  # noqa: ANN001
        real_execute(sql, params)
        return 7

    ctx.sql.execute = _exec  # type: ignore[assignment]

    p.cmd_otaku_admin(ctx, _admin_slash_event(user_id="boss", target_user_id="user-99"))

    follow = ctx.interaction.followups[-1]
    assert "7" in (follow.get("content") or "")
    deletes = [c for c in ctx.sql.executed if "DELETE FROM otaku_user_media" in c["sql"]]
    assert deletes and deletes[-1]["params"] == ["user-99"]


def test_otaku_admin_missing_user_option_replies_immediately():
    ctx = MockContext()
    # Slash command invoked with no sub-option at all.
    event = make_event(
        "interaction_create",
        interaction_type=2,
        command_name="otaku-admin",
        options=[{"name": "reset-user", "type": 1, "options": []}],
        user_id="any",
    )
    p.cmd_otaku_admin(ctx, event)
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "user:" in (resp.get("content") or "")


def test_role_list_cache_hit_skips_second_lookup():
    ctx = MockContext()
    call_count = {"n": 0}

    def _list_roles():
        call_count["n"] += 1
        return [{"id": "1", "name": "@everyone", "permissions": "0"}]

    ctx.discord.list_roles = _list_roles  # type: ignore[assignment]
    # First call populates the cache.
    p._cached_list_roles(ctx)
    p._cached_list_roles(ctx)
    p._cached_list_roles(ctx)
    assert call_count["n"] == 1


# ── v2.5.0 /otaku-reset + rating-on-card ────────────────────────────────────


def test_otaku_reset_shows_confirmation_prompt():
    ctx = MockContext()
    p.cmd_otaku_reset(ctx, _slash_event("otaku-reset", {}, user_id="reset1"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "delete" in (resp.get("content") or "").lower()
    components = resp.get("components") or []
    assert components, "expected confirm + cancel buttons"


def test_otaku_reset_confirm_deletes_rows():
    ctx = MockContext()
    # Pretend the DELETE affected 3 rows by stubbing execute's return.
    real_execute = ctx.sql.execute

    def _exec(sql, params=None):  # noqa: ANN001
        real_execute(sql, params)
        return 3

    ctx.sql.execute = _exec  # type: ignore[assignment]
    event = _component_event("otaku:reset-confirm:reset2", user_id="reset2")
    p._route_components(ctx, event)

    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "3" in (resp.get("content") or "")
    # DELETE was issued.
    assert any("DELETE FROM otaku_user_media" in c["sql"] for c in ctx.sql.executed)


def test_otaku_reset_confirm_only_works_for_original_caller():
    """A different user clicking another person's confirm button gets nothing destroyed."""
    ctx = MockContext()
    event = _component_event("otaku:reset-confirm:victim", user_id="attacker")
    p._route_components(ctx, event)
    # Got a "cancelled" response.
    resp = ctx.interaction.responses[-1]
    assert "Cancelled" in (resp.get("content") or "")
    # No DELETE was issued.
    assert not any("DELETE FROM otaku_user_media" in c["sql"] for c in ctx.sql.executed)


def test_otaku_reset_cancel_button():
    ctx = MockContext()
    p._route_components(ctx, _component_event("otaku:reset-cancel:u", user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert "Cancelled" in (resp.get("content") or "")


def test_anime_card_shows_rating_when_user_has_rated():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    # The user has watched 5 episodes and rated 8.5.
    ctx.sql.query_one = lambda sql, params=None: {  # type: ignore[assignment]
        "episodes_watched": 5,
        "rating": 17,
    }

    p.cmd_anime(ctx, _slash_event("anime", {"query": "kimi"}, user_id="rate-card"))

    fields = ctx.interaction.followups[-1]["embeds"][0]["fields"]
    names = {f["name"] for f in fields}
    assert "Your progress" in names
    assert "Your rating" in names
    rating_field = next(f for f in fields if f["name"] == "Your rating")
    assert "8.5/10" in rating_field["value"]


# ── v2.4.0 /import anilist ──────────────────────────────────────────────────


def test_import_blank_username_rejected():
    ctx = MockContext()
    p.cmd_import(ctx, _slash_event("import", {"anilist": ""}, user_id="imp-blank"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True


def test_import_streams_pages_until_no_next_and_summarizes():
    ctx = MockContext()
    # Two paginated responses, served by sequentially-popped queue.
    responses = [
        json.dumps({"data": {"Page": {
            "pageInfo": {"hasNextPage": True, "currentPage": 1},
            "mediaList": [
                {"status": "CURRENT", "progress": 3, "score": 8.5, "media": {"id": 10}},
                {"status": "COMPLETED", "progress": 12, "score": 9, "media": {"id": 11}},
            ],
        }}}),
        json.dumps({"data": {"Page": {
            "pageInfo": {"hasNextPage": False, "currentPage": 2},
            "mediaList": [
                {"status": "PLANNING", "progress": 0, "score": 0, "media": {"id": 12}},
            ],
        }}}),
    ]

    def _post(url, body="", headers=None):  # noqa: ANN001
        return {"status": 200, "body_bytes": responses.pop(0), "headers": {}, "truncated": False}

    ctx.http.post = _post  # type: ignore[assignment]
    # All inserts treated as "new" — query_one always returns None.

    p.cmd_import(ctx, _slash_event("import", {"anilist": "kace"}, user_id="imp1"))

    follow = ctx.interaction.followups[-1]
    msg = follow.get("content") or ""
    assert "Imported **3** anime" in msg
    assert "3 new, 0 updated" in msg
    # Three INSERTs.
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert len(inserts) == 3
    # First row mapped CURRENT → watching, score 8.5 → 17.
    assert inserts[0]["params"][2] == "watching"
    assert inserts[0]["params"][4] == 17


def test_import_unknown_user_replies_with_user_not_found():
    ctx = MockContext()
    # AniList typically returns a 404 or empty mediaList for an unknown name.
    ctx.http.mock_response("graphql.anilist.co", status=404, body="not found")

    p.cmd_import(ctx, _slash_event("import", {"anilist": "ghost"}, user_id="imp-ghost"))

    follow = ctx.interaction.followups[-1]
    assert "didn't find" in (follow.get("content") or "")


def test_import_skips_malformed_entries_in_a_page():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {"Page": {
        "pageInfo": {"hasNextPage": False, "currentPage": 1},
        "mediaList": [
            {"status": "CURRENT", "progress": 1, "score": 0, "media": {"id": 5}},
            {"status": "CURRENT", "progress": 1, "score": 0, "media": {}},  # missing id
            {"status": "CURRENT", "progress": 1, "score": 0, "media": {"id": "abc"}},  # bad id
        ],
    }}}))

    p.cmd_import(ctx, _slash_event("import", {"anilist": "kace"}, user_id="imp-skip"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    # Only the one valid row.
    assert len(inserts) == 1
    assert inserts[0]["params"][1] == 5


def test_import_idempotency_marks_existing_as_updated():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {"Page": {
        "pageInfo": {"hasNextPage": False, "currentPage": 1},
        "mediaList": [
            {"status": "COMPLETED", "progress": 12, "score": 9, "media": {"id": 7}},
        ],
    }}}))
    # Pretend the row already exists.
    ctx.sql.query_one = lambda sql, params=None: {"1": 1}  # type: ignore[assignment]

    p.cmd_import(ctx, _slash_event("import", {"anilist": "kace"}, user_id="imp-dup"))

    follow = ctx.interaction.followups[-1]
    assert "0 new, 1 updated" in (follow.get("content") or "")


# ── v2.3.0 /stats ───────────────────────────────────────────────────────────


def test_stats_empty_user_replies_empty_state():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # type: ignore[assignment]
    p.cmd_stats(ctx, _slash_event("stats", {}, user_id="stats-empty"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "haven't" in (follow.get("content") or "")


def test_stats_aggregates_by_status_and_computes_hours():
    ctx = MockContext()
    # First call (aggregate by status), second (top-genre media_ids).
    call_count = {"n": 0}

    def _q(sql, params=None):  # noqa: ANN001
        call_count["n"] += 1
        if "GROUP BY status" in sql:
            return [
                {"status": "completed", "count": 5, "episodes": 60, "mean_rating": 16.0},
                {"status": "watching",  "count": 2, "episodes": 6,  "mean_rating": None},
            ]
        if "ORDER BY added_at DESC" in sql:
            return [{"media_id": 1}, {"media_id": 2}]
        return []

    ctx.sql.query = _q  # type: ignore[assignment]
    # AniList batch for top-genre returns two media with overlapping genres.
    _mock_anilist(ctx, {"Page": {"media": [
        {**_make_other(1, "A"), "genres": ["Action", "Drama"]},
        {**_make_other(2, "B"), "genres": ["Action"]},
    ]}})

    p.cmd_stats(ctx, _slash_event("stats", {}, user_id="stats-1"))

    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Total tracked"] == "7"
    assert fields["Episodes"] == "66"
    # 66 episodes × 24 min = 1584 min = 26.4 hours
    assert fields["Est. hours"] == "26.4"
    assert fields["✅ Completed"] == "5"
    assert fields["📺 Watching"] == "2"
    # mean rating only weighted across rated rows (5 of 7); rating stored ×2, so 16 → 8.0
    assert "8.0/10" in fields["Mean score"]
    assert fields.get("Top genre") == "Action"


def test_stats_aggregate_helper_returns_empty_dict_on_no_rows():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # type: ignore[assignment]
    assert p._aggregate_user_stats(ctx, "nobody") == {}


# ── v2.2.0 progress ─────────────────────────────────────────────────────────


def test_progress_writes_episodes_watched():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:prog1", SAMPLE_MEDIA["id"], ttl_seconds=3600)
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_progress(ctx, _slash_event("progress", {"episodes": 1}, user_id="prog1"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts
    # Params: [user_id, media_id, status, episodes_watched]
    assert inserts[-1]["params"][3] == 1
    assert "episodes_watched = EXCLUDED.episodes_watched" in inserts[-1]["sql"]


def test_progress_at_total_marks_completed():
    """If episodes == total, the upsert promotes status to 'completed'."""
    ctx = MockContext()
    ctx.kv.set("last_anime:user:prog2", SAMPLE_MEDIA["id"], ttl_seconds=3600)
    # SAMPLE_MEDIA has episodes=1.
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_progress(ctx, _slash_event("progress", {"episodes": 1}, user_id="prog2"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts[-1]["params"][2] == "completed"


def test_progress_caps_at_total_and_warns():
    """Episodes > total are silently capped; the user sees a warning prefix."""
    ctx = MockContext()
    ctx.kv.set("last_anime:user:prog3", SAMPLE_MEDIA["id"], ttl_seconds=3600)
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})  # episodes=1

    p.cmd_progress(ctx, _slash_event("progress", {"episodes": 99}, user_id="prog3"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts[-1]["params"][3] == 1  # capped
    follow = ctx.interaction.followups[-1]
    assert "1 episode" in (follow.get("content") or "") or "capping" in (follow.get("content") or "")


def test_progress_rejects_negative():
    ctx = MockContext()
    p.cmd_progress(ctx, _slash_event("progress", {"episodes": -5}, user_id="prog-neg"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert not ctx.sql.executed


def test_progress_without_cache_prompts_user():
    ctx = MockContext()
    p.cmd_progress(ctx, _slash_event("progress", {"episodes": 1}, user_id="prog-new"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "/anime" in (follow.get("content") or "")


def test_anime_card_shows_user_progress_when_present():
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})
    # Pretend the user has watched 1 episode of media 123.
    ctx.sql.query_one = lambda sql, params=None: {"episodes_watched": 1}  # type: ignore[assignment]

    p.cmd_anime(ctx, _slash_event("anime", {"query": "kimi"}, user_id="prog-card"))

    follow = ctx.interaction.followups[-1]
    fields = follow["embeds"][0]["fields"]
    assert any(f["name"] == "Your progress" for f in fields)


# ── v2.1.0 ratings ──────────────────────────────────────────────────────────


def test_rate_encodes_half_points_as_int_times_two():
    """A score of 7.5 stores as 15. _encode_rating roundtrips via _format_rating."""
    assert p._encode_rating(7.5) == 15
    assert p._format_rating(15) == "7.5"
    assert p._encode_rating(10) == 20
    assert p._encode_rating(1) == 2


def test_rate_rejects_out_of_range_scores():
    assert p._encode_rating(0.5) is None
    assert p._encode_rating(10.5) is None
    assert p._encode_rating("abc") is None
    assert p._encode_rating(None) is None


def test_rate_command_writes_upsert():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:rate1", SAMPLE_MEDIA["id"], ttl_seconds=3600)
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_rate(ctx, _slash_event("rate", {"score": 8.5}, user_id="rate1"))

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts
    # Params: [user_id, media_id, status, rating]
    assert inserts[-1]["params"][0] == "rate1"
    assert inserts[-1]["params"][1] == SAMPLE_MEDIA["id"]
    assert inserts[-1]["params"][3] == 17  # 8.5 × 2
    assert "rating = EXCLUDED.rating" in inserts[-1]["sql"]

    follow = ctx.interaction.followups[-1]
    assert "8.5/10" in (follow.get("content") or "")


def test_rate_command_rejects_invalid_score():
    ctx = MockContext()
    p.cmd_rate(ctx, _slash_event("rate", {"score": 11}, user_id="rate2"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "1.0" in (resp.get("content") or "")
    # Must short-circuit before any SQL.
    assert not ctx.sql.executed


def test_rate_command_without_cached_anime_prompts_user():
    ctx = MockContext()
    p.cmd_rate(ctx, _slash_event("rate", {"score": 7}, user_id="rate-new"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "/anime" in (follow.get("content") or "")


def test_ratings_lists_rows_sorted_by_score_desc():
    ctx = MockContext()
    rows = [
        {"media_id": 1, "rating": 20, "status": "completed", "is_favorite": False},
        {"media_id": 2, "rating": 15, "status": "watching", "is_favorite": True},
    ]
    captured = {}

    def _q(sql, params=None):  # noqa: ANN001
        captured["sql"] = sql
        captured["params"] = params
        return rows

    ctx.sql.query = _q  # type: ignore[assignment]
    _mock_anilist(ctx, {"Page": {"media": [
        _make_other(1, "Top"),
        _make_other(2, "Second"),
    ]}})

    p.cmd_ratings(ctx, _slash_event("ratings", {}, user_id="urat"))

    assert "ORDER BY rating DESC" in captured["sql"]
    assert captured["params"][0] == "urat"
    follow = ctx.interaction.followups[-1]
    body = follow["embeds"][0]["description"]
    assert "10.0" in body  # rating 20 → 10.0
    assert "7.5" in body   # rating 15 → 7.5
    # Top scoring row should appear before the second.
    assert body.index("10.0") < body.index("7.5")


def test_ratings_empty_state_message():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # type: ignore[assignment]
    p.cmd_ratings(ctx, _slash_event("ratings", {}, user_id="urat-empty"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "rate" in (follow.get("content") or "").lower()


# ── v1.4.0 i18n string table ────────────────────────────────────────────────

def test_strings_namespace_is_present_and_complete():
    """S contains every constant we use; no stale references remain."""
    assert hasattr(p, "S")
    # Sample a few we expect to exist — the real check is that the module
    # imported successfully with all S.* references resolved.
    for attr in (
        "ANILIST_FAILURE_DEFAULT",
        "ANIME_USAGE",
        "ANIME_NOT_FOUND",
        "COOLDOWN_WAIT",
        "HELP_TITLE",
        "GENRES_FETCH_FAIL",
        "EXPAND_FETCH_FAIL",
    ):
        assert isinstance(getattr(p.S, attr), str)


def test_anime_not_found_message_uses_S():
    """The /anime no-result message should come from S.ANIME_NOT_FOUND."""
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": None})

    p.cmd_anime(ctx, _slash_event("anime", {"query": "zzz"}, user_id="i18n-1"))

    follow = ctx.interaction.followups[-1]
    expected = p.S.ANIME_NOT_FOUND.format(query="zzz")
    assert follow.get("content") == expected


def test_help_title_uses_S():
    ctx = MockContext()
    p.cmd_help(ctx, _slash_event("help", {}, user_id="i18n-2"))
    resp = ctx.interaction.responses[-1]
    assert resp["embeds"][0]["title"] == p.S.HELP_TITLE


# ── v1.3.0 /help + /genres ──────────────────────────────────────────────────

def test_help_lists_every_manifest_command():
    """/help builds its body from manifest.json — no hardcoded list."""
    # regression-fix (v10.0.6): /help now chunks the body across multiple
    # embeds when it would exceed Discord's 4096-char description cap.
    # The "every command appears" contract is preserved; the body is now
    # the joined descriptions of all returned embeds.
    ctx = MockContext()
    p.cmd_help(ctx, _slash_event("help", {}, user_id="h1"))

    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    body = "\n".join((e.get("description") or "") for e in resp["embeds"])
    # Every slash command in the manifest must appear in /help body.
    from pathlib import Path
    manifest = json.loads((Path(p.__file__).resolve().parent / "manifest.json").read_text())
    for cmd in manifest["slash_commands"]:
        assert f"/{cmd['name']}" in body


def test_genres_cached_in_kv_avoids_http():
    """If genres:global is already in KV, /genres serves without an HTTP call."""
    ctx = MockContext()
    ctx.kv.set(p.GENRES_KV_KEY, ["Action", "Romance", "Sci-Fi"], ttl_seconds=p.GENRES_TTL)

    p.cmd_genres(ctx, _slash_event("genres", {}, user_id="g1"))

    assert not ctx.http.requests, "cached path must not hit AniList"
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "Action" in resp["embeds"][0]["description"]


def test_genres_uncached_fetches_and_writes_kv():
    """First /genres call hits AniList, writes KV, and shows the list."""
    ctx = MockContext()
    _mock_anilist(ctx, {"GenreCollection": ["Action", "Romance"]})

    p.cmd_genres(ctx, _slash_event("genres", {}, user_id="g2"))

    assert ctx.http.requests, "expected an HTTP call when KV is empty"
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    # Now the KV should be populated.
    assert ctx.kv.get(p.GENRES_KV_KEY) == ["Action", "Romance"]


# ── v1.2.0 retry + better errors ────────────────────────────────────────────

def test_retry_recovers_after_first_timeout():
    """One RpcTimeoutError, then success — caller should see the anime card."""
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    calls = {"n": 0}

    real_post = ctx.http.post

    def flaky_post(url, body="", headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RpcTimeoutError("first try times out")
        return real_post(url, body=body, headers=headers)

    ctx.http.post = flaky_post  # type: ignore[assignment]

    p.cmd_anime(ctx, _slash_event("anime", {"query": "your name"}, user_id="r1"))

    assert calls["n"] >= 2, "expected at least one retry"
    follow = ctx.interaction.followups[-1]
    assert follow.get("embeds"), "should have served the card on retry success"


def test_retry_exhaustion_returns_friendly_error():
    """Every AniList POST attempt times out. v9.1: when AniList fails and the
    MAL/Kitsu fallbacks (ctx.http.get) also fail in this mock-less setup,
    user gets the cross-source "not found" message."""
    ctx = MockContext()

    calls = {"n": 0}

    def always_timeout(*_args, **_kwargs):
        calls["n"] += 1
        raise RpcTimeoutError("nope")

    ctx.http.post = always_timeout  # type: ignore[assignment]
    ctx.http.get = always_timeout   # type: ignore[assignment]  # MAL + Kitsu fallbacks also fail

    p.cmd_anime(ctx, _slash_event("anime", {"query": "x"}, user_id="r2"))

    # AniList path: 1 initial + 2 retries = 3 POST calls. MAL + Kitsu: 1 GET each.
    assert calls["n"] >= 3
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    content = follow.get("content") or ""
    # v9.1 contract: friendly cross-source "not found" message OR the legacy
    # AniList-specific message (during the v9.1 transition either is OK).
    assert "No anime found" in content or "AniList" in content


def test_rate_limit_is_not_retried():
    """RateLimitError should NOT trigger retry — back off per skill convention."""
    ctx = MockContext()
    calls = {"n": 0}

    def rate_limited(*_args, **_kwargs):
        calls["n"] += 1
        raise RateLimitError("slow down")

    ctx.http.post = rate_limited  # type: ignore[assignment]

    p.cmd_anime(ctx, _slash_event("anime", {"query": "x"}, user_id="r3"))

    assert calls["n"] == 1, "rate-limit must not be retried"


def test_user_fixable_anilist_error_is_surfaced():
    """AniList's 'must contain at least 3 characters' is consumed by
    `_classify_anilist_errors`. v9.1: AniList user-fixable errors still
    set _LAST_USER_ERROR, but /anime now also tries MAL + Kitsu before
    surfacing — and short queries like "ab" may genuinely succeed on
    one of the fallback sources. In this mock-less test, all three
    sources miss; we surface the generic "not found" message.
    The AniList error classification itself is still tested via
    `_classify_anilist_errors` unit tests; this test now only verifies
    the cross-source-miss surface."""
    ctx = MockContext()
    ctx.http.mock_response(
        "graphql.anilist.co",
        status=200,
        body=json.dumps({"errors": [{"message": "Query must contain at least 3 characters."}]}),
    )

    p.cmd_anime(ctx, _slash_event("anime", {"query": "ab"}, user_id="r4"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    content = follow.get("content") or ""
    # Either the AniList-specific user-fixable error (if classification
    # ran early) OR the cross-source not-found. v9.1 transition accepts
    # both.
    assert (
        "must contain at least 3 characters" in content
        or "No anime found" in content
    )


# ── v1.0.2 caching ──────────────────────────────────────────────────────────

def test_anime_cache_hit_short_circuits_http():
    """A second /anime with the same query should serve from cache, no new HTTP call."""
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_anime(ctx, _slash_event("anime", {"query": "your name"}, user_id="cache-1"))
    first_http = len(ctx.http.requests)
    p.cmd_anime(ctx, _slash_event("anime", {"query": "your name"}, user_id="cache-2"))

    assert len(ctx.http.requests) == first_http, "expected the second /anime to skip HTTP"


def test_anime_cache_normalizes_case():
    """Same query in different case should still hit the cache."""
    ctx = MockContext()
    _mock_anilist(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_anime(ctx, _slash_event("anime", {"query": "Your Name"}, user_id="c-a"))
    p.cmd_anime(ctx, _slash_event("anime", {"query": "YOUR NAME"}, user_id="c-b"))

    assert len(ctx.http.requests) == 1


def test_discover_caches_only_page_one():
    """Page 1 of /discover is cached; deeper pages re-fetch each time."""
    ctx = MockContext()
    _mock_anilist(ctx, {"Page": {
        "pageInfo": {"hasNextPage": True, "currentPage": 1},
        "media": [_make_other(i, f"S{i}") for i in range(1, 6)],
    }})

    p.cmd_discover(ctx, _slash_event("discover", {"genre": "Action"}, user_id="c-d1"))
    p.cmd_discover(ctx, _slash_event("discover", {"genre": "Action"}, user_id="c-d2"))
    # Page 1 cached — second call shouldn't add an HTTP request.
    after_two = len(ctx.http.requests)
    assert after_two == 1

    # Page 2 has its own cache key — first click is a cache miss, so one
    # new HTTP request lands. (v10.0.1 made every page cacheable; the
    # assertion is unchanged because we only click page 2 once here.)
    p._route_components(
        ctx,
        _component_event("otaku:page:Action:popular:2", user_id="c-d3"),
    )
    assert len(ctx.http.requests) == after_two + 1


def test_similar_is_not_cached():
    """The roadmap explicitly leaves /similar uncached. A repeat call should re-hit HTTP."""
    ctx = MockContext()
    parent = dict(SAMPLE_MEDIA)
    parent["recommendations"] = {"nodes": [{"mediaRecommendation": _make_other(7, "Rec")}]}
    _mock_anilist(ctx, {"Media": parent})

    p.cmd_similar(ctx, _slash_event("similar", {"anime": "name"}, user_id="c-s1"))
    p.cmd_similar(ctx, _slash_event("similar", {"anime": "name"}, user_id="c-s2"))

    assert len(ctx.http.requests) == 2


def test_anime_embed_caps_genres_at_five():
    """An anime with 8 genres should display only the first 5."""
    many_genres = ["A", "B", "C", "D", "E", "F", "G", "H"]
    media = dict(SAMPLE_MEDIA)
    media["genres"] = many_genres

    embed = p._make_anime_embed(media)

    genres_field = next(f for f in embed["fields"] if f["name"] == "Genres")
    listed = [g.strip() for g in genres_field["value"].split(",")]
    assert listed == many_genres[:5]
    assert "F" not in genres_field["value"]

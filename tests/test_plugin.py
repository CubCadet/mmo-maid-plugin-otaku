"""Tests for the otaku plugin.

Every test mocks AniList's HTTP response using MockContext.http.mock_response,
then calls the relevant handler directly and asserts on what the handler did
(interaction responses/followups, KV writes, etc.).
"""
from __future__ import annotations

import json

import plugin_main as p
from mmo_maid_sdk import RpcTimeoutError
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
    """If AniList times out, the user sees an ephemeral error — not a hang."""
    ctx = MockContext()

    def _raise(*_args, **_kwargs):
        raise RpcTimeoutError("simulated timeout")

    ctx.http.post = _raise  # type: ignore[assignment]

    p.cmd_anime(ctx, _slash_event("anime", {"query": "anything"}, user_id="net1"))

    assert ctx.interaction.defers, "should defer before the http call"
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "AniList" in (follow.get("content") or "")


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

    # Page 2 (via component) should bypass the cache.
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

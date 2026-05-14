"""Regression contract for otaku v1.0.0.

These tests are IMMUTABLE — they describe the user-visible behavior that
shipped at v1.0.0. Later versions must continue to satisfy this file. If a
future version would need to edit a test here to keep passing, that's a
breaking change and should be a MAJOR bump (or the test itself was wrong,
documented with a `# regression-fix:` commit).

Coverage (v1.0.0):
- Slash commands present in manifest: /anime, /discover, /trending, /similar
- Capabilities: interaction:respond, proxy:http, storage:kv
- /anime happy path + no-result error path
- /discover happy path + default-sort path
- /trending uses current-season constants
- /similar with query, with cached id, and with neither
- otaku:similar:<id> button dispatch
- otaku:expand select expand path
- per-user 2-second cooldown
- KV key format: last_anime:user:<id>
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event

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


def _other(mid: int, title: str) -> dict:
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


def _mock(ctx: MockContext, data: dict) -> None:
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": data}))


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


# ── Manifest contract ───────────────────────────────────────────────────────

def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


def test_manifest_declares_v1_0_0_slash_commands():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert {"anime", "discover", "trending", "similar"}.issubset(names)


def test_manifest_declares_v1_0_0_capabilities():
    caps = set(_manifest().get("capabilities_required", []))
    assert {"interaction:respond", "proxy:http", "storage:kv"}.issubset(caps)


def test_manifest_declares_anilist_proxy_domain():
    domains = set(_manifest().get("proxy_domains_requested", []))
    assert "graphql.anilist.co" in domains


# ── /anime ──────────────────────────────────────────────────────────────────

def test_anime_happy_path_embeds_and_caches():
    ctx = MockContext()
    _mock(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_anime(ctx, _slash("anime", {"query": "kimi"}, user_id="reg-u1"))

    assert ctx.interaction.defers
    follow = ctx.interaction.followups[-1]
    assert follow["embeds"][0]["title"].startswith("Kimi no Na wa")
    # KV key shape — frozen at v1.0.0.
    assert ctx.kv.get("last_anime:user:reg-u1") == SAMPLE_MEDIA["id"]


def test_anime_no_result_replies_ephemerally():
    ctx = MockContext()
    _mock(ctx, {"Media": None})

    p.cmd_anime(ctx, _slash("anime", {"query": "zzz"}, user_id="reg-u2"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "No anime found" in (follow.get("content") or "")


# ── /discover ───────────────────────────────────────────────────────────────

def test_discover_returns_paginated_list():
    ctx = MockContext()
    _mock(ctx, {"Page": {
        "pageInfo": {"hasNextPage": True, "currentPage": 1},
        "media": [_other(i, f"Show {i}") for i in range(1, 6)],
    }})

    p.cmd_discover(ctx, _slash("discover", {"genre": "Action", "sort": "popular"}, user_id="reg-u3"))

    follow = ctx.interaction.followups[-1]
    assert "Action" in follow["embeds"][0]["title"]
    assert follow.get("components")


def test_discover_defaults_sort_to_popular():
    ctx = MockContext()
    _mock(ctx, {"Page": {
        "pageInfo": {"hasNextPage": False, "currentPage": 1},
        "media": [_other(1, "Solo")],
    }})

    p.cmd_discover(ctx, _slash("discover", {"genre": "Romance"}, user_id="reg-u4"))

    assert ctx.interaction.followups[-1]["embeds"][0]["title"].endswith("Popular")


# ── /trending ───────────────────────────────────────────────────────────────

def test_trending_uses_current_season():
    ctx = MockContext()
    _mock(ctx, {"Page": {
        "pageInfo": {"hasNextPage": False, "currentPage": 1},
        "media": [_other(i, f"T{i}") for i in range(1, 4)],
    }})

    p.cmd_trending(ctx, _slash("trending", user_id="reg-u5"))

    body = ctx.http.requests[-1]["body"]
    assert any(s in body for s in ("WINTER", "SPRING", "SUMMER", "FALL"))


# ── /similar ────────────────────────────────────────────────────────────────

def test_similar_with_query():
    ctx = MockContext()
    parent = dict(SAMPLE_MEDIA)
    parent["recommendations"] = {"nodes": [{"mediaRecommendation": _other(7, "Rec")}]}
    _mock(ctx, {"Media": parent})

    p.cmd_similar(ctx, _slash("similar", {"anime": "name"}, user_id="reg-u6"))

    assert ctx.kv.get("last_anime:user:reg-u6") == SAMPLE_MEDIA["id"]
    assert ctx.interaction.followups[-1]["embeds"][0]["title"].startswith("🔁 Similar to")


def test_similar_with_no_query_and_no_cache_prompts_user():
    ctx = MockContext()

    p.cmd_similar(ctx, _slash("similar", {}, user_id="reg-u-new"))

    assert not ctx.http.requests
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "/anime" in (resp.get("content") or "")


def test_similar_with_no_query_uses_cached_id():
    ctx = MockContext()
    ctx.kv.set("last_anime:user:reg-u7", 555, ttl_seconds=3600)
    parent = dict(SAMPLE_MEDIA)
    parent["id"] = 555
    parent["recommendations"] = {"nodes": [{"mediaRecommendation": _other(99, "Rec")}]}
    _mock(ctx, {"Media": parent})

    p.cmd_similar(ctx, _slash("similar", {}, user_id="reg-u7"))

    assert ctx.interaction.followups[-1]["embeds"][0]["title"].startswith("🔁 Similar to")


# ── Components ──────────────────────────────────────────────────────────────

def test_similar_button_dispatch_and_caches():
    ctx = MockContext()
    parent = dict(SAMPLE_MEDIA)
    parent["recommendations"] = {"nodes": [{"mediaRecommendation": _other(42, "Rec 42")}]}
    _mock(ctx, {"Media": parent})

    p._route_components(ctx, _component("otaku:similar:123", user_id="reg-u8"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert ctx.kv.get("last_anime:user:reg-u8") == 123


def test_expand_select_loads_full_card():
    ctx = MockContext()
    _mock(ctx, {"Media": SAMPLE_MEDIA})

    event = _component("otaku:expand", user_id="reg-u9")
    event["values"] = [str(SAMPLE_MEDIA["id"])]

    p.comp_expand(ctx, event)

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert follow["embeds"][0]["title"].startswith("Kimi no Na wa")


# ── Cooldown ────────────────────────────────────────────────────────────────

def test_cooldown_blocks_rapid_repeat():
    ctx = MockContext()
    _mock(ctx, {"Media": SAMPLE_MEDIA})

    p.cmd_anime(ctx, _slash("anime", {"query": "x"}, user_id="reg-rapid"))
    p.cmd_anime(ctx, _slash("anime", {"query": "x"}, user_id="reg-rapid"))

    assert len(ctx.interaction.defers) == 1
    assert any(
        "Slow down" in (r.get("content") or "") and r.get("ephemeral") is True
        for r in ctx.interaction.responses
    )

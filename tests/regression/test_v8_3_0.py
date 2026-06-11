"""Regression contract for otaku v8.3.0 — /character-popular (closes Phase 8).

IMMUTABLE — what shipped at v8.3.0:
- New /character-popular slash command (no options) — global leaderboard
  of AniList characters sorted by `favourites` (FAVOURITES_DESC).
- AniList query `QUERY_CHARACTER_POPULAR` — Page(characters: sort:
  FAVOURITES_DESC) with each character's most-popular parent media.
- Pagination prefix `otaku:popchar:<page>` (5 per page, frozen by
  `CHARACTER_POPULAR_PER_PAGE`).
- Render shape: `#NNN **Name** · ❤ N,NNN — [Parent Title](url)`. Rank
  numbers continue across pages (page 2 starts at #6, etc.).
- Public response (not ephemeral) — meant to be a shared leaderboard.
- v8.3.0 closes Phase 8. All four Phase 8 commands shipped (manga
  trio, voice-actor, staff, studio, character-popular = 6 commands total).
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from yourbot_sdk.testing import MockContext, make_event


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


def _slash(name: str, **extra) -> dict:
    return make_event(
        "interaction_create", interaction_type=2,
        command_name=name, options=[], **extra,
    )


def _component(custom_id: str, **extra) -> dict:
    return make_event(
        "interaction_create", interaction_type=3,
        custom_id=custom_id, **extra,
    )


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_character_popular():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "character-popular" in names


def test_character_popular_takes_no_options():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "character-popular")
    assert "options" not in cmd or cmd.get("options") in (None, [])


# ── Constants frozen ───────────────────────────────────────────────────────


def test_per_page_is_five():
    assert p.CHARACTER_POPULAR_PER_PAGE == 5


def test_query_uses_anilist_characters_with_favourites_desc():
    assert "characters(sort: FAVOURITES_DESC)" in p.QUERY_CHARACTER_POPULAR


def test_query_requests_parent_media_anchor():
    """Each character row needs a parent-media link as the most-recognisable
    anchor for readers; the query must fetch the top-popularity parent."""
    assert "media(perPage: 1, sort: POPULARITY_DESC)" in p.QUERY_CHARACTER_POPULAR


# ── /character-popular handler ─────────────────────────────────────────────


def _char_node(name: str, favs: int, mid: int, mtitle: str) -> dict:
    return {
        "id": mid * 10,
        "name": {"full": name, "native": ""},
        "image": {"large": "u"},
        "favourites": favs,
        "siteUrl": f"https://anilist.co/character/{mid * 10}",
        "media": {"nodes": [{
            "id": mid,
            "title": {"romaji": mtitle, "english": mtitle},
            "siteUrl": f"https://anilist.co/anime/{mid}",
        }]},
    }


def test_character_popular_renders_rank_name_favs_parent(monkeypatch):
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Page": {
            "pageInfo": {"hasNextPage": True, "currentPage": 1},
            "characters": [
                _char_node("Lelouch", 80000, 1, "Code Geass"),
                _char_node("Levi", 75000, 2, "Attack on Titan"),
            ],
        }},
    )
    ctx = MockContext()
    p.cmd_character_popular(ctx, _slash("character-popular", user_id="u"))
    follow = ctx.interaction.followups[-1]
    body = follow["embeds"][0]["description"]
    # Rank #1 and #2 should appear, with character names AND parent media.
    assert "#  1" in body or "#1" in body
    assert "Lelouch" in body
    assert "Code Geass" in body
    assert "Levi" in body
    assert "Attack on Titan" in body
    # Favourite count should be present (comma-formatted).
    assert "80,000" in body
    assert "75,000" in body


def test_character_popular_rank_continues_across_pages(monkeypatch):
    """Page 2 must start at #6 (or whatever PER_PAGE + 1 is) so rank numbers
    are globally meaningful, not per-page."""
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Page": {
            "pageInfo": {"hasNextPage": False, "currentPage": 2},
            "characters": [_char_node(f"Char {i}", 1000 - i, i + 100, "X")
                            for i in range(5)],
        }},
    )
    ctx = MockContext()
    p._render_character_popular(ctx, page=2, deferred=True)
    follow = ctx.interaction.followups[-1]
    body = follow["embeds"][0]["description"]
    # Page 2 first row should be rank #6 (PER_PAGE=5 + 1).
    assert "#  6" in body or "#6" in body
    # And the last entry on page 2 should be rank #10.
    assert "# 10" in body or "#10" in body


def test_character_popular_pagination_prefix_is_otaku_popchar(monkeypatch):
    """Prev/next buttons must use `otaku:popchar:<page>` so the dispatcher
    routes them to _render_character_popular and not to /trending or any
    other paginated surface."""
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Page": {
            "pageInfo": {"hasNextPage": True, "currentPage": 1},
            "characters": [_char_node("X", 100, 1, "Y")],
        }},
    )
    ctx = MockContext()
    p.cmd_character_popular(ctx, _slash("character-popular", user_id="u"))
    follow = ctx.interaction.followups[-1]
    serialized = json.dumps(
        follow.get("components") or [],
        default=lambda o: getattr(o, "__dict__", str(o)),
    )
    assert "otaku:popchar:2" in serialized
    # Critical: must NOT use the trending or page prefixes.
    assert "otaku:trend:2" not in serialized
    assert "otaku:page:" not in serialized


def test_character_popular_pagination_component_routes(monkeypatch):
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Page": {
            "pageInfo": {"hasNextPage": False, "currentPage": 3},
            "characters": [_char_node("X", 100, 1, "Y")],
        }},
    )
    ctx = MockContext()
    p._component_dispatch(ctx, _component("otaku:popchar:3", user_id="u"))
    assert ctx.interaction.followups, "popular-character pagination must followup"


def test_character_popular_empty_page_surfaces_error(monkeypatch):
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Page": {
            "pageInfo": {"hasNextPage": False, "currentPage": 9999},
            "characters": [],
        }},
    )
    ctx = MockContext()
    p.cmd_character_popular(ctx, _slash("character-popular", user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert "didn't return any" in (follow.get("content") or "")


def test_character_popular_anilist_failure_surfaces_friendly_error(monkeypatch):
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: None)
    ctx = MockContext()
    p.cmd_character_popular(ctx, _slash("character-popular", user_id="u"))
    follow = ctx.interaction.followups[-1]
    content = follow.get("content") or ""
    assert content, "expected an AniList-failure surfacing message"


def test_character_popular_handles_character_with_no_parent_media(monkeypatch):
    """An AniList character without any `media` nodes should render gracefully
    (rank + name + favourites, no parent suffix)."""
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Page": {
            "pageInfo": {"hasNextPage": False, "currentPage": 1},
            "characters": [{
                "id": 1, "name": {"full": "Orphan", "native": ""},
                "image": {"large": "u"}, "favourites": 5,
                "siteUrl": "https://anilist.co/character/1",
                "media": {"nodes": []},
            }],
        }},
    )
    ctx = MockContext()
    p.cmd_character_popular(ctx, _slash("character-popular", user_id="u"))
    follow = ctx.interaction.followups[-1]
    body = follow["embeds"][0]["description"]
    assert "Orphan" in body
    assert "5" in body  # favourites count


# regression-fix (v10.0.1): the v8.3 doctrine cached only page 1 of the
# popular-characters leaderboard to dodge stale pages 2+. v10.0.1 cached
# every page after the audit identified pagination re-fetches as a hot
# path — the 5-minute TTL bounds staleness and the LRU cap (256 entries)
# bounds memory. The new contract is "every page is cacheable" — the
# original spirit (page 1 stays cache-friendly) is preserved.
def test_character_popular_caches_every_page(monkeypatch):
    """Pagination buttons should hit the in-process cache, not re-fetch."""
    seen: dict = {}

    def _spy(_ctx, query, variables=None, cache=False):
        seen[variables["page"]] = cache
        return {"Page": {"pageInfo": {"hasNextPage": False}, "characters": []}}

    monkeypatch.setattr(p, "_anilist_query", _spy)
    ctx = MockContext()
    p.cmd_character_popular(ctx, _slash("character-popular", user_id="u"))
    p._render_character_popular(ctx, page=2, deferred=True)
    assert seen[1] is True, "page 1 should use the in-process cache"
    assert seen[2] is True, "v10.0.1: page 2+ should also cache"

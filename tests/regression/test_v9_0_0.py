"""Regression contract for otaku v9.0.0 — /find natural-language search.

IMMUTABLE — what shipped at v9.0.0:
- New /find description:<text> slash command.
- Lexical mapping: FIND_PHRASES (inline list of {triggers, genres, tags}
  entries — same allowlist constraint as v6.1's MOODS).
- _match_find_phrases(text) → (genres: set[str], tags: set[str]) unions
  every matched entry's blend. Word-boundary substring match against the
  lowercased, punctuation-stripped input so "art" doesn't match "heart".
- Extracted helper _search_by_genre_tag_blend from v6.1's _mood_query;
  both /mood and /find now share the same with-tags-then-genres-only
  fallback path.
- Empty `description:` → ephemeral usage hint. No-trigger-matched →
  friendly "couldn't decode" pointer to plain trigger words.
- AniList returning no results for the decoded blend → friendly pointer
  to /mood.
- Footer shows what we decoded the input as ("Decoded as: genres: X · tags: Y").
- No new capabilities; reuses proxy:http + interaction:respond.
- No pagination in v9.0 — single-page result. v9.0.x can layer paging
  if user demand surfaces.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


def _slash(name: str, options: dict | None = None, **extra) -> dict:
    return make_event(
        "interaction_create", interaction_type=2,
        command_name=name,
        options=[{"name": k, "value": v} for k, v in (options or {}).items()],
        **extra,
    )


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_find():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "find" in names


def test_find_description_option_required_string():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "find")
    desc = next(o for o in cmd["options"] if o["name"] == "description")
    assert desc["required"] is True and desc["type"] == 3


# ── Shared helper survives v6.1 contract ───────────────────────────────────


def test_search_by_genre_tag_blend_extracted_and_callable():
    """v9.0 extracted this from _mood_query. Both /mood and /find call it."""
    assert callable(p._search_by_genre_tag_blend)


def test_mood_still_routes_through_shared_helper(monkeypatch):
    """v6.1 /mood contract: still uses the with-tags-then-genres fallback.
    A regression that re-inlined the logic into /mood would break /find
    parity; pin the shared call path."""
    calls = []

    def _spy(ctx, genres, tags, page):
        calls.append((tuple(genres), tuple(tags), page))
        return ([{"id": 1, "title": {"romaji": "A", "english": "A"}}], False)

    monkeypatch.setattr(p, "_search_by_genre_tag_blend", _spy)
    ctx = MockContext()
    p.cmd_mood(ctx, _slash("mood", {"feeling": "uplifting"}, user_id="u"))
    assert calls, "cmd_mood must route through _search_by_genre_tag_blend"


# ── FIND_PHRASES table contract ────────────────────────────────────────────


def test_find_phrases_has_minimum_coverage():
    """At least 30 entries — the v9.0 spec said 30-60. Below 30 is suspicious."""
    assert len(p.FIND_PHRASES) >= 30


def test_every_find_phrase_has_at_least_one_trigger():
    for entry in p.FIND_PHRASES:
        assert entry.get("triggers"), f"empty triggers: {entry}"


def test_every_find_phrase_has_at_least_one_genre_or_tag():
    for entry in p.FIND_PHRASES:
        has_genre = bool(entry.get("genres"))
        has_tag = bool(entry.get("tags"))
        assert has_genre or has_tag, f"entry decodes to nothing: {entry}"


# ── _match_find_phrases ────────────────────────────────────────────────────


def test_match_multi_word_input_unions_blends():
    """Compound input like 'slow romance' must union ALL matching entries'
    genres/tags — not pick just one."""
    genres, tags = p._match_find_phrases("slow romance set in school")
    # "slow" → Slice of Life + Iyashikei tag
    assert "Slice of Life" in genres
    assert "Iyashikei" in tags
    # "romance" → Romance genre
    assert "Romance" in genres
    # "school" → School tag
    assert "School" in tags


def test_match_is_case_insensitive():
    g_lower, t_lower = p._match_find_phrases("dark psychological")
    g_upper, t_upper = p._match_find_phrases("DARK Psychological")
    g_mixed, t_mixed = p._match_find_phrases("Dark Psychological")
    assert g_lower == g_upper == g_mixed
    assert t_lower == t_upper == t_mixed


def test_match_word_boundary_protects_against_substring_matches():
    """The classic false-positive case: 'art' is contained in 'heart' but
    must NOT match the 'art' trigger if no such trigger exists. Verifies
    the word-boundary discipline."""
    # No FIND_PHRASES entry has "art" as a trigger, so "heart" must not
    # accidentally match anything via "art" substring.
    genres, tags = p._match_find_phrases("heart")
    # "heartwarming" isn't a single word here, so no match for that either.
    assert not genres and not tags, "bare 'heart' must not match anything"


def test_match_handles_punctuation_strip():
    """Periods/commas around triggers don't block matching."""
    # "slow." with a period must still match "slow" trigger.
    genres, _ = p._match_find_phrases("slow.")
    assert "Slice of Life" in genres


def test_match_empty_input_returns_empty():
    genres, tags = p._match_find_phrases("")
    assert genres == set() and tags == set()


def test_match_unrecognized_returns_empty():
    genres, tags = p._match_find_phrases("xyzzy plover qux")
    assert genres == set() and tags == set()


def test_match_isekai_routes_via_isekai_tag():
    genres, tags = p._match_find_phrases("isekai transported to another world")
    assert "Fantasy" in genres
    assert "Isekai" in tags


# ── /find handler ──────────────────────────────────────────────────────────


def test_find_empty_query_short_circuits_no_anilist_call(monkeypatch):
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return None

    monkeypatch.setattr(p, "_anilist_query", _spy)
    ctx = MockContext()
    p.cmd_find(ctx, _slash("find", {"description": "   "}, user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "Describe what you want" in (resp.get("content") or "")
    assert called["n"] == 0


def test_find_no_trigger_match_surfaces_friendly_pointer(monkeypatch):
    """If the description doesn't trigger any FIND_PHRASES, we shouldn't
    call AniList — we should tell the user which trigger words exist."""
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return None

    monkeypatch.setattr(p, "_anilist_query", _spy)
    ctx = MockContext()
    p.cmd_find(ctx, _slash("find", {"description": "xyzzy plover"}, user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "couldn't decode" in (resp.get("content") or "").lower()
    assert called["n"] == 0


def test_find_happy_path_renders_decoded_blend_in_footer(monkeypatch):
    """The footer must surface what we decoded the input as — so users
    understand why we showed what we showed."""
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Page": {
            "media": [{"id": 1, "title": {"romaji": "X", "english": "X"}}],
            "pageInfo": {"hasNextPage": False},
        }},
    )
    ctx = MockContext()
    p.cmd_find(ctx, _slash("find", {"description": "slow romance"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    footer = (embed.get("footer") or {}).get("text") or ""
    assert "Decoded as" in footer
    # Sorted-stable genres/tags must appear.
    assert "Romance" in footer
    assert "Slice of Life" in footer


def test_find_header_includes_user_query_verbatim(monkeypatch):
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Page": {
            "media": [{"id": 1, "title": {"romaji": "X", "english": "X"}}],
            "pageInfo": {"hasNextPage": False},
        }},
    )
    ctx = MockContext()
    p.cmd_find(ctx, _slash("find", {"description": "dark magic"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    assert "dark magic" in embed.get("title") or ""


def test_find_anilist_empty_surfaces_friendly_no_results(monkeypatch):
    """The decoded blend itself can return no matches (e.g. a niche combo).
    Surface a pointer to /mood as the structured fallback."""
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Page": {
            "media": [],
            "pageInfo": {"hasNextPage": False},
        }},
    )
    ctx = MockContext()
    p.cmd_find(ctx, _slash("find", {"description": "slow romance"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    body = (follow.get("content") or "")
    assert "AniList didn't have anything" in body
    # Pointer to /mood for the curated fallback.
    assert "/mood" in body


def test_find_anilist_failure_surfaces_default_error(monkeypatch):
    """AniList returning None (transport failure) → standard failure surface."""
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: None)
    ctx = MockContext()
    p.cmd_find(ctx, _slash("find", {"description": "epic adventure"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("content"), "expected AniList-failure surfacing message"


def test_find_routes_through_shared_search_helper(monkeypatch):
    """/find must call _search_by_genre_tag_blend (not duplicate the
    with-tags-then-genres fallback logic). Locks the shared-helper contract
    that justified the extraction commit."""
    seen: list = []

    def _spy(ctx, genres, tags, page):
        seen.append((tuple(genres), tuple(tags), page))
        return ([{"id": 1, "title": {"romaji": "A", "english": "A"}}], False)

    monkeypatch.setattr(p, "_search_by_genre_tag_blend", _spy)
    ctx = MockContext()
    p.cmd_find(ctx, _slash("find", {"description": "epic adventure"}, user_id="u"))
    assert seen, "/find must route through _search_by_genre_tag_blend"
    # Genres are sorted for deterministic ordering.
    genres, tags, page = seen[0]
    assert page == 1
    # "epic" → Action + Adventure + Fantasy; "adventure" → Adventure.
    # Union, deduplicated, sorted.
    assert genres == tuple(sorted(set(genres)))

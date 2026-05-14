"""Regression contract for otaku v6.1.0 — /mood mood-based suggestions.

IMMUTABLE — what shipped at v6.1.0:
- /mood feeling:<one of 10 choices> slash command.
- MOODS table is curated and inline (the v1.4 allowlist rule blocks
  sibling .json/.py modules). Every mood has at least one AniList genre;
  some moods also carry an AniList tag enrichment.
- With-tags query falls back to genres-only when AniList returns no
  matches for the tagged variant — a fragile or missing tag should
  never strand the user.
- Pagination uses `otaku:mood:<feeling>:<page>` custom_ids and reuses
  _make_list_embed + _page_buttons + _make_select_row (the existing
  list-style pattern from /discover).
- Unknown feeling values are rejected ephemerally without an AniList call.
- The 10 mood values that ship are frozen here so the manifest stays in
  lockstep with the inline MOODS table.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event

SHIPPED_MOODS = {
    "uplifting",
    "tense",
    "cathartic",
    "chill",
    "epic",
    "nostalgic",
    "dark",
    "funny",
    "romantic",
    "adventurous",
}


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


def _component(custom_id: str, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=3,
        custom_id=custom_id,
        **extra,
    )


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_mood():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "mood" in names


def test_mood_feeling_choices_match_shipped_moods():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "mood")
    feeling = next(o for o in cmd["options"] if o["name"] == "feeling")
    assert feeling["required"] is True
    assert feeling["type"] == 3
    values = {c["value"] for c in feeling.get("choices", [])}
    assert values == SHIPPED_MOODS


# ── MOODS table contract ────────────────────────────────────────────────────


def test_moods_table_matches_shipped_set():
    assert set(p.MOODS.keys()) == SHIPPED_MOODS


def test_every_mood_has_at_least_one_genre():
    for name, m in p.MOODS.items():
        assert m.get("genres"), f"mood {name!r} has no genres"


def test_every_mood_has_a_label():
    for name, m in p.MOODS.items():
        assert m.get("label"), f"mood {name!r} has no label"


# ── Query / handler routing ─────────────────────────────────────────────────


def test_mood_uses_genres_only_query_when_mood_has_no_tags(monkeypatch):
    """A mood with `tags=[]` (e.g. tense, epic) hits QUERY_MOOD_GENRE_ONLY directly."""
    seen = {}

    def _fake_anilist(_ctx, query, variables=None, cache=False):
        seen["query"] = query
        seen["variables"] = variables
        return {"Page": {"media": [{"id": 1, "title": {"romaji": "X", "english": "X"}}],
                          "pageInfo": {"hasNextPage": False}}}

    monkeypatch.setattr(p, "_anilist_query", _fake_anilist)
    ctx = MockContext()
    p.cmd_mood(ctx, _slash("mood", {"feeling": "epic"}, user_id="u"))
    assert seen["query"] == p.QUERY_MOOD_GENRE_ONLY
    # The 'tags' var should not have been sent on this code path.
    assert "tags" not in seen["variables"]


def test_mood_falls_back_to_genre_only_when_tagged_query_empty(monkeypatch):
    """A mood with tags whose tagged AniList query returns 0 media should
    re-fire as genre-only rather than show an empty embed."""
    calls = []

    def _fake_anilist(_ctx, query, variables=None, cache=False):
        calls.append(query)
        if query is p.QUERY_MOOD_WITH_TAGS:
            return {"Page": {"media": [], "pageInfo": {"hasNextPage": False}}}
        # Genres-only retry — return one result.
        return {"Page": {"media": [{"id": 7, "title": {"romaji": "Y", "english": "Y"}}],
                          "pageInfo": {"hasNextPage": False}}}

    monkeypatch.setattr(p, "_anilist_query", _fake_anilist)
    ctx = MockContext()
    p.cmd_mood(ctx, _slash("mood", {"feeling": "cathartic"}, user_id="u"))  # has tags
    assert calls == [p.QUERY_MOOD_WITH_TAGS, p.QUERY_MOOD_GENRE_ONLY]


def test_unknown_mood_rejected_without_anilist_call(monkeypatch):
    called = {"n": 0}

    def _fake_anilist(*a, **kw):
        called["n"] += 1
        return None

    monkeypatch.setattr(p, "_anilist_query", _fake_anilist)
    ctx = MockContext()
    p.cmd_mood(ctx, _slash("mood", {"feeling": "wistful"}, user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "Unknown mood" in (resp.get("content") or "")
    assert called["n"] == 0


# ── Pagination component routing ────────────────────────────────────────────


def test_mood_pagination_custom_id_shape(monkeypatch):
    """The next-page button's custom_id matches otaku:mood:<feeling>:<page>."""
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
    p.cmd_mood(ctx, _slash("mood", {"feeling": "chill"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    # Components include the prev/next row plus an expand select.
    rows = follow.get("components") or []
    # Flatten any component-list shape MockContext serializes to.
    serialized = json.dumps(rows, default=lambda o: getattr(o, "__dict__", str(o)))
    assert "otaku:mood:chill:2" in serialized


def test_mood_page_component_dispatches_to_render(monkeypatch):
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {
            "Page": {
                "media": [{"id": 2, "title": {"romaji": "B", "english": "B"}}],
                "pageInfo": {"hasNextPage": False},
            }
        },
    )
    ctx = MockContext()
    p._component_dispatch(ctx, _component("otaku:mood:dark:3", user_id="u"))
    # The dispatcher defers then followups.
    assert ctx.interaction.followups, "mood pagination should followup"


def test_mood_page_component_rejects_unknown_feeling():
    ctx = MockContext()
    p._component_dispatch(ctx, _component("otaku:mood:wistful:1", user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "malformed" in (resp.get("content") or "").lower()

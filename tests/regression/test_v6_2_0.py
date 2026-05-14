"""Regression contract for otaku v6.2.0 — /genre-trends.

IMMUTABLE — what shipped at v6.2.0:
- /genre-trends slash command (no options, ephemeral, self-only).
- Bridges discovery and personalization: pick the user's top 3 tracked
  genres (sampled from the most-recent STATS_TOP_GENRE_SAMPLE anime,
  same heuristic /stats uses), then ask AniList for currently-trending
  anime in those genres.
- Excludes anime the user already tracks (any status) from the result
  set.
- Constants frozen: GENRE_TRENDS_TOP_N=3, GENRE_TRENDS_FETCH=15,
  GENRE_TRENDS_RESULT_LIMIT=5.
- Empty-tracker short-circuit happens BEFORE any AniList call —
  important for the "you haven't tracked anything yet" empty state.
- _user_top_genres returns alphabetically-stable ties.

regression-fix (v8.0.0): otaku_user_anime renamed to otaku_user_media. The
literal-string assertions in the route mocks below were updated to the new
table name. See CHANGELOG v8.0.0 "Migration notes" for the rationale.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


def _slash(name: str, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name=name,
        options=[],
        **extra,
    )


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_genre_trends():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "genre-trends" in names


def test_genre_trends_takes_no_options():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "genre-trends")
    assert "options" not in cmd or cmd.get("options") in (None, [])


# ── Constants ───────────────────────────────────────────────────────────────


def test_constants_frozen():
    assert p.GENRE_TRENDS_TOP_N == 3
    assert p.GENRE_TRENDS_FETCH == 15
    assert p.GENRE_TRENDS_RESULT_LIMIT == 5


# ── _user_top_genres ────────────────────────────────────────────────────────


def test_user_top_genres_orders_by_count_desc_then_alpha(monkeypatch):
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [{"media_id": 1}, {"media_id": 2}, {"media_id": 3}]
    # AniList returns three anime with genres: Action(3), Drama(2), Comedy(2).
    # Drama vs Comedy tie should break alphabetically (Comedy first).
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {
        "Page": {"media": [
            {"id": 1, "genres": ["Action", "Drama", "Comedy"]},
            {"id": 2, "genres": ["Action", "Drama"]},
            {"id": 3, "genres": ["Action", "Comedy"]},
        ]}
    })
    top = p._user_top_genres(ctx, "u", limit=3)
    assert top == ["Action", "Comedy", "Drama"]


def test_user_top_genres_empty_for_empty_tracker(monkeypatch):
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: None)  # never called
    assert p._user_top_genres(ctx, "u", limit=3) == []


def test_user_top_genres_empty_when_anilist_fails(monkeypatch):
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [{"media_id": 1}]
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: None)
    assert p._user_top_genres(ctx, "u", limit=3) == []


# ── cmd_genre_trends ────────────────────────────────────────────────────────


def test_empty_tracker_short_circuits_before_anilist(monkeypatch):
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [{"n": 0}]  # COUNT returns 0

    anilist_calls = {"n": 0}

    def _spy(*a, **kw):
        anilist_calls["n"] += 1
        return None

    monkeypatch.setattr(p, "_anilist_query", _spy)
    p.cmd_genre_trends(ctx, _slash("genre-trends", user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "haven't tracked" in (follow.get("content") or "")
    assert anilist_calls["n"] == 0


def test_filters_out_tracked_media_from_trending(monkeypatch):
    """A trending anime the user already tracks must not surface."""
    ctx = MockContext()

    # Tracker has 1 anime (id 7) — the count check passes, then top-genres
    # SELECT also returns it, and the tracked-ids SELECT returns {7}.
    def _q(sql, params=None):
        if "COUNT(*)" in sql:
            return [{"n": 1}]
        if "ORDER BY added_at DESC" in sql:
            return [{"media_id": 7}]
        # tracked-ids SELECT
        if "SELECT media_id FROM otaku_user_media WHERE user_id = $1" in sql:
            return [{"media_id": 7}]
        return []

    ctx.sql.query = _q

    # Two AniList calls: one for genre sampling, one for the trending query.
    anilist_responses = iter([
        # Top-genres sampling: anime 7 has genres Action + Drama
        {"Page": {"media": [{"id": 7, "genres": ["Action", "Drama"]}]}},
        # Trending: returns the user's already-tracked id 7, plus a fresh id 8
        {"Page": {"media": [
            {"id": 7, "title": {"romaji": "Tracked", "english": "Tracked"}},
            {"id": 8, "title": {"romaji": "Fresh", "english": "Fresh"}},
        ]}},
    ])
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: next(anilist_responses))

    p.cmd_genre_trends(ctx, _slash("genre-trends", user_id="u"))
    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    body = json.dumps(embed)
    assert "Fresh" in body
    assert "Tracked" not in body  # filtered out


def test_no_fresh_results_after_filter_shows_helpful_pointer(monkeypatch):
    """When every trending result is already tracked, surface a pointer
    to /trending instead of returning an empty embed."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "COUNT(*)" in sql:
            return [{"n": 1}]
        if "ORDER BY added_at DESC" in sql:
            return [{"media_id": 7}]
        if "SELECT media_id FROM otaku_user_media WHERE user_id = $1" in sql:
            return [{"media_id": 7}, {"media_id": 8}]
        return []

    ctx.sql.query = _q
    anilist_responses = iter([
        {"Page": {"media": [{"id": 7, "genres": ["Drama"]}]}},
        {"Page": {"media": [{"id": 8, "title": {"romaji": "T", "english": "T"}}]}},
    ])
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: next(anilist_responses))

    p.cmd_genre_trends(ctx, _slash("genre-trends", user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert "/trending" in (follow.get("content") or "")


def test_query_includes_only_top_n_genres(monkeypatch):
    """The trending query must include exactly GENRE_TRENDS_TOP_N genres
    in genre_in even when the user has more distinct genres."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "COUNT(*)" in sql:
            return [{"n": 1}]
        if "ORDER BY added_at DESC" in sql:
            return [{"media_id": 1}]
        return []

    ctx.sql.query = _q

    captured = {}
    call = {"n": 0}

    def _spy(_ctx, query, variables=None, cache=False):
        call["n"] += 1
        if call["n"] == 1:
            # Top-genres sampling: anime has 5 genres
            return {"Page": {"media": [{"id": 1, "genres": [
                "Action", "Adventure", "Comedy", "Drama", "Sci-Fi",
            ]}]}}
        captured["variables"] = variables
        return {"Page": {"media": []}}

    monkeypatch.setattr(p, "_anilist_query", _spy)
    p.cmd_genre_trends(ctx, _slash("genre-trends", user_id="u"))
    assert len(captured["variables"]["genres"]) == p.GENRE_TRENDS_TOP_N
    assert captured["variables"]["perPage"] == p.GENRE_TRENDS_FETCH

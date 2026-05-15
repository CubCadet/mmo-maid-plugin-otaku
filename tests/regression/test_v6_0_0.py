"""Regression contract for otaku v6.0.0 — /recommend collaborative filtering.

IMMUTABLE — what shipped at v6.0.0:
- /recommend slash command (no options, self-only — defers ephemerally).
- Cosine-similarity collaborative filtering across this server's rated rows.
- Vector entries are rating / 2.0 on a 0.5–10.0 scale; unrated rows excluded.
- Peer comparison capped at RECOMMEND_PEER_CAP = 50 (random sample if larger).
- Peer must share ≥ RECOMMEND_MIN_SHARED_TITLES = 3 rated titles to qualify.
- Caller needs ≥ RECOMMEND_MIN_SELF_RATINGS = 3 ratings, otherwise the
  AniList /similar fallback runs (seeded by the caller's top-rated tracked
  anime).
- Top RECOMMEND_RESULT_LIMIT = 5 results displayed.
- Target's already-tracked anime (any status) are excluded from candidates.

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


def test_manifest_includes_recommend():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "recommend" in names


def test_recommend_takes_no_options():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "recommend")
    # No options at all — keeps the contract dead-simple. v6.1 will add /mood
    # as a separate command rather than reshaping this one.
    assert "options" not in cmd or cmd.get("options") in (None, [])


# ── Constants frozen ────────────────────────────────────────────────────────


def test_recommend_constants_frozen():
    assert p.RECOMMEND_PEER_CAP == 50
    assert p.RECOMMEND_MIN_SELF_RATINGS == 3
    assert p.RECOMMEND_MIN_SHARED_TITLES == 3
    assert p.RECOMMEND_RESULT_LIMIT == 5


# ── _recommend_user_vector ──────────────────────────────────────────────────


def test_user_vector_divides_rating_by_two():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [
        {"media_id": 1, "rating": 10},   # 5.0
        {"media_id": 2, "rating": 15},   # 7.5
        {"media_id": 3, "rating": 20},   # 10.0
    ]
    vec = p._recommend_user_vector(ctx, "u")
    assert vec == {1: 5.0, 2: 7.5, 3: 10.0}


def test_user_vector_skips_null_ratings():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: [
        {"media_id": 1, "rating": 16},
        {"media_id": 2, "rating": None},  # safety net — SQL filters too
    ]
    vec = p._recommend_user_vector(ctx, "u")
    assert vec == {1: 8.0}


def test_user_vector_query_filters_rating_not_null():
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        captured["sql"] = sql
        return []

    ctx.sql.query = _q
    p._recommend_user_vector(ctx, "u")
    assert "rating IS NOT NULL" in captured["sql"]


# ── _cosine_similarity ──────────────────────────────────────────────────────


def test_cosine_zero_when_no_overlap():
    sim, shared = p._cosine_similarity({1: 9.0, 2: 8.0}, {3: 7.0, 4: 6.0})
    assert sim == 0.0
    assert shared == 0


def test_cosine_one_for_identical_vectors():
    v = {1: 8.0, 2: 6.0, 3: 9.0}
    sim, shared = p._cosine_similarity(v, dict(v))
    assert abs(sim - 1.0) < 1e-9
    assert shared == 3


def test_cosine_uses_shared_keys_only_for_dot_but_full_norm():
    # Target & peer share media_id 1. The norms are computed over the full
    # vector (otherwise we'd over-favor users who happen to have rated the
    # exact same anime — which is what we want to find but with similarity
    # tempered by how broadly each user has rated).
    target = {1: 10.0}
    peer = {1: 10.0, 2: 10.0}
    sim, shared = p._cosine_similarity(target, peer)
    # dot = 100, norm_t = 10, norm_p = sqrt(200) ≈ 14.142 → 100 / 141.42 ≈ 0.707
    assert shared == 1
    assert abs(sim - (1 / (2 ** 0.5))) < 1e-9


# ── _recommend_candidates ───────────────────────────────────────────────────


# regression-fix (v10.0.1): _recommend_candidates now batches all peer
# vectors via `WHERE user_id = ANY(%s::TEXT[])` instead of one SELECT per
# peer. The mock shape changed from a per-call iterator returning a flat
# row list to a single response carrying every peer's rows tagged with
# `user_id`. Original assertion (peers_kept + candidate set) is preserved
# — only the SQL emission shape is adapted.
def test_candidates_excludes_target_tracked_ids():
    """Anime the target already has in their tracker (any status) never surface."""
    ctx = MockContext()
    target_vec = {1: 9.0, 2: 8.0, 3: 7.0, 4: 6.0}

    queries: list[str] = []

    def _q(sql, params=None):
        queries.append(sql)
        if (
            "SELECT media_id FROM otaku_user_media WHERE user_id = %s" in sql
            and "rating" not in sql
            and "is_favorite" not in sql
            and "status" not in sql
        ):
            return [{"media_id": 1}, {"media_id": 99}]  # target tracks 1 and 99
        if "SELECT DISTINCT user_id" in sql:
            return [{"user_id": "peer"}]
        # v10.0.1: batched peer vector — rows carry user_id.
        if "user_id = ANY" in sql:
            return [
                {"user_id": "peer", "media_id": 1, "rating": 18},
                {"user_id": "peer", "media_id": 2, "rating": 18},
                {"user_id": "peer", "media_id": 3, "rating": 16},
                {"user_id": "peer", "media_id": 99, "rating": 16},
                {"user_id": "peer", "media_id": 42, "rating": 20},
            ]
        return []

    ctx.sql.query = _q
    candidates, peers_kept = p._recommend_candidates(ctx, "target", target_vec)
    ids = {c["media_id"] for c in candidates}
    assert peers_kept == 1
    assert ids == {42}


# regression-fix (v10.0.1): same batched-peer-vector adaptation as the
# test above. Original assertion (only peer B qualifies; candidate {200})
# is preserved — peer rows now arrive in one response tagged with user_id.
def test_candidates_drop_peers_below_min_shared():
    """Peers sharing fewer than RECOMMEND_MIN_SHARED_TITLES rated titles don't count."""
    ctx = MockContext()
    target_vec = {1: 9.0, 2: 8.0, 3: 7.0}

    def _q(sql, params=None):
        if (
            "SELECT media_id FROM otaku_user_media WHERE user_id = %s" in sql
            and "rating" not in sql
            and "is_favorite" not in sql
            and "status" not in sql
        ):
            return []
        if "SELECT DISTINCT user_id" in sql:
            return [{"user_id": "A"}, {"user_id": "B"}]
        if "user_id = ANY" in sql:
            return [
                # peer A: shares 1 title — dropped
                {"user_id": "A", "media_id": 1, "rating": 18},
                {"user_id": "A", "media_id": 100, "rating": 16},
                # peer B: shares 3 titles — kept
                {"user_id": "B", "media_id": 1, "rating": 18},
                {"user_id": "B", "media_id": 2, "rating": 16},
                {"user_id": "B", "media_id": 3, "rating": 14},
                {"user_id": "B", "media_id": 200, "rating": 20},
            ]
        return []

    ctx.sql.query = _q
    candidates, peers_kept = p._recommend_candidates(ctx, "target", target_vec)
    assert peers_kept == 1
    assert {c["media_id"] for c in candidates} == {200}


# ── Fallback paths via cmd_recommend ────────────────────────────────────────


def test_few_self_ratings_triggers_anilist_fallback(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None):
        if "rating IS NOT NULL" in sql and "DISTINCT" not in sql:
            # target vector: only 2 ratings → below RECOMMEND_MIN_SELF_RATINGS
            return [
                {"media_id": 1, "rating": 18},
                {"media_id": 2, "rating": 16},
            ]
        if "ORDER BY rating DESC" in sql:
            return [{"media_id": 1}]  # seed: highest-rated
        return []

    ctx.sql.query = _q

    captured = {}

    def _fake_anilist(_ctx, query, variables=None, cache=False):
        captured["query"] = query
        captured["variables"] = variables
        return {
            "Media": {
                "id": 1,
                "title": {"romaji": "Source", "english": "Source"},
                "siteUrl": "https://anilist.co/anime/1",
                "recommendations": {
                    "nodes": [
                        {"mediaRecommendation": {
                            "id": 7,
                            "title": {"romaji": "RecOne", "english": "RecOne"},
                            "siteUrl": "https://anilist.co/anime/7",
                        }},
                    ]
                },
            }
        }

    monkeypatch.setattr(p, "_anilist_query", _fake_anilist)

    p.cmd_recommend(ctx, _slash("recommend", user_id="u"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    title = follow["embeds"][0]["title"]
    assert "Source" in title  # fallback header references the seed anime
    # The fallback used QUERY_SIMILAR_BY_ID with the seed media_id.
    assert captured["variables"] == {"id": 1}


def test_no_qualifying_peers_triggers_anilist_fallback(monkeypatch):
    ctx = MockContext()
    # Target has 3 ratings (meets self threshold) but the lone peer doesn't
    # overlap enough — should fall back.
    call_count = {"vector": 0}

    def _q(sql, params=None):
        if "rating IS NOT NULL" in sql and "DISTINCT" not in sql:
            call_count["vector"] += 1
            if call_count["vector"] == 1:
                # target
                return [
                    {"media_id": 1, "rating": 18},
                    {"media_id": 2, "rating": 16},
                    {"media_id": 3, "rating": 14},
                ]
            # peer
            return [
                {"media_id": 99, "rating": 20},
                {"media_id": 100, "rating": 20},
            ]
        if "SELECT DISTINCT user_id" in sql:
            return [{"user_id": "peer"}]
        if "ORDER BY rating DESC" in sql:
            return [{"media_id": 1}]
        # tracked ids; default to empty
        return []

    ctx.sql.query = _q

    def _fake_anilist(_ctx, query, variables=None, cache=False):
        return {
            "Media": {
                "id": 1,
                "title": {"romaji": "Seed", "english": "Seed"},
                "siteUrl": "https://anilist.co/anime/1",
                "recommendations": {
                    "nodes": [
                        {"mediaRecommendation": {
                            "id": 8,
                            "title": {"romaji": "Sim", "english": "Sim"},
                            "siteUrl": "https://anilist.co/anime/8",
                        }},
                    ]
                },
            }
        }

    monkeypatch.setattr(p, "_anilist_query", _fake_anilist)
    p.cmd_recommend(ctx, _slash("recommend", user_id="u"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    desc = follow["embeds"][0]["description"]
    # The "no peers" reason is shown in the fallback embed body.
    assert "Nobody else" in desc or "common with you" in desc


def test_no_tracked_data_replies_with_pointer():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # totally empty user
    p.cmd_recommend(ctx, _slash("recommend", user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    # The empty-state error is rendered via _reply_error → followup with content.
    body = follow.get("content") or (
        follow.get("embeds", [{}])[0].get("description", "") if follow.get("embeds") else ""
    )
    assert "haven't tracked" in body or "/favorite" in body

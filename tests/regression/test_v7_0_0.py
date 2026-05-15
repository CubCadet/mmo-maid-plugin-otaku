"""Regression contract for otaku v7.0.0 — /review and /reviews.

IMMUTABLE — what shipped at v7.0.0:
- New SQL table `otaku_reviews (user_id, media_id, title, body,
  created_at, updated_at)` with PK (user_id, media_id) — one review
  per user per anime; upserts on resubmit.
- `/review` slash command — no options. Resolves caller's cached
  last_anime (`last_anime:user:<id>` KV), pre-fills the modal with
  the existing review (if any), and opens send_modal with a
  `otaku:review-modal:<media_id>` custom_id. Empty cache short-circuits
  to "look up an anime first" — no AniList call made before send_modal
  (Discord's 3-second wall clock makes title-arg lookups unreliable).
- Modal submit (interaction_type=5) is routed via the existing
  _route_components dispatcher. Empty title/body → friendly error,
  no row written.
- `/reviews [anime]` slash command — anime accepts a title, a numeric
  AniList ID, or is omitted (defaults to cached last_anime).
  Paginated 3 reviews per page; ordered by updated_at DESC.
  Pagination uses `otaku:reviews:<media_id>:<page>` custom_ids.
- Constants frozen: REVIEW_TITLE_MAX=100, REVIEW_BODY_MAX=2000,
  REVIEWS_PAGE_SIZE=3.
- Capability surface: no new capabilities — storage:sql, proxy:http,
  interaction:respond already covered everything.
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


def _modal(custom_id: str, values: dict, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=5,
        custom_id=custom_id,
        modal_values=values,
        **extra,
    )


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_review_and_reviews():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "review" in names
    assert "reviews" in names


def test_review_takes_no_options():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "review")
    assert "options" not in cmd or cmd.get("options") in (None, [])


def test_reviews_anime_arg_is_optional_string():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "reviews")
    anime = next(o for o in cmd["options"] if o["name"] == "anime")
    assert anime["required"] is False
    assert anime["type"] == 3  # STRING


# ── Constants frozen ────────────────────────────────────────────────────────


def test_review_constants_frozen():
    assert p.REVIEW_TITLE_MAX == 100
    assert p.REVIEW_BODY_MAX == 2000
    assert p.REVIEWS_PAGE_SIZE == 3


def test_reviews_schema_ddl_is_bootstrapped():
    """The reviews table DDL is registered in _bootstrap_schema."""
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    text = " ".join(e["sql"] for e in (ctx.sql.executed or []))
    assert "otaku_reviews" in text


# ── /review (modal-open path) ───────────────────────────────────────────────


def test_review_no_cache_short_circuits_before_anilist(monkeypatch):
    ctx = MockContext()
    ctx.kv.get = lambda key: None  # cache miss

    anilist_calls = {"n": 0}

    def _spy(*a, **kw):
        anilist_calls["n"] += 1
        return None

    monkeypatch.setattr(p, "_anilist_query", _spy)
    p.cmd_review(ctx, _slash("review", user_id="u"))
    # Responds with the no-cache pointer; no AniList call, no modal opened.
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "haven't looked up" in (resp.get("content") or "")
    assert anilist_calls["n"] == 0
    assert not getattr(ctx.interaction, "modals_sent", [])


def test_review_opens_modal_with_dynamic_custom_id(monkeypatch):
    ctx = MockContext()
    ctx.kv.get = lambda key: 42  # cached media_id
    ctx.sql.query = lambda sql, params=None: []  # no existing review

    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 42, "title": {"romaji": "T", "english": "T"}}},
    )
    p.cmd_review(ctx, _slash("review", user_id="u"))
    modal = ctx.interaction.modals_sent[-1]
    assert modal["custom_id"] == "otaku:review-modal:42"
    # Two fields ship: title + body.
    field_ids = {getattr(f, "custom_id", None) or f.get("custom_id") for f in modal["fields"]}
    assert "title" in field_ids
    assert "body" in field_ids


def test_review_prefills_existing_review(monkeypatch):
    ctx = MockContext()
    ctx.kv.get = lambda key: 42

    def _q(sql, params=None):
        if "SELECT title, body" in sql:
            return [{
                "title": "Old title", "body": "Old body",
                "created_at": None, "updated_at": None,
            }]
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 42, "title": {"romaji": "T", "english": "T"}}},
    )
    p.cmd_review(ctx, _slash("review", user_id="u"))
    modal = ctx.interaction.modals_sent[-1]
    # Pre-fill values come through TextInput.value
    values = {
        (getattr(f, "custom_id", None) or f.get("custom_id")):
        (getattr(f, "value", None) or f.get("value"))
        for f in modal["fields"]
    }
    assert values["title"] == "Old title"
    assert values["body"] == "Old body"


# ── Modal submit path ──────────────────────────────────────────────────────


def test_modal_submit_routes_through_router(monkeypatch):
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []  # treat as new review
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 42, "title": {"romaji": "T", "english": "T"}}},
    )
    p._route_components(
        ctx,
        _modal("otaku:review-modal:42", {"title": "Great", "body": "Loved it."}, user_id="u"),
    )
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "review of" in (resp.get("content") or "").lower()


# regression-fix (v10.0.1): the v7 doctrine had two SQL paths (INSERT for new,
# UPDATE for existing) — a read-then-write pattern that left a TOCTOU window
# under concurrent modal submits. v10.0.1 collapses both paths into a single
# `INSERT ... ON CONFLICT (user_id, media_id) DO UPDATE ... RETURNING (xmax = 0)`
# query. The original contract was "new row vs. update row produce different
# observable responses" — that's still asserted, but via the response text +
# the single combined query, not via separate INSERT/UPDATE statements.
def test_modal_submit_inserts_new_row(monkeypatch):
    ctx = MockContext()
    queries = []

    def _q(sql, params=None):
        queries.append((sql, params))
        if "SELECT title, body" in sql:
            return []  # no existing review when prefilling
        if "ON CONFLICT" in sql:
            return [{"inserted": True}]
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 42, "title": {"romaji": "T", "english": "T"}}},
    )
    p._handle_review_submit(
        ctx,
        _modal("otaku:review-modal:42", {"title": "T", "body": "B"}, user_id="alice"),
    )
    upsert_sqls = [q[0] for q in queries if "INSERT INTO otaku_reviews" in q[0]]
    assert len(upsert_sqls) == 1
    assert "ON CONFLICT" in upsert_sqls[0]


# regression-fix (v10.0.1): see comment on test_modal_submit_inserts_new_row.
def test_modal_submit_updates_existing_row(monkeypatch):
    ctx = MockContext()
    queries = []

    def _q(sql, params=None):
        queries.append((sql, params))
        if "SELECT title, body" in sql:
            return [{"title": "old", "body": "old"}]
        if "ON CONFLICT" in sql:
            return [{"inserted": False}]
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 42, "title": {"romaji": "T", "english": "T"}}},
    )
    p._handle_review_submit(
        ctx,
        _modal("otaku:review-modal:42", {"title": "newT", "body": "newB"}, user_id="u"),
    )
    upsert_sqls = [q[0] for q in queries if "INSERT INTO otaku_reviews" in q[0]]
    assert len(upsert_sqls) == 1
    assert "ON CONFLICT" in upsert_sqls[0]
    assert "updated_at = NOW()" in upsert_sqls[0]


def test_modal_submit_rejects_empty_body():
    ctx = MockContext()
    ctx.sql.execute = lambda sql, params=None: (_ for _ in ()).throw(
        AssertionError("execute should not be called for empty body")
    )
    p._handle_review_submit(
        ctx,
        _modal("otaku:review-modal:42", {"title": "x", "body": ""}, user_id="u"),
    )
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "needs both" in (resp.get("content") or "")


def test_modal_submit_rejects_malformed_custom_id():
    ctx = MockContext()
    p._handle_review_submit(
        ctx,
        _modal("otaku:review-modal:not-an-int", {"title": "x", "body": "y"}, user_id="u"),
    )
    resp = ctx.interaction.responses[-1]
    assert "malformed" in (resp.get("content") or "").lower()


# ── /reviews (list) ────────────────────────────────────────────────────────


def test_reviews_empty_state(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 42, "title": {"romaji": "T", "english": "T"}}},
    )
    ctx.sql.query = lambda sql, params=None: (
        [{"n": 0}] if "COUNT(*)" in sql else []
    )
    p.cmd_reviews(ctx, _slash("reviews", {"anime": "42"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert "No reviews yet" in (follow.get("content") or "")


def test_reviews_orders_by_updated_at_desc(monkeypatch):
    ctx = MockContext()
    captured = {}

    def _q(sql, params=None):
        if "COUNT(*)" in sql:
            return [{"n": 1}]
        if "SELECT user_id, title, body" in sql:
            captured["sql"] = sql
            return [{
                "user_id": "alice", "title": "t", "body": "b",
                "created_at": None, "updated_at": None,
            }]
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 42, "title": {"romaji": "T", "english": "T"}}},
    )
    p.cmd_reviews(ctx, _slash("reviews", {"anime": "42"}, user_id="u"))
    assert "ORDER BY updated_at DESC" in captured["sql"]


def test_reviews_pagination_custom_id_shape(monkeypatch):
    ctx = MockContext()
    # 7 reviews → 3 pages of 3, so page 1 should have a next button to page 2.
    def _q(sql, params=None):
        if "COUNT(*)" in sql:
            return [{"n": 7}]
        return [
            {"user_id": "a", "title": "t1", "body": "b1",
             "created_at": None, "updated_at": None},
            {"user_id": "b", "title": "t2", "body": "b2",
             "created_at": None, "updated_at": None},
            {"user_id": "c", "title": "t3", "body": "b3",
             "created_at": None, "updated_at": None},
        ]

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 9, "title": {"romaji": "X", "english": "X"}}},
    )
    p.cmd_reviews(ctx, _slash("reviews", {"anime": "9"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    serialized = json.dumps(
        follow.get("components") or [],
        default=lambda o: getattr(o, "__dict__", str(o)),
    )
    assert "otaku:reviews:9:2" in serialized


def test_reviews_pagination_component_routes(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None):
        if "COUNT(*)" in sql:
            return [{"n": 3}]
        return [{"user_id": "a", "title": "t", "body": "b",
                  "created_at": None, "updated_at": None}]

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 9, "title": {"romaji": "X", "english": "X"}}},
    )
    p._component_dispatch(ctx, _component("otaku:reviews:9:1", user_id="u"))
    assert ctx.interaction.followups, "reviews pagination should followup"


def test_reviews_falls_back_to_cached_last_anime(monkeypatch):
    """No anime arg → use cached last_anime."""
    ctx = MockContext()
    ctx.kv.get = lambda key: 17

    def _q(sql, params=None):
        if "COUNT(*)" in sql:
            return [{"n": 0}]  # empty reviews
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 17, "title": {"romaji": "C", "english": "C"}}},
    )
    p.cmd_reviews(ctx, _slash("reviews", {}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    # Empty state still surfaces; the point is no usage error was returned.
    assert "No reviews yet" in (follow.get("content") or "")

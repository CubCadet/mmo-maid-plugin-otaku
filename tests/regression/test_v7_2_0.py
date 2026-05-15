"""Regression contract for otaku v7.2.0 — generic server polls.

IMMUTABLE — what shipped at v7.2.0:
- /poll root with three subcommands: `create`, `status`, `end`.
- `create` and `end` are admin-gated via _caller_is_admin; `status` is
  public.
- 2–4 options per poll (POLL_MIN_OPTIONS=2, POLL_MAX_OPTIONS=4),
  keyed `a`/`b`/`c`/`d`.
- Multiple polls can be active concurrently per server (each has a
  unique poll_id). The poll_id is shown in the create embed footer.
- Vote buttons use the prefix `otaku:poll-vote:<poll_id>:<option_key>`.
  Re-voting UPDATEs the row (PK on otaku_poll_votes = (poll_id, user_id)).
- `end` marks status=ended with ended_at=NOW; no winner is computed —
  /poll is a discussion tool, not a competition (use /aotw for "which
  wins").
- Three tables wired into _bootstrap_schema: otaku_polls,
  otaku_poll_options, otaku_poll_votes.
- No new capabilities — storage:sql + discord:read + interaction:respond
  already covered everything.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


def _poll_event(sub: str, sub_options: dict | None = None, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name="poll",
        options=[
            {
                "name": sub,
                "type": 1,
                "options": [
                    {"name": k, "value": v}
                    for k, v in (sub_options or {}).items()
                ],
            }
        ],
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


def test_manifest_includes_poll_with_three_subcommands():
    cmd = next(
        (c for c in _manifest().get("slash_commands", []) if c["name"] == "poll"),
        None,
    )
    assert cmd is not None
    sub_names = {o["name"] for o in cmd.get("options", [])}
    assert sub_names == {"create", "status", "end"}


def test_poll_create_requires_question_a_b():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "poll")
    create = next(o for o in cmd["options"] if o["name"] == "create")
    by_name = {o["name"]: o for o in create["options"]}
    assert by_name["question"]["required"] is True
    assert by_name["a"]["required"] is True
    assert by_name["b"]["required"] is True
    assert by_name["c"]["required"] is False
    assert by_name["d"]["required"] is False


# ── Constants frozen ────────────────────────────────────────────────────────


def test_poll_constants_frozen():
    assert p.POLL_MIN_OPTIONS == 2
    assert p.POLL_MAX_OPTIONS == 4
    assert p.POLL_OPTION_KEYS == ["a", "b", "c", "d"]


def test_poll_schema_ddl_is_bootstrapped():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    text = " ".join(e["sql"] for e in (ctx.sql.executed or []))
    assert "otaku_polls" in text
    assert "otaku_poll_options" in text
    assert "otaku_poll_votes" in text


# ── Admin gating ────────────────────────────────────────────────────────────


def test_poll_create_rejects_non_admin(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: False)
    p.cmd_poll(ctx, _poll_event("create", {"question": "q", "a": "1", "b": "2"}, user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert "Only server admins" in (resp.get("content") or "")


def test_poll_end_rejects_non_admin(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: False)
    p.cmd_poll(ctx, _poll_event("end", {"id": 5}, user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert "Only server admins" in (resp.get("content") or "")


def test_poll_status_is_public(monkeypatch):
    ctx = MockContext()
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return False

    monkeypatch.setattr(p, "_caller_is_admin", _spy)
    ctx.sql.query = lambda sql, params=None: []  # poll not found
    p.cmd_poll(ctx, _poll_event("status", {"id": 1}, user_id="u"))
    assert called["n"] == 0


# ── /poll create ───────────────────────────────────────────────────────────


def test_create_rejects_when_only_one_option(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)
    p.cmd_poll(
        ctx,
        _poll_event("create", {"question": "q", "a": "only one"}, user_id="u"),
    )
    resp = ctx.interaction.responses[-1]
    assert "at least 2" in (resp.get("content") or "")


def test_create_inserts_options_in_order(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)

    def _q(sql, params=None):
        if "WHERE started_by = %s AND question = %s" in sql:
            return [{"poll_id": 42}]
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 42, "started_by": "u", "question": "q",
                     "started_at": None, "ended_at": None, "status": "active"}]
        if "FROM otaku_poll_options" in sql:
            return [{"option_key": "a", "text": "opt-a"},
                    {"option_key": "b", "text": "opt-b"}]
        if "FROM otaku_poll_votes" in sql:
            return []
        return []

    ctx.sql.query = _q
    p.cmd_poll(
        ctx,
        _poll_event("create", {"question": "Best vibe?", "a": "opt-a", "b": "opt-b"}, user_id="u"),
    )

    # regression-fix (v10.0.3, updated v10.0.5): the v7.2 doctrine inserted
    # options one row at a time. v10.0.3 collapsed them into a multi-row
    # VALUES INSERT; v10.0.5 then replaced the f-string VALUES builder with
    # static `UNNEST(%s::TEXT[], %s::TEXT[])` SQL to comply with the SDK's
    # "no f-string SQL" rule. Original intent (both options inserted, keys
    # in order 'a' then 'b') is now expressed as: one INSERT, three params
    # (poll_id scalar + two parallel arrays).
    option_inserts = [e for e in (ctx.sql.executed or [])
                      if "INSERT INTO otaku_poll_options" in e["sql"]]
    assert len(option_inserts) == 1
    params = option_inserts[0]["params"]
    assert len(params) == 3
    poll_id, keys_arr, texts_arr = params
    assert isinstance(poll_id, int)
    assert keys_arr == ["a", "b"]
    assert texts_arr == ["opt-a", "opt-b"]


def test_create_posts_vote_buttons_with_correct_custom_ids(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)

    def _q(sql, params=None):
        if "WHERE started_by = %s AND question = %s" in sql:
            return [{"poll_id": 42}]
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 42, "started_by": "u", "question": "q",
                     "started_at": None, "ended_at": None, "status": "active"}]
        if "FROM otaku_poll_options" in sql:
            return [{"option_key": "a", "text": "opt-a"},
                    {"option_key": "b", "text": "opt-b"}]
        if "FROM otaku_poll_votes" in sql:
            return []
        return []

    ctx.sql.query = _q
    p.cmd_poll(
        ctx,
        _poll_event("create", {"question": "q?", "a": "x", "b": "y"}, user_id="u"),
    )
    follow = ctx.interaction.followups[-1]
    serialized = json.dumps(
        follow.get("components") or [],
        default=lambda o: getattr(o, "__dict__", str(o)),
    )
    assert "otaku:poll-vote:42:a" in serialized
    assert "otaku:poll-vote:42:b" in serialized


# ── Voting ─────────────────────────────────────────────────────────────────


def test_vote_rejected_on_closed_poll(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None):
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "status": "ended"}]
        return []

    ctx.sql.query = _q
    p._component_dispatch(ctx, _component("otaku:poll-vote:5:a", user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert "closed" in (resp.get("content") or "")


def test_vote_rejected_on_invalid_option_key():
    ctx = MockContext()
    p._component_dispatch(ctx, _component("otaku:poll-vote:5:z", user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert "malformed" in (resp.get("content") or "").lower()


def test_vote_inserts_when_first_time(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None):
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "status": "active"}]
        if "FROM otaku_poll_options" in sql:
            return [{"option_key": "a", "text": "x"},
                    {"option_key": "b", "text": "y"}]
        if "FROM otaku_poll_votes" in sql:
            return []
        return []

    ctx.sql.query = _q
    p._component_dispatch(ctx, _component("otaku:poll-vote:5:a", user_id="u"))
    inserts = [e for e in (ctx.sql.executed or [])
               if "INSERT INTO otaku_poll_votes" in e["sql"]]
    assert len(inserts) == 1


def test_vote_updates_when_user_already_voted(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None):
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "status": "active"}]
        if "FROM otaku_poll_options" in sql:
            return [{"option_key": "a", "text": "x"},
                    {"option_key": "b", "text": "y"}]
        if "FROM otaku_poll_votes" in sql:
            return [{"option_key": "b"}]  # prior vote for b
        return []

    ctx.sql.query = _q
    p._component_dispatch(ctx, _component("otaku:poll-vote:5:a", user_id="u"))
    updates = [e for e in (ctx.sql.executed or [])
               if e["sql"].startswith("UPDATE otaku_poll_votes")]
    assert len(updates) == 1


def test_vote_noop_when_same_choice(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None):
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "status": "active"}]
        if "FROM otaku_poll_options" in sql:
            return [{"option_key": "a", "text": "x"},
                    {"option_key": "b", "text": "y"}]
        if "FROM otaku_poll_votes" in sql:
            return [{"option_key": "a"}]
        return []

    ctx.sql.query = _q
    p._component_dispatch(ctx, _component("otaku:poll-vote:5:a", user_id="u"))
    writes = [e for e in (ctx.sql.executed or [])
              if "otaku_poll_votes" in e["sql"]
              and (e["sql"].startswith("INSERT") or e["sql"].startswith("UPDATE"))]
    assert writes == []
    resp = ctx.interaction.responses[-1]
    assert "already voted" in (resp.get("content") or "")


# ── /poll end ──────────────────────────────────────────────────────────────


def test_end_marks_status_ended(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)

    def _q(sql, params=None):
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "status": "active"}]
        if "FROM otaku_poll_votes" in sql:
            return [{"option_key": "a", "n": 3}]
        return []

    ctx.sql.query = _q
    p.cmd_poll(ctx, _poll_event("end", {"id": 5}, user_id="u"))
    update = [e for e in (ctx.sql.executed or [])
              if e["sql"].startswith("UPDATE otaku_polls")]
    assert any("status = 'ended'" in e["sql"] for e in update)


def test_end_handles_already_ended_poll(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)

    def _q(sql, params=None):
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "status": "ended"}]
        return []

    ctx.sql.query = _q
    p.cmd_poll(ctx, _poll_event("end", {"id": 5}, user_id="u"))
    # Should not crash, should not re-UPDATE.
    update = [e for e in (ctx.sql.executed or [])
              if e["sql"].startswith("UPDATE otaku_polls")]
    assert update == []

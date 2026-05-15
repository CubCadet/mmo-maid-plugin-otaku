"""Regression contract for otaku v7.1.0 — anime-of-the-week voting.

IMMUTABLE — what shipped at v7.1.0:
- /aotw root with three subcommands: `start`, `status`, `end`.
- `start` and `end` are admin-gated via _caller_is_admin (Manage Server
  or Administrator), same gating as /otaku-admin.
- Candidates come from otaku_server_watchlist (top
  AOTW_CANDIDATE_LIMIT=5 by added_at DESC). Need ≥2 entries before
  starting.
- One active poll per server enforced in `start` — the second attempt
  is rejected and pointed at /aotw status.
- Vote buttons use the prefix `otaku:aotw-vote:<poll_id>:<media_id>`.
  Clicking another candidate UPDATEs the existing vote rather than
  inserting another row (PK (poll_id, user_id) on otaku_aotw_votes
  enforces this at the SQL level).
- Winner = max votes; tie-break by lowest media_id (deterministic).
- End with no votes marks status=ended without crowning a winner.
- Winner announcement posts to the configured announcement channel
  (falls back to the channel where /aotw end was run); if neither
  succeeds, a friendly ephemeral pointer surfaces.
- Three tables wired into _bootstrap_schema: otaku_aotw_polls,
  otaku_aotw_candidates, otaku_aotw_votes.
- No new capabilities — storage:sql + discord:send_message +
  interaction:respond + discord:read already covered everything.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


def _aotw(sub: str, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name="aotw",
        options=[{"name": sub, "type": 1, "options": []}],
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


def test_manifest_includes_aotw_with_three_subcommands():
    cmd = next(
        (c for c in _manifest().get("slash_commands", []) if c["name"] == "aotw"),
        None,
    )
    assert cmd is not None
    sub_names = {o["name"] for o in cmd.get("options", [])}
    assert sub_names == {"start", "status", "end"}


def test_aotw_subcommand_types_are_subcommand():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "aotw")
    for o in cmd["options"]:
        assert o["type"] == 1  # SUB_COMMAND


# ── Constants frozen ────────────────────────────────────────────────────────


def test_aotw_candidate_limit_is_five():
    assert p.AOTW_CANDIDATE_LIMIT == 5


def test_aotw_schema_ddl_is_bootstrapped():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    text = " ".join(e["sql"] for e in (ctx.sql.executed or []))
    assert "otaku_aotw_polls" in text
    assert "otaku_aotw_candidates" in text
    assert "otaku_aotw_votes" in text


# ── Admin gating ────────────────────────────────────────────────────────────


def test_aotw_start_rejects_non_admin(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: False)
    p.cmd_aotw(ctx, _aotw("start", user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "Only server admins" in (resp.get("content") or "")


def test_aotw_end_rejects_non_admin(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: False)
    p.cmd_aotw(ctx, _aotw("end", user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert "Only server admins" in (resp.get("content") or "")


def test_aotw_status_is_public(monkeypatch):
    """status doesn't pass through _caller_is_admin."""
    ctx = MockContext()
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return False

    monkeypatch.setattr(p, "_caller_is_admin", _spy)
    ctx.sql.query = lambda sql, params=None: []  # no active poll
    p.cmd_aotw(ctx, _aotw("status", user_id="u"))
    assert called["n"] == 0


# ── /aotw start ────────────────────────────────────────────────────────────


def test_start_rejects_when_active_poll_exists(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE status = 'active'" in sql:
            return [{"poll_id": 17}]
        return []

    ctx.sql.query = _q
    p.cmd_aotw(ctx, _aotw("start", user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert "already an active" in (follow.get("content") or "")


def test_start_rejects_when_watchlist_has_fewer_than_two(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE status = 'active'" in sql:
            return []
        if "FROM otaku_server_watchlist" in sql:
            return [{"media_id": 1}]  # only 1 — not enough
        return []

    ctx.sql.query = _q
    p.cmd_aotw(ctx, _aotw("start", user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert "at least 2" in (follow.get("content") or "")


def test_start_inserts_candidates_and_posts_buttons(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Page": {"media": [
            {"id": 1, "title": {"romaji": "A", "english": "A"}},
            {"id": 2, "title": {"romaji": "B", "english": "B"}},
            {"id": 3, "title": {"romaji": "C", "english": "C"}},
        ]}},
    )

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE status = 'active'" in sql:
            return []
        if "FROM otaku_server_watchlist" in sql:
            return [{"media_id": 1}, {"media_id": 2}, {"media_id": 3}]
        if "WHERE started_by = $1 AND status = 'active'" in sql:
            return [{"poll_id": 99}]
        if "FROM otaku_aotw_votes" in sql:
            return []
        return []

    ctx.sql.query = _q
    p.cmd_aotw(ctx, _aotw("start", user_id="u"))

    # regression-fix (v10.0.3): the v7.1 doctrine inserted candidates one
    # row at a time (N INSERTs for N candidates). v10.0.3 collapses them
    # into a single multi-row VALUES (...) INSERT — same rows reach the
    # table, only fewer round-trips. Original intent (all candidates
    # inserted, ordered) is now expressed as: one INSERT statement
    # containing each candidate's params, in order.
    insert_stmts = [e for e in (ctx.sql.executed or [])
                    if e["sql"].startswith("INSERT INTO otaku_aotw_candidates")]
    assert len(insert_stmts) == 1
    params = insert_stmts[0]["params"]
    # 3 candidates × 2 params each = 6 params total.
    assert len(params) == 6
    # Candidate media_ids are at odd indices (params: poll_id, mid, poll_id, mid, ...).
    inserted_mids = [params[2 * i + 1] for i in range(len(params) // 2)]
    assert inserted_mids == [1, 2, 3]

    follow = ctx.interaction.followups[-1]
    serialized = json.dumps(
        follow.get("components") or [],
        default=lambda o: getattr(o, "__dict__", str(o)),
    )
    assert "otaku:aotw-vote:99:1" in serialized
    assert "otaku:aotw-vote:99:2" in serialized


# ── Voting ─────────────────────────────────────────────────────────────────


def test_vote_rejected_on_closed_poll(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE poll_id" in sql:
            return [{"status": "ended", "poll_id": 5}]
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: None)
    p._component_dispatch(ctx, _component("otaku:aotw-vote:5:1", user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert "closed" in (resp.get("content") or "")


def test_vote_inserts_when_first_time(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE poll_id" in sql:
            return [{"status": "active", "poll_id": 5}]
        if "FROM otaku_aotw_candidates" in sql:
            return [{"media_id": 1}, {"media_id": 2}]
        if "FROM otaku_aotw_votes" in sql:
            return []  # no prior vote
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Page": {"media": [
            {"id": 1, "title": {"romaji": "A", "english": "A"}},
        ]}},
    )
    p._component_dispatch(ctx, _component("otaku:aotw-vote:5:1", user_id="u"))

    inserts = [e["sql"] for e in (ctx.sql.executed or [])
               if "INSERT INTO otaku_aotw_votes" in e["sql"]]
    assert len(inserts) == 1


def test_vote_updates_when_user_already_voted(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE poll_id" in sql:
            return [{"status": "active", "poll_id": 5}]
        if "FROM otaku_aotw_candidates" in sql:
            return [{"media_id": 1}, {"media_id": 2}]
        if "FROM otaku_aotw_votes" in sql:
            return [{"media_id": 2}]  # already voted for 2
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Page": {"media": [
            {"id": 1, "title": {"romaji": "A", "english": "A"}},
        ]}},
    )
    p._component_dispatch(ctx, _component("otaku:aotw-vote:5:1", user_id="u"))

    updates = [e["sql"] for e in (ctx.sql.executed or [])
               if e["sql"].startswith("UPDATE otaku_aotw_votes")]
    assert len(updates) == 1


def test_vote_noop_when_same_choice(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE poll_id" in sql:
            return [{"status": "active", "poll_id": 5}]
        if "FROM otaku_aotw_candidates" in sql:
            return [{"media_id": 1}, {"media_id": 2}]
        if "FROM otaku_aotw_votes" in sql:
            return [{"media_id": 1}]  # already voted for 1
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Page": {"media": [
            {"id": 1, "title": {"romaji": "A", "english": "A"}},
        ]}},
    )
    p._component_dispatch(ctx, _component("otaku:aotw-vote:5:1", user_id="u"))
    # No INSERT or UPDATE — just the ephemeral confirmation.
    writes = [e["sql"] for e in (ctx.sql.executed or [])
              if "otaku_aotw_votes" in e["sql"]
              and (e["sql"].startswith("INSERT") or e["sql"].startswith("UPDATE"))]
    assert writes == []


# ── /aotw end ──────────────────────────────────────────────────────────────


def test_end_no_votes_marks_ended_without_winner(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE status = 'active'" in sql:
            return [{"poll_id": 5}]
        if "FROM otaku_aotw_votes" in sql:
            return []
        return []

    ctx.sql.query = _q
    p.cmd_aotw(ctx, _aotw("end", user_id="u", channel_id="c"))

    update = [e for e in (ctx.sql.executed or [])
              if e["sql"].startswith("UPDATE otaku_aotw_polls")]
    assert any("status = 'ended'" in e["sql"] for e in update)
    # No winner_id set when no votes.
    assert not any("winner_id" in e["sql"] for e in update)


def test_end_winner_uses_lowest_media_id_on_tie(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)
    captured = {}

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE status = 'active'" in sql:
            return [{"poll_id": 5}]
        if "FROM otaku_aotw_votes" in sql:
            # media 2 and media 7 both have 3 votes — media 2 should win.
            return [{"media_id": 2, "n": 3}, {"media_id": 7, "n": 3}]
        return []

    def _e(sql, params=None):
        captured.setdefault("execs", []).append((sql, params))

    ctx.sql.query = _q
    ctx.sql.execute = _e
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Page": {"media": [
            {"id": 2, "title": {"romaji": "Winner", "english": "Winner"}},
        ]}},
    )
    # Stub send_message so the test doesn't need a real channel.
    ctx.discord.send_message = lambda **kw: None
    p.cmd_aotw(ctx, _aotw("end", user_id="u", channel_id="c"))

    update_with_winner = [
        (sql, params) for sql, params in captured["execs"]
        if "winner_id = $1" in sql
    ]
    assert len(update_with_winner) == 1
    assert update_with_winner[0][1][0] == 2  # lower media_id wins the tie


def test_end_posts_to_announcement_channel_when_set(monkeypatch):
    ctx = MockContext()
    monkeypatch.setattr(p, "_caller_is_admin", lambda c, u: True)
    monkeypatch.setattr(p, "_resolve_announcement_channel", lambda c: "announce-c")

    def _q(sql, params=None):
        if "FROM otaku_aotw_polls WHERE status = 'active'" in sql:
            return [{"poll_id": 5}]
        if "FROM otaku_aotw_votes" in sql:
            return [{"media_id": 9, "n": 4}]
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(
        p,
        "_anilist_query",
        lambda *a, **kw: {"Page": {"media": [
            {"id": 9, "title": {"romaji": "X", "english": "X"}},
        ]}},
    )
    sent = {}

    def _send(**kw):
        sent.update(kw)

    ctx.discord.send_message = _send
    p.cmd_aotw(ctx, _aotw("end", user_id="u", channel_id="origin-c"))
    assert sent.get("channel_id") == "announce-c"

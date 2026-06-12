"""v10.0.12 regression contracts — full-codebase regression-check fixes.

The 2026-06-12 sixteen-agent regression sweep (post-v10.0.11) confirmed seven
medium findings; this file pins their fixes:

  1. _http_json's truncated gate coerces like _http_status — a host drifting
     `truncated` to the STRING "false" no longer rejects every body (which
     would have recreated the v10.0.11 live incident through the fix itself).
  2. The wide-PK heal is reachable for ALREADY-poisoned tenants: it moved out
     of migration branch (b) — which the schema_v8_migrated fast-path made
     unreachable — into _ensure_wide_pk, called after every migration return
     and once post-completion via the otaku:schema_pk_verified marker. The
     probe COALESCEs to a 'missing' sentinel so the decision never depends on
     NULL-column serialization.
  3. _jikan_query cache hits unwrap the {"data": ...} envelope (hit and miss
     now return the same shape; repeat MAL-fallback searches no longer
     silently degrade to Kitsu within the TTL).
  4. _http_status coercion is pinned at the AniList 200-gate and 5xx retry
     gate (a revert would previously have kept the whole suite green).
  5. Vote handlers defer before their first SQL (3-second rule); replies
     arrive as followups. Parse-failure exits stay instant respond()s.
  6. Kitsu siteUrl reads `type` from the resource top level and `slug` from
     attributes (JSON:API levels were swapped; manga links pointed at /anime/).
  7. Anomaly diagnostics: non-dict parsed bodies log at Jikan/Kitsu; the raw
     body is picked with _http_json's key priority; bytearray heads render;
     the AniList non-200 line carries the coerced status the gate compared.

These contracts are immutable.
"""

from __future__ import annotations

import json

import plugin_main as p
import pytest
from yourbot_sdk import RateLimitError
from yourbot_sdk.testing import MockContext

# ── 1. truncated-flag coercion ──────────────────────────────────────────────


def test_truncated_string_false_is_not_truncated():
    """THE drift-tolerance contract: a stringly-typed falsy truncated flag
    must not reject the body."""
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": "false"}) == {"a": 1}
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": "0"}) == {"a": 1}
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": ""}) == {"a": 1}
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": None}) == {"a": 1}
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": False}) == {"a": 1}


def test_truncated_truthy_shapes_still_fail_closed():
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": True}) is None
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": 1}) is None
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": "true"}) is None
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": "1"}) is None
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": " TRUE "}) is None


def test_truncated_unknown_shapes_fail_closed_not_open():
    """Explicit-falsy allowlist: UNKNOWN truthy drift shapes must count as
    truncated (the enumerated-truthy draft failed open on these)."""
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": 2}) is None
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": "on"}) is None
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": b"true"}) is None
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": ["?"]}) is None
    # …while bytes-typed falsy still passes.
    assert p._http_json({"body_bytes": '{"a": 1}', "truncated": b"false"}) == {"a": 1}


# ── 2. wide-PK heal reachability ────────────────────────────────────────────


def _pk_stub(pk_value):
    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_media_pkey')" in sql:
            return [{"pk": pk_value}]
        return []
    return _q


_REPAIR_MARKERS = (
    "DROP INDEX IF EXISTS otaku_user_anime_user_status_added_idx",
    "ADD COLUMN IF NOT EXISTS media_type",
    "DROP CONSTRAINT IF EXISTS otaku_user_anime_pkey",
    "ADD CONSTRAINT otaku_user_media_pkey",
)


def test_poisoned_tenant_with_migrated_marker_is_healed():
    """The v10.0.11 gap: schema_v8_migrated == "1" returned before the
    branch-(b) probe, so the exact population the fix targeted was never
    healed. The heal now runs after every migration return."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")
    ctx.sql.query = _pk_stub("missing")
    p._bootstrap_schema(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    for marker in _REPAIR_MARKERS:
        assert any(marker in s for s in sqls), f"repair must include: {marker}"


def test_completed_tenant_gets_one_time_pk_probe_and_repair():
    """A tenant that finished bootstrap under v10.0.11 with the PK missing:
    the version-marker fast path runs a ONE-TIME probe gated by
    otaku:schema_pk_verified, repairs, and sets the gate."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_VERSION_KV, p._CURRENT_SCHEMA_VERSION)
    ctx.sql.query = _pk_stub("missing")
    p._bootstrap_schema(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("ADD CONSTRAINT otaku_user_media_pkey" in s for s in sqls)
    assert ctx.kv.get(p._SCHEMA_PK_VERIFIED_KV) == "1"


def test_completed_healthy_tenant_probe_runs_once_then_steady_state_is_silent():
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_VERSION_KV, p._CURRENT_SCHEMA_VERSION)
    ctx.sql.query = _pk_stub("otaku_user_media_pkey")
    p._bootstrap_schema(ctx)
    # One probe, zero DDL, gate set.
    assert ctx.kv.get(p._SCHEMA_PK_VERIFIED_KV) == "1"
    ddl = [c["sql"] for c in ctx.sql.executed if "ALTER" in c["sql"] or "DROP" in c["sql"]]
    assert ddl == []
    # Second boot: fully silent (the v10.0.7 zero-SQL contract, restored).
    ctx.sql.executed.clear()
    p._bootstrap_schema(ctx)
    assert ctx.sql.executed == []


def test_full_bootstrap_completion_sets_pk_verified_gate():
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")
    p._bootstrap_schema(ctx)
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION
    assert ctx.kv.get(p._SCHEMA_PK_VERIFIED_KV) == "1"


def test_ensure_wide_pk_probe_without_pk_key_counts_as_healthy():
    """Back-compat with stubbed SQL that answers every to_regclass query with
    a v7/v8-shaped row: no pk key → healthy → zero DDL."""
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        if "to_regclass(" in sql:
            return [{"v7": None, "v8": "otaku_user_media"}]
        return []

    ctx.sql.query = _q
    p._ensure_wide_pk(ctx)
    assert ctx.sql.executed == []


def test_ensure_wide_pk_swallows_lost_add_constraint_race():
    """Two pool workers can both probe 'missing'; the loser's ADD CONSTRAINT
    fails. A re-probe showing the PK present means the race was lost benignly
    — no exception escapes."""
    ctx = MockContext()
    probes = {"n": 0}

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_media_pkey')" in sql:
            probes["n"] += 1
            # First probe: missing (triggers repair). Re-probe: present.
            return [{"pk": "missing" if probes["n"] == 1 else "otaku_user_media_pkey"}]
        return []

    real_execute = ctx.sql.execute

    def _execute(sql, params=None):
        if "ADD CONSTRAINT otaku_user_media_pkey" in sql:
            raise RuntimeError("RPC error (sql.execute): constraint already exists")
        return real_execute(sql, params)

    ctx.sql.query = _q
    ctx.sql.execute = _execute
    p._ensure_wide_pk(ctx)  # must not raise
    assert probes["n"] == 2


def test_ensure_wide_pk_reraises_genuine_add_constraint_failure():
    ctx = MockContext()
    ctx.sql.query = _pk_stub("missing")  # probe AND re-probe both say missing

    real_execute = ctx.sql.execute

    def _execute(sql, params=None):
        if "ADD CONSTRAINT otaku_user_media_pkey" in sql:
            raise RuntimeError("RPC error (sql.execute): something real")
        return real_execute(sql, params)

    ctx.sql.execute = _execute
    with pytest.raises(RuntimeError):
        p._ensure_wide_pk(ctx)


def test_pk_heal_rate_limit_log_reports_cursor_aware_remaining():
    """RateLimitError out of the heal phase logs the historic warning with a
    cursor-aware remaining count, not a flat 16."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")
    ctx.kv.set(p._SCHEMA_DDL_CURSOR_KV, "5")

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_media_pkey')" in sql:
            raise RateLimitError("DDL rate limit exceeded")
        return []

    ctx.sql.query = _q
    p._bootstrap_schema(ctx)  # must not raise
    entry = next(e for e in ctx.log_entries if "rate-limited" in e["message"])
    assert entry["remaining_ddl"] == str(len(p._SCHEMA_DDL_SEQUENCE) - 5)
    assert "wide-PK heal" in entry["next_ddl"]


def test_fresh_install_heal_skips_missing_table_and_bootstrap_completes():
    """THE fresh-install contract (caught by the v10.0.12 adversarial verify):
    when otaku_user_media does not exist yet, the heal must NOT fire repair
    DDL against the nonexistent table — bootstrap proceeds to CREATE TABLE
    and completes. The pre-fix code crashed every fresh install permanently."""
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_anime')" in sql:
            return [{"v7": None, "v8": None}]  # fresh: neither table exists
        if "to_regclass('otaku_user_media_pkey')" in sql:
            return [{"tbl": "missing", "pk": "missing"}]  # realistic host answer
        return []

    ctx.sql.query = _q
    p._bootstrap_schema(ctx)  # must not raise
    sqls = [c["sql"] for c in ctx.sql.executed]
    # No repair DDL fired against the missing table…
    assert not any("ADD CONSTRAINT otaku_user_media_pkey" in s for s in sqls)
    assert not any("DROP INDEX IF EXISTS otaku_user_anime_user_status_added_idx" in s
                   for s in sqls if "CREATE" not in s)
    # …and the real bootstrap ran to completion.
    assert any("CREATE TABLE IF NOT EXISTS otaku_user_media" in s for s in sqls)
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION


def test_heal_failure_does_not_block_table_creation():
    """A non-RateLimit heal failure mid-bootstrap logs and CONTINUES into the
    DDL loop — it must never prevent CREATE TABLE from running."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_media_pkey')" in sql:
            raise RuntimeError("RPC error (sql.query): something odd")
        return []

    ctx.sql.query = _q
    p._bootstrap_schema(ctx)  # must not raise
    assert "wide-PK heal failed; continuing bootstrap" in ctx.log_lines
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("CREATE TABLE IF NOT EXISTS otaku_user_media" in s for s in sqls)
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION


def test_post_completion_heal_failure_is_logged_not_silent():
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_VERSION_KV, p._CURRENT_SCHEMA_VERSION)

    def _q(sql, params=None, limit=1000):
        raise RuntimeError("RPC error (sql.query): broken")

    ctx.sql.query = _q
    p._bootstrap_schema(ctx)  # must not raise
    assert "wide-PK heal failed; will retry next boot" in ctx.log_lines
    assert ctx.kv.get(p._SCHEMA_PK_VERIFIED_KV) is None  # retried next boot


def test_pk_repair_logs_before_firing_ddl():
    ctx = MockContext()
    ctx.sql.query = _pk_stub("missing")
    p._ensure_wide_pk(ctx)
    assert "wide-PK missing on otaku_user_media; running repair DDL" in ctx.log_lines


# ── 3. jikan cache-hit shape ────────────────────────────────────────────────


def test_jikan_cache_hit_returns_same_shape_as_miss():
    ctx = MockContext()
    ctx.http.mock_response(
        "api.jikan.moe", status=200,
        body=json.dumps({"data": [{"mal_id": 1535, "title": "Death Note"}]}),
    )
    miss = p._jikan_query(ctx, "/anime", {"q": "death note"}, cache=True)
    hit = p._jikan_query(ctx, "/anime", {"q": "death note"}, cache=True)
    assert miss == hit == [{"mal_id": 1535, "title": "Death Note"}]
    # The hit must NOT have re-fetched.
    assert len(ctx.http.requests) == 1


# ── 4. _http_status pinned at the AniList gates ─────────────────────────────


def test_anilist_accepts_string_status_200():
    ctx = MockContext()
    ctx.http._mock_responses["graphql.anilist.co"] = {
        "status": "200",  # drifted: numeric string
        "body_bytes": json.dumps({"data": {"Media": {"id": 1}}}),
        "headers": {}, "truncated": False,
    }
    assert p._anilist_query(ctx, "query { Media }", {}) == {"Media": {"id": 1}}


def test_anilist_string_5xx_still_classified_as_retryable():
    ctx = MockContext()
    ctx.http._mock_responses["graphql.anilist.co"] = {
        "status": "503", "body_bytes": "upstream sad", "headers": {},
    }
    assert p._anilist_query(ctx, "query { Media }", {}) is None
    assert any(e["message"] == "anilist 5xx" for e in ctx.log_entries)


def test_anilist_non_200_log_carries_coerced_status():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=404, body="{}")
    assert p._anilist_query(ctx, "query { Media }", {}) is None
    entry = next(e for e in ctx.log_entries if e["message"] == "anilist non-200")
    assert entry["status_coerced"] == "404"


# ── 5. vote handlers defer before SQL ───────────────────────────────────────


def _component(custom_id: str, user_id: str = "u1") -> dict:
    return {
        "interaction_type": 3, "custom_id": custom_id, "user_id": user_id,
        "guild_id": "g", "channel_id": "c", "interaction_id": "i1",
    }


def test_poll_vote_defers_before_sql_and_replies_via_followup():
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        # The defer must already be recorded when the first SQL runs.
        assert ctx.interaction.defers, "vote handler must defer before SQL"
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "status": "ended"}]
        return []

    ctx.sql.query = _q
    p._component_dispatch(ctx, _component("otaku:poll-vote:5:a"))
    assert ctx.interaction.followups, "post-defer reply must be a followup"
    assert not ctx.interaction.responses


def test_aotw_vote_defers_before_sql_and_replies_via_followup():
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        assert ctx.interaction.defers, "vote handler must defer before SQL"
        if "FROM otaku_aotw_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "status": "ended"}]
        return []

    ctx.sql.query = _q
    p._component_dispatch(ctx, _component("otaku:aotw-vote:5:1"))
    assert ctx.interaction.followups
    assert not ctx.interaction.responses


def test_poll_vote_success_path_sends_recorded_followup():
    """Pins the success reply (the adversarial verify proved deleting the
    final followup passed every test — 'vote recorded + eternal spinner')."""
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        if "FROM otaku_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "status": "active"}]
        if "FROM otaku_poll_options" in sql:
            return [{"option_key": "a", "text": "Option A"}]
        if "FROM otaku_poll_votes" in sql:
            return []  # first-time voter
        return []

    ctx.sql.query = _q
    p._component_dispatch(ctx, _component("otaku:poll-vote:5:a"))
    assert ctx.interaction.followups, "success path must reply"
    assert "Voted" in (ctx.interaction.followups[-1].get("content") or "")
    assert not ctx.interaction.responses


def test_aotw_vote_success_path_sends_recorded_followup(monkeypatch):
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        if "FROM otaku_aotw_polls WHERE poll_id" in sql:
            return [{"poll_id": 5, "status": "active"}]
        if "FROM otaku_aotw_candidates" in sql:
            return [{"media_id": 1}]
        if "FROM otaku_aotw_votes" in sql:
            return []
        return []

    ctx.sql.query = _q
    monkeypatch.setattr(p, "_aotw_resolve_media", lambda *a, **kw: {1: {"id": 1, "title": {"romaji": "A"}}})
    p._component_dispatch(ctx, _component("otaku:aotw-vote:5:1"))
    assert ctx.interaction.followups
    assert "Voted" in (ctx.interaction.followups[-1].get("content") or "")
    assert not ctx.interaction.responses


def test_vote_exception_after_defer_sends_error_followup_not_eternal_spinner():
    """A post-defer failure (missing table mid-bootstrap, duplicate-key race)
    must produce an error followup — never a stranded deferred interaction."""
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        raise RuntimeError("RPC error (sql.query): relation does not exist")

    ctx.sql.query = _q
    p._component_dispatch(ctx, _component("otaku:poll-vote:5:a"))  # must not raise
    assert ctx.interaction.defers
    assert ctx.interaction.followups, "user must get a reply even on failure"
    assert "went wrong" in (ctx.interaction.followups[-1].get("content") or "")
    assert any("poll vote failed" in m for m in ctx.log_lines)


def test_vote_parse_failures_stay_instant_responds():
    """Malformed custom_ids exit before any slow work — they keep respond()
    (no defer), preserving the cheapest path."""
    ctx = MockContext()
    p._component_dispatch(ctx, _component("otaku:poll-vote:notanint:a"))
    assert ctx.interaction.responses and not ctx.interaction.defers


# ── 6. kitsu JSON:API levels ────────────────────────────────────────────────


def test_kitsu_site_url_uses_top_level_type_and_attribute_slug():
    raw = {
        "id": "7442", "type": "manga",
        "attributes": {
            "canonicalTitle": "Attack on Titan",
            "titles": {"en": "Attack on Titan"},
            "slug": "shingeki-no-kyojin",
        },
    }
    out = p._canonicalize_kitsu_media(raw)
    assert out["siteUrl"] == "https://kitsu.io/manga/shingeki-no-kyojin"


def test_kitsu_site_url_falls_back_to_id_without_slug():
    raw = {"id": "1", "type": "anime",
           "attributes": {"canonicalTitle": "X", "titles": {"en": "X"}}}
    out = p._canonicalize_kitsu_media(raw)
    assert out["siteUrl"] == "https://kitsu.io/anime/1"


# ── 7. anomaly diagnostics fidelity ─────────────────────────────────────────


def test_jikan_non_dict_parsed_body_logs_anomaly():
    ctx = MockContext()
    ctx.http.mock_response("api.jikan.moe", status=200, body="[1, 2, 3]")
    assert p._jikan_query(ctx, "/anime", {"q": "x"}) is None
    assert "jikan http body anomaly" in ctx.log_lines


def test_kitsu_non_dict_parsed_body_logs_anomaly():
    ctx = MockContext()
    ctx.http.mock_response("kitsu.io", status=200, body='"just a string"')
    assert p._kitsu_query(ctx, "/anime", {"filter[text]": "x"}) is None
    assert "kitsu http body anomaly" in ctx.log_lines


def test_anomaly_logger_reports_populated_body_when_body_bytes_is_none():
    """key-priority fidelity: a present-but-None body_bytes must not hide a
    populated `body` from the diagnostic line."""
    ctx = MockContext()
    p._log_http_body_anomaly(ctx, "anilist", {"body_bytes": None, "body": "<html>"})
    entry = next(e for e in ctx.log_entries if e["message"] == "anilist http body anomaly")
    assert entry["body_type"] == "str"
    assert "<html>" in entry["head"]


def test_anomaly_logger_handles_bytearray_head():
    ctx = MockContext()
    p._log_http_body_anomaly(ctx, "anilist", {"body_bytes": bytearray(b"\x00\x01abc")})
    entry = next(e for e in ctx.log_entries if e["message"] == "anilist http body anomaly")
    assert entry["body_type"] == "bytearray"
    assert entry["head"] != ""

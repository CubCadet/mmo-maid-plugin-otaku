"""v10.0.13 regression contracts — lean-recheck fixes.

A single-agent deep recheck of the final v10.0.12 code (the post-verify edits
that the 13-agent fleet never re-reviewed) confirmed one medium and one low
defect, plus three test gaps. This file pins the fixes:

  1. MEDIUM: bootstrap completion set otaku:schema_pk_verified even when the
     wide-PK heal FAILED that same boot — permanently gating off the
     post-completion retry backstop (marker-set-while-broken lock-in through
     a new door). Now gated on a heal_ok flag.
  2. LOW: the vote-handler exception guards logged BEFORE the error followup
     inside one try — a log failure stranded the user on the eternal spinner
     the guard exists to prevent. Now: followup first, independent guards,
     exc string truncated.
  3. Fast-path marker-write failure after a SUCCESSFUL heal is no longer
     mislabeled "wide-PK heal failed".
  4. Non-dict cache entries fall through to a TRUE re-fetch instead of
     reporting source failure for the TTL window.
  5. Closed test gaps: aotw guard twin, anomaly-logger present-key fallback,
     steady-state zero-probe pin.

These contracts are immutable.
"""

from __future__ import annotations

import plugin_main as p
from yourbot_sdk.testing import MockContext

# ── 1. pk_verified gated on heal outcome ────────────────────────────────────


def test_failed_heal_on_completing_boot_leaves_pk_verified_unset():
    """THE lock-in contract: when the heal fails on the boot that completes
    the DDL loop, the version marker is set (bootstrap IS complete) but
    pk_verified must stay unset so the post-completion backstop retries."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_media_pkey')" in sql:
            raise RuntimeError("RPC error (sql.query): transient")
        return []

    ctx.sql.query = _q
    p._bootstrap_schema(ctx)  # must not raise
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION
    assert ctx.kv.get(p._SCHEMA_PK_VERIFIED_KV) is None, \
        "failed heal must NOT satisfy the one-time verification gate"


def test_backstop_retries_after_failed_heal_then_heals():
    """TRUE two-boot chain in one MockContext: boot 1's heal fails on the
    completing boot (pk_verified stays unset); boot 2's fast-path backstop
    probes, finds the PK missing, repairs, and only then sets the gate."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")
    state = {"mode": "raise"}

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_media_pkey')" in sql:
            if state["mode"] == "raise":
                raise RuntimeError("RPC error (sql.query): transient")
            return [{"tbl": "otaku_user_media", "pk": "missing"}]
        return []

    ctx.sql.query = _q
    p._bootstrap_schema(ctx)  # boot 1: heal fails, bootstrap completes
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION
    assert ctx.kv.get(p._SCHEMA_PK_VERIFIED_KV) is None

    state["mode"] = "missing"  # the transient failure cleared; PK truly absent
    ctx.sql.executed.clear()
    p._bootstrap_schema(ctx)  # boot 2: backstop fires
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("ADD CONSTRAINT otaku_user_media_pkey" in s for s in sqls)
    assert ctx.kv.get(p._SCHEMA_PK_VERIFIED_KV) == "1"


def test_steady_state_boot_makes_zero_sql_queries():
    """Pins the one-probe-ever gate observably: with both markers set, a boot
    issues zero sql.query calls (the prior contract only counted executes)."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_VERSION_KV, p._CURRENT_SCHEMA_VERSION)
    ctx.kv.set(p._SCHEMA_PK_VERIFIED_KV, "1")
    counter = {"queries": 0}
    real_query = ctx.sql.query

    def _q(sql, params=None, limit=1000):
        counter["queries"] += 1
        return real_query(sql, params)

    ctx.sql.query = _q
    p._bootstrap_schema(ctx)
    assert counter["queries"] == 0
    assert ctx.sql.executed == []


def test_fast_path_marker_write_failure_not_logged_as_heal_failure():
    """A kv.set failure AFTER a successful heal self-corrects next boot and
    must not pollute the pk-heal error channel being watched live."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_VERSION_KV, p._CURRENT_SCHEMA_VERSION)

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_media_pkey')" in sql:
            return [{"tbl": "otaku_user_media", "pk": "otaku_user_media_pkey"}]
        return []

    real_set = ctx.kv.set

    def _set(key, value, ttl_seconds=None):
        if key == p._SCHEMA_PK_VERIFIED_KV:
            raise RuntimeError("RPC error (kv.set): transient")
        return real_set(key, value)

    ctx.sql.query = _q
    ctx.kv.set = _set
    p._bootstrap_schema(ctx)  # must not raise
    assert "wide-PK heal failed; will retry next boot" not in ctx.log_lines


def test_indeterminate_probe_fails_closed_no_ddl_no_marker():
    """THE drift contract (expert-fleet finding): an empty probe rowset or a
    row without the tbl/pk keys is INDETERMINATE — no repair DDL, no
    pk_verified marker, one warning log; the probe retries next boot.
    The fail-open draft let drifted rows lock pk_verified in over a
    genuinely missing PK."""
    # (a) empty rowset (MockSql default / rows-lost drift)
    ctx = MockContext()
    assert p._ensure_wide_pk(ctx) is False
    assert ctx.sql.executed[1:] == []  # only the probe query itself recorded
    assert "wide-PK probe indeterminate; will re-probe next boot" in ctx.log_lines

    # (b) re-keyed columns
    ctx2 = MockContext()

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_media_pkey')" in sql:
            return [{"TBL": "otaku_user_media", "PK": "missing"}]
        return []

    ctx2.sql.query = _q
    assert p._ensure_wide_pk(ctx2) is False
    assert not any("ALTER" in c["sql"] for c in ctx2.sql.executed)


def test_indeterminate_probe_on_completing_boot_leaves_pk_verified_unset():
    """End to end: indeterminate probe during bootstrap → completion sets the
    version marker but NOT pk_verified, so the backstop keeps retrying."""
    ctx = MockContext()  # default [] rows → indeterminate probe
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")
    p._bootstrap_schema(ctx)
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION
    assert ctx.kv.get(p._SCHEMA_PK_VERIFIED_KV) is None


def test_indeterminate_reprobe_after_lost_race_returns_false():
    """A lost ADD CONSTRAINT race is only swallowed when the RE-probe
    positively confirms the PK; an indeterminate re-probe certifies nothing
    and must return False (mutation testing proved this branch was deletable
    without failing the suite)."""
    ctx = MockContext()
    calls = {"n": 0}

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_media_pkey')" in sql:
            calls["n"] += 1
            if calls["n"] == 1:
                return [{"tbl": "otaku_user_media", "pk": "missing"}]
            return []  # re-probe: indeterminate (rows lost / drift)

    real_execute = ctx.sql.execute

    def _execute(sql, params=None):
        if "ADD CONSTRAINT otaku_user_media_pkey" in sql:
            raise RuntimeError("RPC error (sql.execute): constraint already exists")
        return real_execute(sql, params)

    ctx.sql.query = _q
    ctx.sql.execute = _execute
    assert p._ensure_wide_pk(ctx) is False  # must not raise, must not certify


def test_fast_path_indeterminate_probe_leaves_marker_unset():
    """The fast-path caller must not set pk_verified off a non-positive
    return (mutation `or True` on the call passed the suite before this)."""
    ctx = MockContext()  # default [] rows → indeterminate probe
    ctx.kv.set(p._SCHEMA_VERSION_KV, p._CURRENT_SCHEMA_VERSION)
    p._bootstrap_schema(ctx)
    assert ctx.kv.get(p._SCHEMA_PK_VERIFIED_KV) is None
    assert not any(
        "ALTER" in c["sql"] or "ADD CONSTRAINT" in c["sql"] for c in ctx.sql.executed
    )


# ── 2. vote-guard ordering ──────────────────────────────────────────────────


def _component(custom_id: str, user_id: str = "u1") -> dict:
    return {
        "interaction_type": 3, "custom_id": custom_id, "user_id": user_id,
        "guild_id": "g", "channel_id": "c", "interaction_id": "i1",
    }


def test_poll_vote_error_followup_survives_log_failure():
    """Reply-first ordering: even if ctx.log raises, the user gets the error
    followup — never the eternal spinner."""
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        raise RuntimeError("RPC error (sql.query): relation does not exist")

    def _broken_log(message, level="info", tags=None, **extra):
        raise RuntimeError("log transport down")

    ctx.sql.query = _q
    ctx.log = _broken_log
    p._component_dispatch(ctx, _component("otaku:poll-vote:5:a"))  # must not raise
    assert ctx.interaction.followups, "followup must be sent before/despite log failure"
    assert "went wrong" in (ctx.interaction.followups[-1].get("content") or "")


def test_aotw_vote_exception_after_defer_sends_error_followup():
    """The aotw twin of the post-defer guard (was unpinned — deleting the
    aotw guard passed the whole suite)."""
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        raise RuntimeError("RPC error (sql.query): relation does not exist")

    ctx.sql.query = _q
    p._component_dispatch(ctx, _component("otaku:aotw-vote:5:1"))  # must not raise
    assert ctx.interaction.defers
    assert ctx.interaction.followups
    assert "went wrong" in (ctx.interaction.followups[-1].get("content") or "")
    assert any("aotw vote failed" in m for m in ctx.log_lines)


def test_aotw_vote_error_followup_survives_log_failure():
    """Reply-first ordering pinned for the aotw twin too (mutation testing
    proved the poll-only contract let the aotw revert pass green)."""
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        raise RuntimeError("RPC error (sql.query): relation does not exist")

    def _broken_log(message, level="info", tags=None, **extra):
        raise RuntimeError("log transport down")

    ctx.sql.query = _q
    ctx.log = _broken_log
    p._component_dispatch(ctx, _component("otaku:aotw-vote:5:1"))  # must not raise
    assert ctx.interaction.followups
    assert "went wrong" in (ctx.interaction.followups[-1].get("content") or "")


# ── 3. anomaly-logger present-key fallback ──────────────────────────────────


def test_anomaly_logger_all_empty_reports_first_present_key():
    """{"body": ""}: the priority scan skips empty values, so the fallback
    must report the present key's actual (empty-str) value — body_type 'str',
    not 'NoneType'."""
    ctx = MockContext()
    p._log_http_body_anomaly(ctx, "anilist", {"status": 200, "body": ""})
    entry = next(e for e in ctx.log_entries if e["message"] == "anilist http body anomaly")
    assert entry["body_type"] == "str"
    assert entry["body_len"] == "0"


# ── 4. non-dict cache entries are a true miss ───────────────────────────────


def test_jikan_non_dict_cache_entry_refetches():
    ctx = MockContext()
    ctx.http.mock_response(
        "api.jikan.moe", status=200, body='{"data": [{"mal_id": 1}]}'
    )
    # Poison the cache with a non-dict entry under the exact key.
    key = p._cache_key("jikan", "/anime", {"q": "x"})
    p._cache_put(key, ["not", "an", "envelope"])
    data = p._jikan_query(ctx, "/anime", {"q": "x"}, cache=True)
    assert data == [{"mal_id": 1}], "non-dict cache entry must fall through to a re-fetch"
    assert len(ctx.http.requests) == 1


def test_kitsu_non_dict_cache_entry_refetches():
    """The kitsu twin (mutation testing proved the jikan-only contract let
    the kitsu revert pass green)."""
    ctx = MockContext()
    ctx.http.mock_response(
        "kitsu.io", status=200,
        body='{"data": [{"id": "1", "type": "anime"}]}',
    )
    key = p._cache_key("kitsu", "/anime", {"filter[text]": "x"})
    p._cache_put(key, ["not", "an", "envelope"])
    data = p._kitsu_query(ctx, "/anime", {"filter[text]": "x"}, cache=True)
    assert data == [{"id": "1", "type": "anime"}]
    assert len(ctx.http.requests) == 1

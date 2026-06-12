"""v10.0.11 regression contracts — host-drift-tolerant HTTP body extraction.

Live incident (2026-06-11, first smoke test after the v10.0.10 yourbot-sdk
upgrade): every proxy.request returned rpc.ok with an intact `status`, but the
body failed to parse on all three sources — AniList logged "unparseable JSON",
Jikan/Kitsu fell through silently, and users saw "No anime found" for queries
that certainly match. The SDK client's HTTP path is byte-identical between
0.5.4 and 0.6.1, so the body shape drifted host-side (this host's specialty:
$N→%s, options→command_options, the rejected params= kwarg).

The fix is `_http_json(resp)`: a never-raising extractor that accepts the
documented shape plus every plausible drift (alternate key names, bytes,
base64, pre-parsed passthrough), and `_log_http_body_anomaly(...)`: one
bounded diagnostic log line — keys, body type/len, truncated flag, head bytes
— so the next live run identifies any still-unhandled shape exactly.

These contracts are immutable. They pin:
  1. the documented shape still parses (no behavior change on healthy hosts),
  2. each drift shape parses or degrades to None without raising,
  3. the anomaly line fires at all three transports on unusable bodies.
"""

from __future__ import annotations

import base64
import json

import plugin_main as p
from yourbot_sdk.testing import MockContext

# ── _http_json: shape tolerance ─────────────────────────────────────────────


def test_http_json_documented_shape():
    """The SDK-documented shape must keep working unchanged."""
    assert p._http_json({"status": 200, "body_bytes": '{"a": 1}'}) == {"a": 1}


def test_http_json_body_key_fallback():
    assert p._http_json({"status": 200, "body": '{"a": 2}'}) == {"a": 2}


def test_http_json_text_key_fallback():
    assert p._http_json({"status": 200, "text": '{"a": 3}'}) == {"a": 3}


def test_http_json_bytes_body():
    """A bytes-typed body must decode and parse (json.loads accepts bytes, but
    we pin the decode path explicitly for the errors='replace' guarantee)."""
    assert p._http_json({"body_bytes": b'{"a": 4}'}) == {"a": 4}


def test_http_json_preparsed_dict_passthrough():
    """If the host ever pre-parses JSON and ships a dict, pass it through."""
    assert p._http_json({"body": {"data": {"x": 1}}}) == {"data": {"x": 1}}


def test_http_json_preparsed_list_passthrough():
    assert p._http_json({"body_bytes": [1, 2]}) == [1, 2]


def test_http_json_base64_alternate_keys():
    blob = base64.b64encode(b'{"a": 5}').decode()
    assert p._http_json({"body_b64": blob}) == {"a": 5}
    assert p._http_json({"body_base64": blob}) == {"a": 5}


def test_http_json_base64_under_documented_key():
    """Base64 accidentally delivered under body_bytes must still parse: a real
    JSON body never both b64-decodes with validate=True and re-parses."""
    blob = base64.b64encode(b'{"a": 6}').decode()
    assert p._http_json({"body_bytes": blob}) == {"a": 6}


def test_http_json_key_precedence_body_bytes_first():
    """body_bytes wins over body when both are present (documented key first)."""
    assert p._http_json({"body_bytes": '{"k": "bb"}', "body": '{"k": "b"}'}) == {"k": "bb"}


def test_http_json_truncated_body_returns_none():
    """A body cut mid-JSON (host truncation) must yield None, not raise."""
    assert p._http_json({"body_bytes": '{"data": {"Medi', "truncated": True}) is None


def test_http_json_empty_and_missing_shapes():
    assert p._http_json(None) is None
    assert p._http_json({}) is None
    assert p._http_json({"status": 200}) is None
    assert p._http_json({"body_bytes": ""}) is None
    assert p._http_json({"body_bytes": b""}) is None
    assert p._http_json("not a dict") is None


def test_http_json_never_raises_on_garbage():
    assert p._http_json({"body_bytes": 12345}) is None
    assert p._http_json({"body_bytes": "<html>cloudflare</html>"}) is None
    assert p._http_json({"body_b64": "!!!not-base64!!!"}) is None
    assert p._http_json({"body": object()}) is None


# ── anomaly diagnostics at the three transports ─────────────────────────────


def test_anilist_unparseable_body_logs_anomaly():
    """AniList: 200 + non-JSON body keeps the historic 'unparseable JSON' line
    AND adds the new bounded anomaly line with shape diagnostics."""
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200, body="<html>not json</html>")
    assert p._anilist_query(ctx, "query { Media }", {}) is None
    assert "anilist returned unparseable JSON" in ctx.log_lines
    assert "anilist http body anomaly" in ctx.log_lines
    entry = next(e for e in ctx.log_entries if e["message"] == "anilist http body anomaly")
    assert entry["body_type"] == "str"
    assert entry["body_len"] == str(len("<html>not json</html>"))
    assert "body_bytes" in entry["keys"]
    assert len(entry.get("head", "")) <= 170  # repr() of a 160-char slice


def test_anilist_non_dict_json_treated_as_unparseable():
    """A valid-JSON-but-non-dict body (e.g. '123') historically crashed into
    payload.get(); v10.0.11 pins the safe path: log + None."""
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200, body="123")
    assert p._anilist_query(ctx, "query { Media }", {}) is None
    assert "anilist returned unparseable JSON" in ctx.log_lines


def test_jikan_drifted_body_key_still_parses():
    """Jikan: a body under the drifted 'body' key must produce results, not a
    silent None (the live-incident failure mode)."""
    ctx = MockContext()
    # mock_response() always emits the canonical shape, so inject the drifted
    # shape directly the same way mock_response stores it.
    ctx.http._mock_responses["api.jikan.moe"] = {
        "status": 200,
        "body": json.dumps({"data": [{"mal_id": 1535, "title": "Death Note"}]}),
        "headers": {},
        "truncated": False,
    }
    data = p._jikan_query(ctx, "/anime", {"q": "death note", "limit": 1})
    assert data == [{"mal_id": 1535, "title": "Death Note"}]


def test_jikan_unusable_body_logs_anomaly():
    ctx = MockContext()
    ctx.http._mock_responses["api.jikan.moe"] = {"status": 200, "headers": {}, "truncated": True}
    assert p._jikan_query(ctx, "/anime", {"q": "x"}) is None
    assert "jikan http body anomaly" in ctx.log_lines
    entry = next(e for e in ctx.log_entries if e["message"] == "jikan http body anomaly")
    assert entry["truncated"] == "True"
    assert entry["body_type"] == "NoneType"


def test_kitsu_unusable_body_logs_anomaly():
    ctx = MockContext()
    ctx.http._mock_responses["kitsu.io"] = {"status": 200, "body_bytes": '{"data":', "headers": {}}
    assert p._kitsu_query(ctx, "/anime", {"filter[text]": "x"}) is None
    assert "kitsu http body anomaly" in ctx.log_lines


def test_kitsu_base64_drift_still_parses():
    ctx = MockContext()
    blob = base64.b64encode(json.dumps({"data": [{"id": "1", "type": "anime"}]}).encode()).decode()
    ctx.http._mock_responses["kitsu.io"] = {"status": 200, "body_bytes": blob, "headers": {}}
    data = p._kitsu_query(ctx, "/anime", {"filter[text]": "x"})
    assert data == [{"id": "1", "type": "anime"}]


def test_http_json_truncated_but_parseable_body_still_fails():
    """truncated=True means unknown data loss: even a fragment that happens
    to be valid JSON must be rejected (audit finding: the flag was never
    checked anywhere, live or draft)."""
    assert p._http_json({"body_bytes": '{"data": []}', "truncated": True}) is None


# ── _http_status: status coercion ───────────────────────────────────────────


def test_http_status_documented_and_drift_shapes():
    assert p._http_status({"status": 200}) == 200
    assert p._http_status({"status": "200"}) == 200          # numeric string
    assert p._http_status({"status_code": 404}) == 404       # legacy key
    assert p._http_status({"status": None, "status_code": 503}) == 503
    assert p._http_status({"status": "OK"}) == 0              # garbled → fail closed
    assert p._http_status({}) == 0
    assert p._http_status(None) == 0


def test_jikan_non_2xx_now_logged_not_silent():
    """Audit finding: the status gate silently dropped both real upstream
    errors and status-shape drift. v10.0.11 logs a warning either way."""
    ctx = MockContext()
    ctx.http.mock_response("api.jikan.moe", status=500, body="oops")
    assert p._jikan_query(ctx, "/anime", {"q": "x"}) is None
    assert "jikan non-2xx" in ctx.log_lines


def test_kitsu_non_2xx_now_logged_not_silent():
    ctx = MockContext()
    ctx.http.mock_response("kitsu.io", status=403, body="denied")
    assert p._kitsu_query(ctx, "/anime", {"filter[text]": "x"}) is None
    assert "kitsu non-2xx" in ctx.log_lines


# ── bootstrap: resumable DDL cursor ─────────────────────────────────────────


def test_bootstrap_cursor_resumes_skipping_done_ddl():
    """THE live-incident heal contract: with the cursor at 5 (the live
    server's exact state — DDLs #1-#5 done, rate-limited at #6), the next
    boot must NOT re-issue the first five statements."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")  # migration already done
    ctx.kv.set(p._SCHEMA_DDL_CURSOR_KV, "5")
    p._bootstrap_schema(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    ddl_sqls = [s for s in sqls if not s.startswith("SELECT")]
    # First five sequence entries must be absent…
    assert not any("CREATE TABLE IF NOT EXISTS otaku_user_media" in s for s in ddl_sqls)
    assert not any("CREATE INDEX IF NOT EXISTS" in s for s in ddl_sqls)
    assert not any("otaku_server_watchlist" in s for s in ddl_sqls)
    # …and the loop resumed at #6 (watch parties) and ran to the end.
    assert any("otaku_watch_parties" in s for s in ddl_sqls)
    assert any("otaku_achievements" in s for s in ddl_sqls)
    assert len(ddl_sqls) == len(p._SCHEMA_DDL_SEQUENCE) - 5
    # Completed: version marker set.
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION


def test_bootstrap_cursor_persisted_after_each_statement():
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")
    p._bootstrap_schema(ctx)
    assert ctx.kv.get(p._SCHEMA_DDL_CURSOR_KV) == str(len(p._SCHEMA_DDL_SEQUENCE))


def test_bootstrap_rate_limit_saves_progress_and_logs_position():
    """Rate-limit mid-run: cursor must point at the FAILED statement (so the
    next boot retries it), the version marker must stay unset, and the
    historic warning string must carry remaining/next_ddl diagnostics."""
    from yourbot_sdk import RateLimitError

    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")
    real_execute = ctx.sql.execute
    calls = {"n": 0}

    def _execute(sql, params=None):
        calls["n"] += 1
        if calls["n"] > 5:  # the live host's budget: 5 DDL per window
            raise RateLimitError("DDL rate limit exceeded")
        return real_execute(sql, params)

    ctx.sql.execute = _execute
    p._bootstrap_schema(ctx)
    assert ctx.kv.get(p._SCHEMA_DDL_CURSOR_KV) == "5"
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) is None
    assert "schema bootstrap rate-limited; will resume on next worker boot" in ctx.log_lines
    entry = next(e for e in ctx.log_entries if "rate-limited" in e["message"])
    assert entry["remaining_ddl"] == str(len(p._SCHEMA_DDL_SEQUENCE) - 5)
    assert entry["next_ddl"] == p._SCHEMA_DDL_SEQUENCE[5][0]


def test_bootstrap_garbled_cursor_falls_back_to_full_replay():
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")
    ctx.kv.set(p._SCHEMA_DDL_CURSOR_KV, "not-a-number")
    p._bootstrap_schema(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("CREATE TABLE IF NOT EXISTS otaku_user_media" in s for s in sqls)
    assert ctx.kv.get(p._SCHEMA_VERSION_KV) == p._CURRENT_SCHEMA_VERSION


def test_schema_ddl_sequence_matches_known_statement_count():
    """Append-only guard: 16 statements as of v10.0.11. If this grows, the
    addition must be an APPEND (cursor indices are persisted in tenant KV)."""
    assert len(p._SCHEMA_DDL_SEQUENCE) == 16
    assert p._SCHEMA_DDL_SEQUENCE[0][1] is p._SCHEMA_DDL
    assert p._SCHEMA_DDL_SEQUENCE[-1][1] is p._SCHEMA_ACHIEVEMENTS_DDL


# ── migration branch (b): PK verification + repair ──────────────────────────


def test_migration_branch_b_repairs_missing_pk():
    """A v7 upgrade that rate-limited at the final ADD CONSTRAINT left the
    renamed table with NO primary key, and branch (b) then declared the
    migration done forever. The PK-missing state must trigger the finishing
    steps before bootstrap completes.

    # regression-fix (v10.0.12): the v10.0.11 inline branch-(b) repair was
    # unreachable for tenants whose migrated marker was already set, so the
    # heal moved to _ensure_wide_pk, called by _bootstrap_schema after every
    # _migrate_v7_to_v8 return. The probe now COALESCEs to a 'missing'
    # sentinel so the decision doesn't depend on NULL-column serialization.
    # Same user-visible contract (PK-less table gets repaired); the stub and
    # entry point are updated to the new mechanism."""
    ctx = MockContext()
    ctx.kv.set(p._SCHEMA_V8_MIGRATED_KV, "1")  # poisoned tenant: marker set, PK gone

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_media_pkey')" in sql:
            # regression-fix (v10.0.13): tbl key included — the probe is
            # table-aware and fails closed on rows missing expected keys.
            return [{"tbl": "otaku_user_media", "pk": "missing"}]
        return []

    ctx.sql.query = _q
    p._bootstrap_schema(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("DROP INDEX IF EXISTS otaku_user_anime_user_status_added_idx" in s for s in sqls)
    assert any("ADD COLUMN IF NOT EXISTS media_type" in s for s in sqls)
    assert any("DROP CONSTRAINT IF EXISTS otaku_user_anime_pkey" in s for s in sqls)
    assert any("ADD CONSTRAINT otaku_user_media_pkey" in s for s in sqls)


def test_migration_branch_b_healthy_pk_issues_no_ddl():
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        if "to_regclass('otaku_user_anime')" in sql:
            return [{"v7": None, "v8": "otaku_user_media"}]
        if "to_regclass('otaku_user_media_pkey')" in sql:
            return [{"pk": "otaku_user_media_pkey"}]
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)
    # Only the advisory-lock SELECT may run — zero DDL.
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert not any("ALTER TABLE" in s or "DROP" in s or "ADD CONSTRAINT" in s for s in sqls)
    assert ctx.kv.get(p._SCHEMA_V8_MIGRATED_KV) == "1"


def test_migration_branch_b_probe_without_pk_key_counts_as_healthy():
    """Back-compat with the v10.0.8 contracts: a stub answering every
    to_regclass query with the v7/v8 row (no 'pk' key) must not trigger
    repair DDL — only an explicit NULL does."""
    ctx = MockContext()

    def _q(sql, params=None, limit=1000):
        if "to_regclass(" in sql:
            return [{"v7": None, "v8": "otaku_user_media"}]
        return []

    ctx.sql.query = _q
    p._migrate_v7_to_v8(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert not any("ALTER TABLE" in s or "DROP" in s or "ADD CONSTRAINT" in s for s in sqls)
    assert ctx.kv.get(p._SCHEMA_V8_MIGRATED_KV) == "1"


# ── airing cron: dedup claimed only after the subscriber query succeeds ─────


def _airing_http_fixture(ctx):
    ctx.http.mock_response(
        "graphql.anilist.co",
        status=200,
        body=json.dumps({
            "data": {"Page": {"airingSchedules": [
                {"media": {"id": 42, "episodes": 12}, "episode": 3}
            ]}}
        }),
    )


def test_airing_dedup_not_burned_when_sql_fails():
    """The old order claimed the per-episode dedup key, then ran the SQL; a
    failing query (missing table, transient host error) burned the claim and
    silently skipped that episode's pings for 24h. v10.0.11 queries first."""
    import pytest

    ctx = MockContext()
    _airing_http_fixture(ctx)

    def _raising_query(sql, params=None, limit=1000):
        raise RuntimeError("RPC error (sql.query): relation does not exist")

    ctx.sql.query = _raising_query
    with pytest.raises(RuntimeError):
        p._dispatch_airing_announcements(ctx)
    # The dedup key must NOT have been claimed: a retry can still announce.
    assert p._airing_dedup_key(42, 3) not in ctx.ephemeral._seen


def test_airing_dedup_claimed_on_success_path():
    ctx = MockContext()
    _airing_http_fixture(ctx)

    def _q(sql, params=None, limit=1000):
        if "otaku_notifications" in sql:
            return [{"user_id": "u1", "channel_id": "c1"}]
        return []

    ctx.sql.query = _q
    sent = p._dispatch_airing_announcements(ctx)
    assert sent == 1
    assert p._airing_dedup_key(42, 3) in ctx.ephemeral._seen


def test_anomaly_logger_never_raises():
    """The diagnostic helper must swallow every pathological shape."""
    ctx = MockContext()
    p._log_http_body_anomaly(ctx, "anilist", None)
    p._log_http_body_anomaly(ctx, "anilist", {})
    p._log_http_body_anomaly(ctx, "anilist", {1: 2, None: object()})
    p._log_http_body_anomaly(ctx, "anilist", {"body_bytes": object(), "truncated": object()})
    # Every call above must have produced a log entry without raising.
    assert len([m for m in ctx.log_lines if m == "anilist http body anomaly"]) == 4

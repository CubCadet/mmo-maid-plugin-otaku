"""v10.0.16 regression contracts — tune-up patch.

Three fixes, each pinned here:

  (#2) `_rate_acquire` / `_cache_put` are now thread-safe. The SDK runs up to 4
       dispatcher threads per worker; the old unsynchronized read-modify-write on
       the module-global `_RATE_BUCKETS` could overshoot a source's budget and
       could raise IndexError out of an already-deferred handler. A module-level
       lock serializes the bucket mutation; the blocking sleep stays OUTSIDE it.

  (#4) On retry exhaustion `_anilist_query` records the outcome that actually
       exhausted the retries (`non_2xx` for an all-5xx storm) instead of a
       hardcoded `timeout`, so the v10.0.15 per-(source,outcome) dashboard tells
       the truth during an upstream 5xx incident.

  (#6) Every HTTP-transport error/anomaly log now carries
       `request_id=ctx.request_id`, so a burst of upstream failures correlates
       back to the one interaction that caused it.

These contracts are immutable.
"""

from __future__ import annotations

import json
import threading

import plugin_main as p
from yourbot_sdk import RpcTimeoutError
from yourbot_sdk.testing import MockContext


def _outcomes(ctx, source: str) -> list[str]:
    out = []
    for rec in ctx.metrics.recorded:
        if rec["metric"] == "http.request" and rec["tags"].get("source") == source:
            out.append(rec["tags"].get("outcome"))
    return out


# ── #4 — retry-exhaustion outcome reflects the real failure class ────────────


def test_anilist_5xx_exhaustion_records_non_2xx():
    """A persistent 5xx storm exhausts retries and is recorded as non_2xx,
    NOT timeout (the pre-v10.0.16 mislabel that defeated the dashboard)."""
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=503, body="upstream down")
    p._anilist_query(ctx, "query { Media }", {})
    assert _outcomes(ctx, "anilist") == ["non_2xx"]
    assert "timeout" not in _outcomes(ctx, "anilist")


def test_anilist_timeout_exhaustion_still_records_timeout():
    """The #4 fix must not over-reach: an all-timeout exhaustion is still a
    single 'timeout' datapoint (regression guard alongside v10.0.15)."""
    ctx = MockContext()

    def _raise_post(*_a, **_k):
        raise RpcTimeoutError("simulated timeout")

    ctx.http.post = _raise_post  # type: ignore[assignment]
    p._anilist_query(ctx, "query { Media }", {})
    assert _outcomes(ctx, "anilist") == ["timeout"]


def test_anilist_500_exhaustion_emits_single_datapoint():
    """Still exactly one datapoint per transport call, not one per retry."""
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=500, body="boom")
    p._anilist_query(ctx, "query { Media }", {})
    assert len(_outcomes(ctx, "anilist")) == 1


# ── #2 — thread-safe rate buckets + cache eviction ──────────────────────────


def test_state_locks_exist():
    for name in ("_RATE_LOCK", "_CACHE_LOCK"):
        lock = getattr(p, name)
        assert hasattr(lock, "acquire") and hasattr(lock, "release"), name


def test_rate_acquire_concurrent_does_not_overshoot(monkeypatch):
    """Under concurrent callers, exactly `max_n` requests are admitted without
    sleeping — the lock serializes the check-then-append so the budget can't be
    overshot. Without the lock, more than `max_n` could slip through the gate
    before any of them appended."""
    monkeypatch.setattr(p, "_RATE_BUCKETS", {s: [] for s in p.SOURCE_RATE_LIMITS})
    max_n, _window = p.SOURCE_RATE_LIMITS["jikan"]  # (3, 1)
    n_threads = max_n * 4
    barrier = threading.Barrier(n_threads)
    results: list[float] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()  # release all threads at once to maximize contention
        slept = p._rate_acquire("jikan")
        with results_lock:
            results.append(slept)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n_threads
    immediate = sum(1 for r in results if r == 0.0)
    assert immediate == max_n, f"expected exactly {max_n} immediate admits, got {immediate}"


def test_concurrent_cache_and_rate_never_raise(monkeypatch):
    """Hammer the shared cache + rate buckets from many threads; the compound
    read-modify-writes must never raise (the pre-fix IndexError that could
    unwind into a deferred handler)."""
    monkeypatch.setattr(p, "_RATE_BUCKETS", {s: [] for s in p.SOURCE_RATE_LIMITS})
    p._cache_clear()
    errors: list[BaseException] = []

    def worker(i: int):
        try:
            for j in range(50):
                p._rate_acquire("jikan")
                p._cache_put(f"k{i}-{j}", {"data": j})
                p._cache_get(f"k{i}-{j}")
        except BaseException as exc:  # noqa: BLE001 — the whole point is to catch a raise
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent access raised: {errors[:3]}"
    # Cache stayed bounded despite concurrent eviction.
    assert len(p._CACHE) <= p.ANILIST_CACHE_MAX_ENTRIES


def test_rate_acquire_single_thread_behavior_preserved(monkeypatch):
    """The lock must not change single-threaded semantics: within budget admits
    with slept==0.0; over budget sleeps once."""
    monkeypatch.setattr(p, "_RATE_BUCKETS", {s: [] for s in p.SOURCE_RATE_LIMITS})
    sleeps: list[float] = []
    monkeypatch.setattr(p, "_sleep_for_retry", lambda s: sleeps.append(s))
    max_n, _window = p.SOURCE_RATE_LIMITS["jikan"]
    for _ in range(max_n):
        assert p._rate_acquire("jikan") == 0.0
    assert not sleeps
    over = p._rate_acquire("jikan")
    assert over > 0.0 and sleeps and sleeps[0] > 0.0


# ── #6 — request_id stamped on transport error/anomaly logs ─────────────────


def test_transport_failure_log_carries_request_id():
    ctx = MockContext()

    def _raise_post(*_a, **_k):
        raise RuntimeError("boom")

    ctx.http.post = _raise_post  # type: ignore[assignment]
    p._anilist_query(ctx, "query { Media }", {})
    entry = next(e for e in ctx.log_entries if e["message"].startswith("anilist call failed"))
    assert entry["request_id"] == ctx.request_id


def test_anomaly_log_carries_request_id():
    ctx = MockContext()
    p._log_http_body_anomaly(ctx, "anilist", {"status": 200, "body_bytes": b"\x1f\x8b\x08x"})
    entry = next(e for e in ctx.log_entries if e["message"] == "anilist http body anomaly")
    assert entry["request_id"] == ctx.request_id


def test_http_log_helper_stamps_request_id_and_forwards_fields():
    ctx = MockContext()
    p._http_log(ctx, "unit probe", level="error", tags=["t"], status="503")
    entry = next(e for e in ctx.log_entries if e["message"] == "unit probe")
    assert entry["request_id"] == ctx.request_id
    assert entry["status"] == "503"
    assert entry["level"] == "error"


def test_non_2xx_log_carries_request_id():
    ctx = MockContext()
    ctx.http.mock_response("api.jikan.moe", status=500, body="boom")
    p._jikan_query(ctx, "/anime", {"q": "x"})
    entry = next(e for e in ctx.log_entries if e["message"] == "jikan non-2xx")
    assert entry["request_id"] == ctx.request_id


def test_ok_path_still_works_after_edits():
    """Smoke: a normal AniList success is unaffected by the transport edits."""
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"data": {"Media": {"id": 1}}}))
    out = p._anilist_query(ctx, "query { Media }", {})
    assert out == {"Media": {"id": 1}}
    assert _outcomes(ctx, "anilist") == ["ok"]

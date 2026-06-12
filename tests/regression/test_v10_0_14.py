"""v10.0.14 regression contracts — host proxy compression regression.

Live diagnosis (2026-06-12 16:57 UTC, first run of the v10.0.11+ anomaly
diagnostics): the host proxy stopped decompressing upstream responses. The
anomaly lines named it exactly — body heads carried the gzip magic
(`\\x1f\\x8b\\x08`, AniList + Jikan) and the zstd magic (`\\x28\\xb5\\x2f\\xfd`,
Kitsu), with every byte >= 0x80 replaced by U+FFFD: the host lossily
utf-8-decodes the compressed bytes, so the str shape is unrecoverable
client-side. (The host response dict also gained an `ok` key.)

The fix is avoidance plus recovery plus diagnosis:
  1. All three transports send `Accept-Encoding: identity` so upstreams never
     compress in the first place.
  2. If a host ever forwards compressed bodies as REAL bytes, _http_json
     gunzips them (recoverable case).
  3. The anomaly logger names known compression signatures in a new
     `compression=` field, so the next such incident self-diagnoses.

These contracts are immutable.
"""

from __future__ import annotations

import gzip
import json

import plugin_main as p
from yourbot_sdk.testing import MockContext

# ── 1. Accept-Encoding: identity on every transport ─────────────────────────


def test_anilist_requests_identity_encoding():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200,
                           body=json.dumps({"data": {"Media": {"id": 1}}}))
    p._anilist_query(ctx, "query { Media }", {})
    headers = ctx.http.requests[0]["headers"] or {}
    assert headers.get("Accept-Encoding") == "identity"


def test_jikan_requests_identity_encoding():
    ctx = MockContext()
    ctx.http.mock_response("api.jikan.moe", status=200, body='{"data": []}')
    p._jikan_query(ctx, "/anime", {"q": "x"})
    headers = ctx.http.requests[0]["headers"] or {}
    assert headers.get("Accept-Encoding") == "identity"


def test_kitsu_requests_identity_encoding():
    ctx = MockContext()
    ctx.http.mock_response("kitsu.io", status=200, body='{"data": []}')
    p._kitsu_query(ctx, "/anime", {"filter[text]": "x"})
    headers = ctx.http.requests[0]["headers"] or {}
    assert headers.get("Accept-Encoding") == "identity"


# ── 2. gzip bytes bodies are recoverable ────────────────────────────────────


def test_http_json_gunzips_bytes_body():
    blob = gzip.compress(json.dumps({"data": {"ok": True}}).encode())
    assert p._http_json({"status": 200, "body_bytes": blob}) == {"data": {"ok": True}}


def test_http_json_corrupt_gzip_bytes_degrades_gracefully():
    corrupt = b"\x1f\x8b\x08\x00not really gzip"
    assert p._http_json({"status": 200, "body_bytes": corrupt}) is None  # no raise


# ── 3. compression signatures named in the anomaly line ────────────────────


def _mangle(blob: bytes) -> str:
    """Reproduce the host's lossy decode: utf-8 with replacement chars."""
    return blob.decode("utf-8", errors="replace")


def test_anomaly_names_gzip_mangled_str():
    """THE live-incident shape: gzip bytes lossily decoded to str."""
    ctx = MockContext()
    mangled = _mangle(gzip.compress(b'{"data": []}'))
    assert mangled.startswith("\x1f�\x08")  # sanity: matches the live head
    p._log_http_body_anomaly(ctx, "anilist", {
        "status": 200, "ok": True, "headers": {}, "truncated": False,
        "body_bytes": mangled,
    })
    entry = next(e for e in ctx.log_entries if e["message"] == "anilist http body anomaly")
    assert "gzip magic + U+FFFD" in entry["compression"]
    assert "unrecoverable" in entry["compression"]


def test_anomaly_names_zstd_mangled_str():
    ctx = MockContext()
    mangled = _mangle(b"\x28\xb5\x2f\xfd" + b"\x00" * 20)
    p._log_http_body_anomaly(ctx, "kitsu", {"status": 200, "body_bytes": mangled})
    entry = next(e for e in ctx.log_entries if e["message"] == "kitsu http body anomaly")
    assert "zstd magic + U+FFFD" in entry["compression"]


def test_anomaly_names_gzip_bytes():
    ctx = MockContext()
    p._log_http_body_anomaly(ctx, "jikan", {"status": 200, "body_bytes": b"\x1f\x8b\x08rest"})
    entry = next(e for e in ctx.log_entries if e["message"] == "jikan http body anomaly")
    assert "gzip-compressed bytes" in entry["compression"]


def test_anomaly_names_zstd_bytes():
    ctx = MockContext()
    p._log_http_body_anomaly(ctx, "kitsu", {"status": 200, "body_bytes": b"\x28\xb5\x2f\xfd" + b"\x00" * 8})
    entry = next(e for e in ctx.log_entries if e["message"] == "kitsu http body anomaly")
    assert "zstd-compressed bytes" in entry["compression"]


def test_anomaly_plain_body_reports_no_compression():
    ctx = MockContext()
    p._log_http_body_anomaly(ctx, "anilist", {"status": 200, "body_bytes": "<html>"})
    entry = next(e for e in ctx.log_entries if e["message"] == "anilist http body anomaly")
    assert entry["compression"] == "none-detected"

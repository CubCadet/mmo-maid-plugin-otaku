"""Regression contract for otaku v1.3.0.

IMMUTABLE — describes the user-visible behavior shipped at v1.3.0:
- /help lists every command from manifest.json (no hardcoded list)
- /genres caches AniList's GenreCollection in KV at `genres:global` for 24h
- Both commands present in manifest.json.slash_commands
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


def _slash(name: str, options: dict | None = None, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name=name,
        options=[{"name": k, "value": v} for k, v in (options or {}).items()],
        **extra,
    )


def _mock(ctx: MockContext, data: dict) -> None:
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": data}))


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


# ── Manifest contract ───────────────────────────────────────────────────────

def test_manifest_includes_help_and_genres():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert {"help", "genres"}.issubset(names)


# ── /help ───────────────────────────────────────────────────────────────────

# regression-fix (v10.0.6): /help now chunks the body across multiple embeds
# (Discord caps a single embed description at 4096 chars; the 47-command body
# exceeds that). The contract — every manifest command appears in the
# response — is preserved; the assertion now joins all embed descriptions
# before searching.
def test_help_reflects_every_manifest_command():
    ctx = MockContext()
    p.cmd_help(ctx, _slash("help", {}, user_id="reg-h"))

    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    body = "\n".join((e.get("description") or "") for e in resp["embeds"])
    for cmd in _manifest()["slash_commands"]:
        assert f"/{cmd['name']}" in body, f"missing /{cmd['name']} in /help"


def test_help_makes_no_http_call():
    ctx = MockContext()
    p.cmd_help(ctx, _slash("help", {}, user_id="reg-h2"))
    assert not ctx.http.requests


# ── /genres ─────────────────────────────────────────────────────────────────

def test_genres_writes_kv_with_24h_ttl():
    ctx = MockContext()
    _mock(ctx, {"GenreCollection": ["Action", "Drama"]})

    p.cmd_genres(ctx, _slash("genres", {}, user_id="reg-g"))

    assert ctx.kv.get(p.GENRES_KV_KEY) == ["Action", "Drama"]
    # TTL constant frozen at v1.3.0.
    assert p.GENRES_TTL == 24 * 60 * 60


def test_genres_uses_kv_cache_when_populated():
    ctx = MockContext()
    ctx.kv.set(p.GENRES_KV_KEY, ["Cached"], ttl_seconds=p.GENRES_TTL)

    p.cmd_genres(ctx, _slash("genres", {}, user_id="reg-g2"))

    assert not ctx.http.requests
    resp = ctx.interaction.responses[-1]
    assert "Cached" in resp["embeds"][0]["description"]

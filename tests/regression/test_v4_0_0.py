"""Regression contract for otaku v4.0.0 — airing notifications.

IMMUTABLE — what shipped at v4.0.0:
- `discord:send_message` capability is declared (Risky tier).
- /notify, /unnotify, /notify-list slash commands.
- /otaku-admin set-channel sub-command (admin-gated).
- Schema: otaku_notifications (user_id, media_id, channel_id, added_at;
  PK (user_id, media_id)).
- KV key for the announcement channel: NOTIFY_CHANNEL_KV.
- _dispatch_airing_announcements() honors the global announcement channel
  when set; otherwise falls back to the per-subscription channel_id.
- Dedup key shape: otaku:airing:<media_id>:<episode>, 24h TTL.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from yourbot_sdk.testing import MockContext, make_event


def _slash(name: str, options: dict | None = None, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name=name,
        options=[{"name": k, "value": v} for k, v in (options or {}).items()],
        **extra,
    )


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


SAMPLE = {
    "id": 901,
    "title": {"romaji": "Sample", "english": ""},
    "description": "",
    "coverImage": {"large": ""},
    "bannerImage": None,
    "averageScore": 0,
    "popularity": 0,
    "format": "TV",
    "episodes": 12,
    "status": "RELEASING",
    "season": "SUMMER",
    "seasonYear": 2024,
    "genres": [],
    "siteUrl": "https://anilist.co/anime/901",
}


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_declares_discord_send_message():
    caps = set(_manifest().get("capabilities_required", []))
    assert "discord:send_message" in caps


def test_manifest_includes_notify_commands():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert {"notify", "unnotify", "notify-list"}.issubset(names)


def test_otaku_admin_has_set_channel_subcommand():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "otaku-admin")
    subs = {o["name"] for o in cmd.get("options", []) if o.get("type") == 1}
    assert "set-channel" in subs


# ── Schema ──────────────────────────────────────────────────────────────────


def test_bootstrap_creates_otaku_notifications():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    sqls = [c["sql"] for c in ctx.sql.executed]
    assert any("CREATE TABLE IF NOT EXISTS otaku_notifications" in s for s in sqls)


# ── Constants frozen ────────────────────────────────────────────────────────


def test_notify_channel_kv_key():
    assert p.NOTIFY_CHANNEL_KV == "notify_channel:guild"


def test_dedup_key_format():
    assert p._airing_dedup_key(42, 3) == "otaku:airing:42:3"


def test_notify_dedup_ttl_is_24h():
    assert p.NOTIFY_DEDUP_TTL == 24 * 60 * 60


# ── /notify writes the expected upsert ──────────────────────────────────────


def test_notify_inserts_with_channel_id():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {"Media": SAMPLE}}))
    ctx.sql.query_one = lambda sql, params=None: None

    event = _slash("notify", {"anime": "x"}, user_id="reg-n")
    event["channel_id"] = "ch-1"
    p.cmd_notify(ctx, event)

    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_notifications" in c["sql"]]
    assert inserts and inserts[-1]["params"] == ["reg-n", 901, "ch-1"]


# ── Airing dispatch contract ────────────────────────────────────────────────


def _airing_payload(media_id: int, episode: int) -> str:
    from datetime import datetime, timezone
    return json.dumps({"data": {"Page": {
        "pageInfo": {"hasNextPage": False},
        "airingSchedules": [{
            "id": 1, "episode": episode,
            "airingAt": int(datetime.now(timezone.utc).timestamp()),
            "media": {
                "id": media_id, "episodes": 12,
                "siteUrl": f"https://anilist.co/anime/{media_id}",
                "title": {"romaji": "T", "english": ""},
                "coverImage": {"large": ""},
            },
        }],
    }}})


def test_dispatch_posts_to_kv_channel_when_set():
    ctx = MockContext()
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "global-ch")
    ctx.http.mock_response("graphql.anilist.co", status=200, body=_airing_payload(7, 2))
    ctx.sql.query = lambda sql, params=None: [{"user_id": "u", "channel_id": "user-ch"}]

    p._dispatch_airing_announcements(ctx)

    sent = ctx.discord.messages_sent[-1]
    assert sent["channel_id"] == "global-ch"


def test_dispatch_falls_back_to_subscription_channel_when_no_global():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200, body=_airing_payload(7, 2))
    ctx.sql.query = lambda sql, params=None: [{"user_id": "u", "channel_id": "user-ch"}]

    p._dispatch_airing_announcements(ctx)

    sent = ctx.discord.messages_sent[-1]
    assert sent["channel_id"] == "user-ch"


def test_dispatch_dedups_repeat_runs():
    ctx = MockContext()
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "g")
    ctx.http.mock_response("graphql.anilist.co", status=200, body=_airing_payload(7, 2))
    ctx.sql.query = lambda sql, params=None: [{"user_id": "u", "channel_id": "g"}]

    p._dispatch_airing_announcements(ctx)
    p._dispatch_airing_announcements(ctx)
    assert len(ctx.discord.messages_sent) == 1

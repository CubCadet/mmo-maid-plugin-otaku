"""Regression contract for otaku v2.6.0 — admin gating.

IMMUTABLE — what shipped at v2.6.0:
- `discord:read` capability is declared in the manifest.
- /otaku-admin slash command with `reset-user <user>` sub-command.
- `_caller_is_admin(ctx, user_id)` returns True for the guild owner OR any
  user whose roles have ADMINISTRATOR (0x8) or MANAGE_GUILD (0x20) bits set.
- Admin denial returns an ephemeral "server-admin only" message; no DELETE issued.
- Admin allow path deletes by `user_id` (the target).
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from yourbot_sdk.testing import MockContext, make_event


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_discord_read():
    caps = set(_manifest().get("capabilities_required", []))
    assert "discord:read" in caps


def test_manifest_includes_otaku_admin_with_subcommand():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "otaku-admin")
    sub = next(o for o in cmd["options"] if o["name"] == "reset-user")
    assert sub["type"] == 1  # SUB_COMMAND
    target = next(o for o in sub["options"] if o["name"] == "user")
    assert target["type"] == 6  # USER
    assert target["required"] is True


# ── _caller_is_admin contract ───────────────────────────────────────────────


def test_admin_check_constants_frozen():
    assert p.PERMISSION_ADMINISTRATOR == 0x8
    assert p.PERMISSION_MANAGE_GUILD == 0x20


def test_guild_owner_is_admin():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "owner"}
    assert p._caller_is_admin(ctx, "owner") is True


def test_user_with_administrator_role_is_admin():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}
    ctx.discord.get_member = lambda *, user_id: {"user_id": user_id, "roles": ["mod"]}
    ctx.discord.list_roles = lambda: [{"id": "mod", "name": "Mod", "permissions": "8"}]
    assert p._caller_is_admin(ctx, "any") is True


def test_user_with_manage_guild_role_is_admin():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}
    ctx.discord.get_member = lambda *, user_id: {"user_id": user_id, "roles": ["mg"]}
    ctx.discord.list_roles = lambda: [{"id": "mg", "name": "MG", "permissions": "32"}]
    assert p._caller_is_admin(ctx, "any") is True


def test_user_with_only_message_perms_is_not_admin():
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}
    ctx.discord.get_member = lambda *, user_id: {"user_id": user_id, "roles": ["plain"]}
    ctx.discord.list_roles = lambda: [{"id": "plain", "name": "Plain", "permissions": "2048"}]
    assert p._caller_is_admin(ctx, "any") is False


def test_non_admin_otaku_admin_denied():
    ctx = MockContext()
    # MockDiscord defaults: get_member returns roles=[], get_guild owner_id="1"
    event = make_event(
        "interaction_create",
        interaction_type=2,
        command_name="otaku-admin",
        options=[{"name": "reset-user", "type": 1,
                  "options": [{"name": "user", "value": "victim", "type": 6}]}],
        user_id="rando",
    )
    # regression-fix (v8.0.0): otaku_user_anime renamed to otaku_user_media.
    p.cmd_otaku_admin(ctx, event)
    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True
    assert not any("DELETE FROM otaku_user_media" in c["sql"] for c in ctx.sql.executed)


def test_admin_otaku_admin_deletes_target_user():
    # regression-fix (v8.0.0): otaku_user_anime renamed to otaku_user_media.
    ctx = MockContext()
    ctx.discord.get_guild = lambda: {"id": "g", "owner_id": "boss"}
    event = make_event(
        "interaction_create",
        interaction_type=2,
        command_name="otaku-admin",
        options=[{"name": "reset-user", "type": 1,
                  "options": [{"name": "user", "value": "victim", "type": 6}]}],
        user_id="boss",
    )
    p.cmd_otaku_admin(ctx, event)
    deletes = [c for c in ctx.sql.executed if "DELETE FROM otaku_user_media" in c["sql"]]
    assert deletes and deletes[-1]["params"] == ["victim"]

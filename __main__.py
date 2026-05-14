"""
My MMO Maid Plugin
==================

This is the entry point for your plugin.
The SDK handles all communication with the MMO Maid runner automatically.

Rename this plugin, update capabilities in the dev portal when uploading,
then submit for review.

Docs: https://mmomaid.com/dev/docs
"""
from mmo_maid_sdk import Plugin, Context

plugin = Plugin()


@plugin.on_ready
def on_ready(ctx: Context):
    """Called once when your plugin boots. Good place to load config."""
    ctx.log("My plugin is ready!")


# ── Event handlers ────────────────────────────────────────────────────────────
#
# Uncomment and customise the events your plugin needs.
# Remember to declare the matching capabilities when uploading your zip.
#
# @plugin.on_event("message_create")
# def on_message(ctx: Context, event: dict):
#     """
#     Called when a message is sent in any channel the bot can see.
#
#     event keys:
#       channel_id   str   Discord channel ID
#       user_id      str   Discord user ID of the sender
#       username     str   Display name of the sender
#       content      str   Message text (requires events:message_content capability)
#       guild_id     str   Discord server ID
#     """
#     content = event.get("content", "")
#
#     if "!hello" in content:
#         ctx.discord.send_message(
#             channel_id=event["channel_id"],
#             content=f"Hello, {event.get('username', 'there')}! 👋",
#         )


# @plugin.on_event("member_join")
# def on_member_join(ctx: Context, event: dict):
#     """
#     Called when a member joins the server.
#
#     event keys:
#       user_id    str   Discord user ID
#       username   str   Display name
#       guild_id   str   Discord server ID
#     """
#     ctx.log(f"New member: {event.get('username')}")


# @plugin.on_event("member_leave")
# def on_member_leave(ctx: Context, event: dict):
#     """Called when a member leaves or is kicked/banned."""
#     ctx.log(f"Member left: {event.get('username')}")


# ── KV storage example ────────────────────────────────────────────────────────
#
# Store and retrieve data scoped to this server + plugin:
#
# @plugin.on_event("message_create")
# def count_messages(ctx: Context, event: dict):
#     count = ctx.kv.get("msg_count") or 0
#     ctx.kv.set("msg_count", count + 1)
#
#     if count % 100 == 0:
#         ctx.discord.send_message(
#             channel_id=event["channel_id"],
#             content=f"🎉 {count} messages sent in this server!",
#         )


# ── HTTP requests example ─────────────────────────────────────────────────────
#
# Make HTTP requests to pre-approved domains (requires proxy:http capability).
# Only domains you declared when uploading can be reached.
#
# @plugin.on_event("message_create")
# def fetch_joke(ctx: Context, event: dict):
#     if "!joke" not in event.get("content", ""):
#         return
#
#     resp = ctx.http.get("https://official-joke-api.appspot.com/random_joke")
#     if resp.get("status") == 200:
#         import json
#         joke = json.loads(resp["body"])
#         ctx.discord.send_message(
#             channel_id=event["channel_id"],
#             content=f"{joke['setup']}\n\n||{joke['punchline']}||",
#         )


# ── Dashboard handlers ────────────────────────────────────────────────────────
#
# Define data handlers for your plugin's web dashboard.
# The platform calls these when a server admin visits /p/{plugin_id}/dashboard.
# You define the dashboard layout in a JSON manifest (Dev Portal → Dashboard Editor).
#
# @plugin.on_dashboard("get_stat")
# def handle_stat(ctx: Context, params: dict):
#     """Return data for a stat_card widget."""
#     total = ctx.kv.get("stat:total_events") or 0
#     return {"value": int(total), "change": "+5%"}
#
# @plugin.on_dashboard("get_timeseries")
# def handle_timeseries(ctx: Context, params: dict):
#     """Return data for a chart widget."""
#     data = ctx.kv.get("weekly_counts") or {}
#     return {
#         "labels": data.get("labels", ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]),
#         "series": [{"name": "Events", "data": data.get("values", [0]*7)}],
#     }
#
# @plugin.on_dashboard("get_settings")
# def handle_get_settings(ctx: Context, params: dict):
#     """Return current config for a form widget."""
#     return {"values": ctx.kv.get("config") or {"prefix": "!", "enabled": True}}
#
# @plugin.on_dashboard("save_settings")
# def handle_save_settings(ctx: Context, params: dict):
#     """Save config from a form widget submission."""
#     ctx.kv.set("config", {"prefix": params.get("prefix", "!"), "enabled": bool(params.get("enabled"))})
#     return {"ok": True}


# ── This must be the last line ────────────────────────────────────────────────
plugin.run()

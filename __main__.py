"""
Otaku — anime discovery, tracking, and community plugin for Discord via AniList.

Surfaces 35 slash commands across discovery (/anime, /discover, /trending,
/similar, /random, /character, /genres, /mood, /genre-trends), personal
tracking (/favorite, /favorites, /watch, /list, /rate, /ratings, /progress,
/import, /stats, /my-stats, /otaku-reset), social/community (/compare, /wp,
/server-watchlist, /leaderboard, /aotw, /poll, /review, /reviews), and
airing notifications (/notify, /unnotify, /notify-list, /season-premieres).
Admin tools live under /otaku-admin.

Backed by Postgres (per-user tracker, reviews, polls, watch parties,
notifications), KV (last-anime cache + per-server settings), an hourly UTC
cron for airing pings + seasonal premieres digest, and a manifest-mode
dashboard. Storage is auto-scoped per (server_id, plugin_id) by the runner.

The full per-phase contract lives in CHANGELOG.md and ROADMAP.md.
"""
from __future__ import annotations

import base64
import json
import math
import os
import random
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from yourbot_sdk import (
    ActionRow,
    Button,
    Context,
    Plugin,
    RateLimitError,
    RpcTimeoutError,
    SelectMenu,
    SelectOption,
    TextInput,
    ValidationError,
)

plugin = Plugin()


# ── SQL schema ───────────────────────────────────────────────────────────────
# Per-user anime tracking. Rows auto-scoped to ctx.server_id by the runner —
# no server_id column needed. DDL is idempotent (IF NOT EXISTS) and runs from
# both on_install and on_ready so pool-mode workers and v1.x→v2.0 upgrades
# both seed cleanly. See ROADMAP.md "Phase 2" for the rationale.
# v8.0.0 renamed `otaku_user_anime` → `otaku_user_media` and added a
# `media_type` column (PK extended to include it) so manga, light novels,
# and future media types can share the table. The `episodes_watched` column
# stays named — it carries chapter count for manga rows; the column comment
# documents the dual interpretation. Migration from v7.x is handled by
# `_migrate_v7_to_v8`, run idempotently from `_bootstrap_schema`.
_SCHEMA_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_user_media ("
    "  user_id TEXT NOT NULL,"
    "  media_id INTEGER NOT NULL,"
    "  media_type TEXT NOT NULL DEFAULT 'anime',"
    "  status TEXT NOT NULL,"
    "  is_favorite BOOLEAN NOT NULL DEFAULT FALSE,"
    "  added_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    # v10.0.7 — folded into the initial CREATE TABLE so fresh installs
    # don't need separate ALTER ADD COLUMN statements against the host's
    # DDL rate limit. The standalone _SCHEMA_RATING_DDL / _SCHEMA_EPISODES_DDL
    # in `_bootstrap_schema` remain to cover existing v2.0-era tables
    # that pre-date these columns; they no-op via IF NOT EXISTS on fresh
    # installs.
    "  rating SMALLINT,"
    "  episodes_watched SMALLINT DEFAULT 0,"
    "  PRIMARY KEY (user_id, media_id, media_type))"
)
_SCHEMA_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS otaku_user_media_user_status_added_idx "
    "ON otaku_user_media (user_id, status, added_at DESC)"
)

# v2.1.0 — additive column for ratings (1.0–10.0 stored as int*2, range 2..20).
# IF NOT EXISTS makes this idempotent and safe to run on existing tables.
_SCHEMA_RATING_DDL = (
    "ALTER TABLE otaku_user_media ADD COLUMN IF NOT EXISTS rating SMALLINT"
)
# v2.2.0 — additive column for episode progress (chapter count for manga rows).
_SCHEMA_EPISODES_DDL = (
    "ALTER TABLE otaku_user_media ADD COLUMN IF NOT EXISTS episodes_watched SMALLINT DEFAULT 0"
)
# v3.0.0 — per-server shared watchlist. Auto-scoped to server_id by the runner,
# so media_id alone is enough for the PK. Curated by admins.
_SCHEMA_SERVER_WATCHLIST_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_server_watchlist ("
    "  media_id INTEGER NOT NULL,"
    "  added_by TEXT NOT NULL,"
    "  added_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  note TEXT,"
    "  PRIMARY KEY (media_id))"
)
# v3.2.0 — watch parties. party_id is SERIAL across all servers; the runner's
# row-level scoping by server_id keeps tenants from seeing each other's parties.
_SCHEMA_WATCH_PARTY_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_watch_parties ("
    "  party_id SERIAL PRIMARY KEY,"
    "  media_id INTEGER NOT NULL,"
    "  created_by TEXT NOT NULL,"
    "  created_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  status TEXT NOT NULL DEFAULT 'active')"
)
_SCHEMA_WATCH_PARTY_MEMBERS_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_watch_party_members ("
    "  party_id INTEGER NOT NULL,"
    "  user_id TEXT NOT NULL,"
    "  episodes_watched SMALLINT NOT NULL DEFAULT 0,"
    "  joined_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  PRIMARY KEY (party_id, user_id))"
)
# v4.0.0 — per-user airing notification subscriptions. channel_id remembers
# where the user subscribed so the cron has a fallback target if no server-wide
# announcement channel is configured.
_SCHEMA_NOTIFICATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_notifications ("
    "  user_id TEXT NOT NULL,"
    "  media_id INTEGER NOT NULL,"
    "  channel_id TEXT,"
    "  added_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  PRIMARY KEY (user_id, media_id))"
)
# v7.0.0 — per-user-per-anime reviews. Updating a review keeps the original
# created_at and bumps updated_at, so we can sort newest-edited-first while
# still showing how old the review is.
_SCHEMA_REVIEWS_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_reviews ("
    "  user_id TEXT NOT NULL,"
    "  media_id INTEGER NOT NULL,"
    "  title TEXT NOT NULL,"
    "  body TEXT NOT NULL,"
    "  created_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  updated_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  PRIMARY KEY (user_id, media_id))"
)
# v7.1.0 — anime of the week. One active poll per server (enforced in
# /aotw start). Candidates are pulled from otaku_server_watchlist; votes
# are one-per-user-per-poll (PK (poll_id, user_id)); clicking another
# candidate updates the existing vote rather than adding a new one.
_SCHEMA_AOTW_POLLS_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_aotw_polls ("
    "  poll_id SERIAL PRIMARY KEY,"
    "  started_by TEXT NOT NULL,"
    "  started_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  ended_at TIMESTAMP,"
    "  winner_id INTEGER,"
    "  status TEXT NOT NULL DEFAULT 'active')"
)
_SCHEMA_AOTW_CANDIDATES_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_aotw_candidates ("
    "  poll_id INTEGER NOT NULL,"
    "  media_id INTEGER NOT NULL,"
    "  PRIMARY KEY (poll_id, media_id))"
)
_SCHEMA_AOTW_VOTES_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_aotw_votes ("
    "  poll_id INTEGER NOT NULL,"
    "  user_id TEXT NOT NULL,"
    "  media_id INTEGER NOT NULL,"
    "  voted_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  PRIMARY KEY (poll_id, user_id))"
)
# v7.2.0 — generic server polls. Free-form question + up to 4 options;
# admin-only create/end, public status. One vote per user per poll
# (changing your choice UPDATEs).
_SCHEMA_POLLS_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_polls ("
    "  poll_id SERIAL PRIMARY KEY,"
    "  started_by TEXT NOT NULL,"
    "  question TEXT NOT NULL,"
    "  started_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  ended_at TIMESTAMP,"
    "  status TEXT NOT NULL DEFAULT 'active')"
)
_SCHEMA_POLL_OPTIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_poll_options ("
    "  poll_id INTEGER NOT NULL,"
    "  option_key TEXT NOT NULL,"
    "  text TEXT NOT NULL,"
    "  PRIMARY KEY (poll_id, option_key))"
)
_SCHEMA_POLL_VOTES_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_poll_votes ("
    "  poll_id INTEGER NOT NULL,"
    "  user_id TEXT NOT NULL,"
    "  option_key TEXT NOT NULL,"
    "  voted_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  PRIMARY KEY (poll_id, user_id))"
)
# v10.0.0 — per-user achievements. Once a user meets an achievement
# predicate the row is inserted; existence is idempotent for the
# rest of the user's lifetime on this server. Awarded lazily on
# /achievements lookup (no per-handler check overhead).
_SCHEMA_ACHIEVEMENTS_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_achievements ("
    "  user_id         TEXT NOT NULL,"
    "  achievement_key TEXT NOT NULL,"
    "  awarded_at      TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  PRIMARY KEY (user_id, achievement_key))"
)


# ── User-facing strings (i18n-ready) ─────────────────────────────────────────
# Every string the user could ever see lives here. The runtime upload zip's
# allowlist doesn't permit a sibling strings.py module, so we keep them in a
# single namespace inside __main__.py. A future localization layer can swap
# this namespace out per-locale.
class _Strings:
    # Error helpers.
    ANILIST_FAILURE_DEFAULT = (
        "AniList didn't answer — try again in a moment, or try a different keyword."
    )
    ANILIST_PREFIX = "AniList: {msg}"

    # Cooldown.
    COOLDOWN_WAIT = "Slow down a bit — try again in {remaining}s."

    # Generic.
    UNTITLED = "Untitled"

    # /anime.
    ANIME_USAGE = "Usage: `/anime query: <title>`"
    ANIME_NOT_FOUND = "No anime found matching **{query}**."
    ANIME_NO_DESCRIPTION = "*(no description)*"

    # /manga (v8.0.0).
    MANGA_USAGE = "Usage: `/manga query: <title>`"
    MANGA_NOT_FOUND = "No manga found matching **{query}**."
    MANGA_NO_DESCRIPTION = "*(no description)*"
    MANGA_DISCOVER_USAGE = "Usage: `/manga-discover genre: <name> [sort: popular|trending|score]`"
    MANGA_DISCOVER_NO_RESULTS = "No {genre} manga found."
    MANGA_PROGRESS_BOUNDED = "Chapter {chapters} / {total}"
    MANGA_PROGRESS_UNBOUNDED = "Chapter {chapters}"

    # /manga-favorites (v8.0.0). Largely mirrors the anime favorite strings;
    # split so the wording can drift if needed.
    MANGA_FAVORITE_NO_CACHE = (
        "You haven't looked up a manga yet. Try `/manga query: <title>` first."
    )
    MANGA_FAVORITE_ADDED = "⭐ Added **{title}** to your manga favorites."
    MANGA_FAVORITE_ALREADY = "**{title}** is already in your manga favorites."
    MANGA_FAVORITE_REMOVED = "Removed **{title}** from your manga favorites."
    MANGA_FAVORITE_NOT_PRESENT = "**{title}** wasn't in your manga favorites."
    MANGA_FAVORITES_HEADER_OWN = "📚 Your manga favorites"
    MANGA_FAVORITES_HEADER_OTHER = "📚 {who}'s manga favorites"
    MANGA_FAVORITES_EMPTY_OWN = (
        "You haven't added any manga favorites yet. Run `/manga-favorites` "
        "after a `/manga query: <title>` lookup."
    )
    MANGA_FAVORITES_EMPTY_OTHER = "{who} hasn't added any manga favorites yet."

    # /discover.
    DISCOVER_USAGE = "Usage: `/discover genre: <name> [sort: popular|trending|score]`"
    DISCOVER_NO_RESULTS = "No {genre} anime found."

    # /trending.
    TRENDING_NO_RESULTS = "No trending anime found for this season."

    # /similar.
    SIMILAR_NO_RECS = "No recommendations on AniList for **{title}** yet."
    SIMILAR_INVALID_CACHED = (
        "Your cached anime ID looks invalid — look one up again with `/anime`."
    )
    SIMILAR_NO_CACHE = (
        "You haven't looked up an anime yet. Try `/anime query: <title>` first."
    )
    SIMILAR_FETCH_FAIL_CACHED = (
        "Couldn't fetch recommendations for your last anime — try `/anime query: <title>` to refresh it."
    )
    SIMILAR_FETCH_FAIL_BUTTON = (
        "Couldn't fetch recommendations from AniList — try again, or pick a different anime."
    )

    # /random.
    RANDOM_NO_RESULTS = "No random anime came back from {label} — try a different genre."
    RANDOM_FILTER_LABEL = "the **{genre}** filter"
    RANDOM_NO_FILTER_LABEL = "AniList"

    # /character.
    CHARACTER_USAGE = "Usage: `/character query: <name>`"
    CHARACTER_NOT_FOUND = "No character found matching **{query}**."
    CHARACTER_NO_DESCRIPTION = "*(no description on AniList)*"
    CHARACTER_UNKNOWN_NAME = "Unknown character"

    # /voice-actor (v8.1.0).
    VOICE_ACTOR_USAGE = "Usage: `/voice-actor query: <name>`"
    VOICE_ACTOR_NOT_FOUND = "No voice actor found matching **{query}**."
    VOICE_ACTOR_NO_DESCRIPTION = "*(no bio on AniList)*"
    VOICE_ACTOR_NO_ROLES = "*(no notable character roles on AniList)*"

    # /staff (v8.1.0).
    STAFF_USAGE = "Usage: `/staff query: <name>`"
    STAFF_NOT_FOUND = "No staff found matching **{query}**."
    STAFF_NO_DESCRIPTION = "*(no bio on AniList)*"
    STAFF_NO_CREDITS = "*(no production credits on AniList)*"

    # /character-popular (v8.3.0).
    CHARACTER_POPULAR_HEADER = "⭐ Top characters by AniList favourites"
    CHARACTER_POPULAR_EMPTY = "AniList didn't return any characters."
    CHARACTER_POPULAR_FOOTER = "Page {page} · sorted by AniList favourites"
    CHARACTER_POPULAR_PAGE_MALFORMED = "Popular-character pagination button malformed."

    # /studio (v8.2.0).
    STUDIO_USAGE = "Usage: `/studio query: <name>`"
    STUDIO_NOT_FOUND = "No studio found matching **{query}**."
    STUDIO_NO_WORKS = "*(no main-work credits on AniList)*"
    STUDIO_HEADER_ANIM = "🎬 {name}"
    STUDIO_HEADER_OTHER = "🏢 {name}"
    STUDIO_FOOTER = "Data from AniList · main-credit works only"

    # /genres.
    GENRES_FETCH_FAIL = "Couldn't fetch AniList's genre list — try again in a moment."
    GENRES_EMPTY = "*(no genres returned)*"

    # /help.
    HELP_TITLE = "Otaku — commands"
    HELP_FOOTER = "All data comes from AniList. Type `/help` any time."
    HELP_EMPTY = "*(no commands registered)*"

    # Component errors.
    PAGE_MALFORMED = "Pagination button malformed."
    SIMILAR_BTN_MALFORMED = "Similar button malformed."
    EXPAND_NO_SELECTION = "No selection received."
    EXPAND_INVALID = "Selection wasn't a valid anime ID."
    EXPAND_FETCH_FAIL = (
        "Couldn't fetch that anime from AniList — try searching again with `/anime`."
    )

    # Footers & list strings.
    FOOTER_ANILIST = "Data from AniList"
    FOOTER_CHARACTER = "Data from AniList · first match only"
    LIST_NO_RESULTS = "*No results.*"
    LIST_LAST_PAGE = "(last page)"

    # /favorite.
    FAVORITE_NO_CACHE = (
        "You haven't looked up an anime yet. Try `/anime query: <title>` first."
    )
    FAVORITE_ADDED = "⭐ Added **{title}** to your favorites."
    FAVORITE_ALREADY = "**{title}** is already in your favorites."
    FAVORITE_REMOVED = "Removed **{title}** from your favorites."
    FAVORITE_NOT_PRESENT = "**{title}** wasn't in your favorites."

    # /watch.
    WATCH_NO_CACHE = (
        "You haven't looked up an anime yet. Try `/anime query: <title>` first."
    )
    WATCH_INVALID_STATUS = "Status must be one of: {choices}"
    WATCH_SET = "Marked **{title}** as **{status}**."

    # /list & /favorites.
    LIST_HEADER_OWN = "📚 Your {scope} list"
    LIST_HEADER_OTHER = "📚 {who}'s {scope} list"
    LIST_SCOPE_ALL = "anime"
    LIST_SCOPE_FAVORITES = "favorites"
    LIST_EMPTY_OWN = (
        "You haven't added any anime yet. Try `/favorite` or "
        "`/watch status: watching` after a `/anime` lookup."
    )
    LIST_EMPTY_OTHER = "{who} hasn't added any anime yet."

    # Generic errors for v2 paths.
    SQL_FAIL = "Couldn't reach the database — try again in a moment."
    LIST_PAGE_MALFORMED = "List button malformed."

    # /rate.
    RATE_NO_CACHE = (
        "You haven't looked up an anime yet. Try `/anime query: <title>` first."
    )
    RATE_OUT_OF_RANGE = "Score must be between 1.0 and 10.0."
    RATE_SET = "Rated **{title}** {score}/10."

    # /notify, /unnotify, /notify-list.
    NOTIFY_USAGE = "Pass an anime with `anime:` — e.g. `/notify anime: Frieren`."
    NOTIFY_SUBSCRIBED = "🔔 You'll be pinged when new episodes of **{title}** air."
    NOTIFY_ALREADY = "You're already subscribed to **{title}**."
    NOTIFY_REMOVED = "Removed airing notifications for **{title}**."
    NOTIFY_NOT_SUBSCRIBED = "You're not subscribed to **{title}**."
    NOTIFY_LIST_HEADER = "🔔 Your airing subscriptions"
    NOTIFY_LIST_EMPTY = (
        "You're not subscribed to any anime. Try `/notify anime: <title>` "
        "to get pinged when new episodes air."
    )
    NOTIFY_LIST_LINE = "• [{title}]({url}) · next: {next_eta}"
    NOTIFY_NO_NEXT = "no upcoming episode"
    NOTIFY_ANNOUNCEMENT_TITLE = "📺 New episode airing — {title}"
    NOTIFY_ANNOUNCEMENT_BODY = (
        "Episode **{episode}**{of_total} is airing now.\n"
        "{mentions}"
    )

    # /season-premieres.
    PREMIERES_USAGE = "Pick a season from the choices and pass a valid year."
    PREMIERES_HEADER = "🌸 {season} {year} premieres"
    PREMIERES_EMPTY = "No premieres found for {season} {year}."
    PREMIERES_PAGE_MALFORMED = "Premieres button malformed."
    PREMIERES_DIGEST_TITLE = "🌸 {season} {year} is starting — top premieres"
    PREMIERES_DIGEST_FOOTER = (
        "Use `/notify anime: <title>` to get pinged when episodes air."
    )

    # /otaku-admin set-channel.
    ADMIN_CHANNEL_SET = "Airing notifications will now post in <#{channel_id}>."
    ADMIN_CHANNEL_CLEARED = (
        "Cleared the announcement channel. Airing notifications will fall back to "
        "the channel where each user subscribed."
    )

    # /leaderboard.
    LEADERBOARD_HEADER_COMPLETED = "🏆 Server leaderboard — most completed"
    LEADERBOARD_HEADER_SCORE = "🏆 Server leaderboard — highest mean score"
    LEADERBOARD_HEADER_HOURS = "🏆 Server leaderboard — most hours watched"
    LEADERBOARD_EMPTY = (
        "Nobody on this server has tracked enough anime for a leaderboard yet. "
        "Try `/favorite`, `/watch`, or `/progress` to start the standings."
    )
    LEADERBOARD_FOOTER_COMPLETED = "Top {n} by completed count · Data from AniList"
    LEADERBOARD_FOOTER_SCORE = "Top {n} by mean score (≥ {min_rated} rated to qualify) · Data from AniList"
    LEADERBOARD_FOOTER_HOURS = "Top {n} by hours · 24min/episode heuristic · Data from AniList"

    # /wp — watch parties.
    WP_CREATE_USAGE = "Pass an anime with `anime:` — e.g. `/wp create anime: Frieren`."
    WP_ID_USAGE = "Pass a party id with `id:` — see `/wp status` or the create embed."
    WP_PROGRESS_NEGATIVE = "Episode must be 0 or higher."
    WP_PROGRESS_OVER_TOTAL = "Capped your episode at the show's total ({total})."
    WP_CREATED_TITLE = "🎬 Watch party started — {title}"
    WP_CREATED_BODY = (
        "Party id: **{party_id}**\nStarted by <@{user}>\n\n"
        "Anyone in the server can join with `/wp join id: {party_id}` "
        "(or hit the button below). Track your progress with "
        "`/wp progress id: {party_id} episode: <n>`."
    )
    WP_JOIN_BUTTON = "Join party"
    WP_NOT_FOUND = "No watch party with id **{party_id}** on this server."
    WP_JOINED = "Joined watch party **{party_id}** ({title})."
    WP_ALREADY_JOINED = "You're already in watch party **{party_id}**."
    WP_PROGRESS_UPDATED = "Recorded your progress on party **{party_id}**: episode {episodes}{of_total}."
    WP_PROGRESS_NOT_MEMBER = "You haven't joined party **{party_id}** yet — `/wp join id: {party_id}`."
    WP_STATUS_HEADER = "🎬 Watch party {party_id} — {title}"
    WP_STATUS_EMPTY_MEMBERS = "*(no members yet)*"
    WP_SYNC_ANNOUNCE = "🎉 Everyone in watch party **{party_id}** has reached episode {episode}!"
    WP_STATUS_COMPLETED = "✅ Completed"
    WP_STATUS_ABANDONED = "🛑 Abandoned"
    WP_STATUS_ACTIVE = "▶ Active"

    # /compare.
    COMPARE_SELF = "Pick a *different* user — comparing against yourself isn't very interesting."
    COMPARE_HEADER = "🔍 You vs <@{other}>"
    COMPARE_EMPTY_BOTH = (
        "Neither of you have tracked any anime on this server yet. "
        "Try `/favorite` or `/watch` after a `/anime` lookup first."
    )
    COMPARE_EMPTY_YOU = (
        "You haven't tracked anything yet — go run `/anime` then `/favorite` "
        "or `/watch`, and try again."
    )
    COMPARE_EMPTY_THEM = "<@{other}> hasn't tracked anything on this server yet."
    COMPARE_FIELD_SHARED = "Shared favorites"
    COMPARE_FIELD_DIVERGENT = "You disagree on"
    COMPARE_FIELD_RECS = "Anime they've completed (and you haven't)"
    COMPARE_FIELD_TOTALS = "Tracked totals"
    COMPARE_NONE = "*(none yet)*"

    # /server-watchlist.
    SWL_ADD_USAGE = "Pass an anime with `anime:` — e.g. `/server-watchlist add anime: Frieren`."
    SWL_ADDED = "📥 Added **{title}** to the server watchlist."
    SWL_ALREADY = "**{title}** is already on the server watchlist."
    SWL_REMOVED = "Removed **{title}** from the server watchlist."
    SWL_NOT_PRESENT = "**{title}** isn't on the server watchlist."
    SWL_EMPTY = (
        "This server hasn't added anything to its watchlist yet. "
        "Admins can add with `/server-watchlist add anime: <title>`."
    )
    SWL_HEADER = "📚 Server watchlist"
    SWL_PAGE_MALFORMED = "Watchlist button malformed."
    SWL_ADMIN_DENIED = "Adding to the server watchlist is admin-only. Ask someone with `Manage Server`."

    # /otaku-admin.
    ADMIN_DENIED = (
        "This command is server-admin only. Ask someone with `Manage Server` or "
        "`Administrator` to run it."
    )
    ADMIN_USER_REQUIRED = "Pass a user with `user:` — e.g. `/otaku-admin reset-user user:@them`."
    ADMIN_RESET_DONE = "🗑 Deleted **{rows}** tracked row(s) for <@{user}>."
    ADMIN_RESET_NOTHING = "<@{user}> has no tracked anime on this server. Nothing to delete."

    # /otaku-reset.
    RESET_CONFIRM_PROMPT = (
        "⚠️ This will delete **all** of your otaku data on this server: favorites, "
        "ratings, watch statuses, episode progress, everything tracked. "
        "Your `/anime` lookups stay (those aren't stored). This **cannot be undone**."
    )
    RESET_CONFIRM_BUTTON = "Yes, delete it all"
    RESET_CANCEL_BUTTON = "Cancel"
    RESET_DONE = "Deleted **{rows}** tracked row(s). You're starting fresh on this server."
    RESET_NOTHING = "You don't have any tracked anime on this server. Nothing to delete."
    RESET_CANCELLED = "Cancelled — nothing was deleted."

    # Rating-on-card.
    RATING_FIELD_NAME = "Your rating"

    # /progress.
    PROGRESS_NO_CACHE = (
        "You haven't looked up an anime yet. Try `/anime query: <title>` first."
    )
    PROGRESS_NEGATIVE = "Episode count must be 0 or higher."
    PROGRESS_OVER_TOTAL = (
        "**{title}** only has {total} episode(s) — capping your progress at that."
    )
    PROGRESS_SET = "📺 Marked **{title}** at episode {episodes}{of_total}."
    PROGRESS_OF_TOTAL = " / {total}"
    PROGRESS_FIELD_NAME = "Your progress"
    PROGRESS_FIELD_VALUE_UNBOUNDED = "Episode {episodes}"
    PROGRESS_FIELD_VALUE_BOUNDED = "Episode {episodes} / {total}"

    # /import.
    IMPORT_USERNAME_BLANK = "Pass an AniList username with `anilist:` — e.g. `/import anilist: yourname`."
    IMPORT_USER_NOT_FOUND = (
        "AniList didn't find a user named **{username}** — double-check spelling and "
        "make sure their list is public."
    )
    IMPORT_SUMMARY = (
        "Imported **{total}** anime from AniList user **{username}**: "
        "{new} new, {updated} updated."
    )
    IMPORT_PARTIAL = (
        "Import stopped after page {pages} due to a malformed response. "
        "Got {total} so far — try again to pick up the rest."
    )

    # /my-stats.
    MY_STATS_HEADER = "📊 Your full otaku report"
    MY_STATS_EMPTY = (
        "You haven't tracked any anime yet. Try `/favorite`, `/watch`, or "
        "`/progress` after a `/anime` lookup."
    )
    MY_STATS_NONE = "*(none yet)*"

    # /stats.
    STATS_HEADER_OWN = "📊 Your anime stats"
    STATS_HEADER_OTHER = "📊 {who}'s anime stats"
    STATS_EMPTY_OWN = (
        "You haven't tracked any anime yet. Try `/favorite`, `/watch`, or `/progress` "
        "after a `/anime` lookup."
    )
    STATS_EMPTY_OTHER = "{who} hasn't tracked any anime yet."

    # /ratings.
    RATINGS_HEADER_OWN = "⭐ Your ratings"
    RATINGS_HEADER_OTHER = "⭐ {who}'s ratings"
    RATINGS_EMPTY_OWN = (
        "You haven't rated any anime yet. Try `/rate score: 8` after a `/anime` lookup."
    )
    RATINGS_EMPTY_OTHER = "{who} hasn't rated any anime yet."

    # /recommend (v6.0.0).
    RECOMMEND_HEADER = "✨ Recommended for you"
    RECOMMEND_FOOTER_CF = "Collaborative filtering · {peers} similar viewer(s) in this server"
    RECOMMEND_FALLBACK_HEADER = "✨ Like **{title}**"
    RECOMMEND_FALLBACK_FEW_RATINGS = (
        "Rate a few more anime with `/rate` and I'll mix in this server's taste. "
        "For now, here are AniList's picks based on your top-rated tracked anime:"
    )
    RECOMMEND_FALLBACK_NO_PEERS = (
        "Nobody else on this server has rated enough anime in common with you yet. "
        "Here are AniList's picks based on your top-rated tracked anime:"
    )
    RECOMMEND_FALLBACK_EMPTY = (
        "AniList doesn't have any unseen similar anime for your top pick — "
        "try `/similar` on something else."
    )
    RECOMMEND_NO_DATA = (
        "You haven't tracked any anime yet. Try `/favorite` or `/rate score: <n>` "
        "after a `/anime` lookup."
    )

    # /poll (v7.2.0).
    POLL_NOT_ADMIN = (
        "Only server admins can run `/poll {action}`. Ask a moderator with "
        "`Manage Server` or `Administrator`."
    )
    POLL_USAGE = "Pick a subcommand: `create`, `status`, or `end`."
    POLL_QUESTION_REQUIRED = "Poll needs a question."
    POLL_OPTIONS_REQUIRED = "Poll needs at least 2 options."
    POLL_STATUS_HEADER = "📊 Poll #{poll_id} — {question}"
    POLL_FOOTER_LIVE = "{n} votes cast · poll #{poll_id}"
    POLL_FOOTER_ENDED = "Closed poll #{poll_id} · {n} votes cast"
    POLL_NOT_FOUND = "No poll with id `{poll_id}` on this server."
    POLL_ENDED_OK = "Closed poll #{poll_id} ({n} votes)."
    POLL_VOTE_BUTTON_MALFORMED = "Poll vote button malformed."
    POLL_VOTE_CLOSED = "That poll is closed — `/poll status id: {poll_id}` shows the result."
    POLL_VOTE_RECORDED = "🗳 Voted **{label}**."
    POLL_VOTE_CHANGED = "🗳 Changed your vote to **{label}**."
    POLL_VOTE_NOOP = "You already voted for **{label}**."
    VOTE_FAILED = "Something went wrong recording your vote — please try again."

    # /aotw (v7.1.0).
    AOTW_NOT_ADMIN = (
        "Only server admins can run `/aotw {action}`. Ask a moderator with "
        "`Manage Server` or `Administrator`."
    )
    AOTW_ALREADY_ACTIVE = (
        "There's already an active anime-of-the-week vote (#{poll_id}). "
        "End it first with `/aotw end`, or check `/aotw status` for the "
        "current standings."
    )
    AOTW_NO_CANDIDATES = (
        "The server watchlist needs at least 2 entries before starting a vote. "
        "Add some with `/server-watchlist add anime: <title>`."
    )
    AOTW_NONE_ACTIVE = (
        "No anime-of-the-week vote is running right now. An admin can kick "
        "one off with `/aotw start`."
    )
    AOTW_USAGE = "Pick a subcommand: `start`, `status`, or `end`."
    AOTW_STARTED = "🎬 Anime of the Week — vote!"
    AOTW_STATUS_HEADER = "🎬 Anime of the Week — standings"
    AOTW_VOTE_BUTTON_MALFORMED = "Vote button malformed."
    AOTW_VOTE_POLL_CLOSED = (
        "That poll is closed — `/aotw status` shows the winner."
    )
    AOTW_VOTE_RECORDED = "🗳 Voted for **{title}**."
    AOTW_VOTE_CHANGED = "🗳 Changed your vote to **{title}**."
    AOTW_VOTE_NOOP = "You already voted for **{title}**."
    AOTW_FOOTER_VOTES = "{n} votes cast · poll #{poll_id}"
    AOTW_ENDED_WINNER = (
        "🏆 **{title}** wins this week's anime poll (#{poll_id}) with "
        "{votes} vote(s)!"
    )
    AOTW_ENDED_NO_VOTES = (
        "Poll #{poll_id} closed with no votes — nothing to announce. Start "
        "a fresh one with `/aotw start`."
    )
    AOTW_ANNOUNCE_FAIL = (
        "Closed poll #{poll_id} but couldn't post the winner announcement. "
        "Set the announcement channel with `/otaku-admin set-channel`."
    )

    # /review and /reviews (v7.0.0).
    REVIEW_NO_CACHE = (
        "You haven't looked up an anime yet. Try `/anime query: <title>` first."
    )
    REVIEW_NO_DATA = (
        "Couldn't load your last lookup right now — try `/anime query: <title>` to refresh it."
    )
    REVIEW_MODAL_TITLE = "Review · {title}"
    REVIEW_FIELD_TITLE = "Title"
    REVIEW_FIELD_BODY = "Body"
    REVIEW_PLACEHOLDER_TITLE = "Short summary — e.g. \"a quiet masterpiece\""
    REVIEW_PLACEHOLDER_BODY = "Your thoughts on the anime…"
    REVIEW_SUBMIT_MALFORMED = "Review form was malformed — try `/review` again."
    REVIEW_SUBMIT_EMPTY = "Review needs both a title and a body."
    REVIEW_SAVED_NEW = "📝 Posted your review of **{title}**."
    REVIEW_SAVED_EDIT = "📝 Updated your review of **{title}**."

    REVIEWS_USAGE = (
        "Pass `anime:` with a title or numeric AniList ID, or run "
        "`/anime query: <title>` first to set your last lookup."
    )
    REVIEWS_HEADER = "📝 Reviews · {title}"
    REVIEWS_EMPTY = (
        "No reviews yet for **{title}** on this server. Be the first with `/review`!"
    )
    REVIEWS_PAGE_MALFORMED = "Reviews button malformed."
    REVIEWS_FOOTER_PAGE = "Page {page}/{total} · {n} reviews on this server"

    # /genre-trends (v6.2.0).
    GENRE_TRENDS_EMPTY = (
        "You haven't tracked any anime yet. Try `/favorite` or `/watch` "
        "after a `/anime` lookup, then come back."
    )
    GENRE_TRENDS_NO_GENRES = (
        "Couldn't read your top tracked genres from AniList right now — "
        "try again in a moment."
    )
    GENRE_TRENDS_NO_FRESH = (
        "Everything trending in **{genres}** is already on your list! "
        "Try `/trending` for current-season top picks across all genres."
    )
    GENRE_TRENDS_HEADER = "📈 Trending in {genres}"
    GENRE_TRENDS_FOOTER = "Picked from your top {n} tracked genres"

    # /mood (v6.1.0).
    MOOD_UNKNOWN = (
        "Unknown mood **{feeling}**. Pick one of the suggested choices."
    )
    MOOD_NO_RESULTS = "No anime matched {label} — try another mood."
    MOOD_PAGE_MALFORMED = "Mood page button malformed."
    MOOD_HEADER = "{label} picks"
    MOOD_FOOTER_GENRES = "Genres: {genres}"
    MOOD_FOOTER_GENRES_TAGS = "Genres: {genres} · Tags: {tags}"

    # /find (v9.0.0).
    FIND_USAGE = "Describe what you want — e.g. `/find description: slow romance with magic`."
    FIND_NO_MATCH = (
        "I couldn't decode **{query}** into genres or tags. Try plain words "
        "like *slow*, *dark*, *romance*, *school*, *supernatural*, *epic*."
    )
    FIND_NO_RESULTS = (
        "AniList didn't have anything matching **{blend}**. Try a different "
        "description, or run `/mood` for a curated vibe."
    )
    FIND_HEADER = "🔎 Found for *{query}*"
    FIND_FOOTER = "Decoded as: {blend}"

    # /achievements (v10.0.0).
    ACHIEVEMENTS_HEADER_OWN = "🏅 Your achievements"
    ACHIEVEMENTS_HEADER_OTHER = "🏅 {who}'s achievements"
    ACHIEVEMENTS_NONE_OWN = (
        "You haven't earned any achievements yet. Track some anime, write a "
        "review, or rate something to start — `/achievements` re-checks every "
        "time you run it."
    )
    ACHIEVEMENTS_NONE_OTHER = "{who} hasn't earned any achievements yet."
    ACHIEVEMENTS_NEW_AWARDED = "✨ **Newly earned this check:** {names}"
    ACHIEVEMENTS_EARNED_HEADER = "✅ Earned"
    ACHIEVEMENTS_UNEARNED_HEADER = "⏳ In progress"
    ACHIEVEMENTS_FOOTER = "{earned}/{total} earned · rerun /achievements to re-check"

    # /preferences (v9.3.0).
    PREFERENCES_HEADER = "⚙️ Your preferences"
    PREFERENCES_NO_CHANGES = (
        "No preferences passed. Showing current values — pass `language:` "
        "and/or `spoilers:` to update."
    )
    PREFERENCES_UPDATED = "Updated preferences."
    PREFERENCES_LANG_NOT_SET = "*(not set)*"
    PREFERENCES_LANG_LINE = "🌐 **Language**: `{lang}`"
    PREFERENCES_SPOILERS_LINE = (
        "🙈 **Spoilers**: `{state}` *(hide = wrap detected spoiler "
        "markers; show = render plain text)*"
    )
    PREFERENCES_LANG_NOTE = (
        "Language preference is stored but doesn't translate yet — v9.3 set "
        "the foundation; a future SDK with a translation proxy will activate it."
    )


S = _Strings


# ── v10.0.0 — Localization (i18n) ────────────────────────────────────────────
#
# v1.4 set up the structural pattern: every user-facing string lives in
# `_Strings` (aliased as `S`). v9.3 added the `pref:lang:user:<id>` KV
# preference. v10 activates the pipeline: `T(key, lang=..., **fmt)` returns
# the translated string when a translation exists, falls back to English
# otherwise.
#
# Coverage is intentionally **partial** for v10.0.0 — we ship the
# infrastructure plus translations for the 20+ most-visible user-facing
# strings in Japanese and Spanish. Strings without a translation fall
# back to English transparently. v10.x can grow coverage incrementally
# without breaking any existing caller (T() always works; missing keys
# return the English value).
#
# Per the v1.4 deviation note, sibling .py modules can't live in the
# runtime zip's allowlist. So translations live inline as nested dicts.

# Each language maps `_Strings` attribute names to translated values.
# Missing keys fall back to the English value via T()'s default path.
TRANSLATIONS: dict[str, dict[str, str]] = {
    "ja": {
        "UNTITLED": "無題",
        "ANILIST_FAILURE_DEFAULT": "AniListが応答しません — 少し待ってもう一度試してください。",
        "COOLDOWN_WAIT": "少し落ち着いて — あと {remaining}秒で再試行できます。",
        "ANIME_USAGE": "使い方: `/anime query: <タイトル>`",
        "ANIME_NOT_FOUND": "**{query}** に一致するアニメが見つかりません。",
        "ANIME_NO_DESCRIPTION": "*(説明なし)*",
        "DISCOVER_USAGE": "使い方: `/discover genre: <名前> [sort: popular|trending|score]`",
        "DISCOVER_NO_RESULTS": "{genre} のアニメは見つかりませんでした。",
        "TRENDING_NO_RESULTS": "このシーズンのトレンドアニメは見つかりませんでした。",
        "SIMILAR_NO_RECS": "**{title}** の推奨はまだAniListにありません。",
        "SIMILAR_NO_CACHE": "まだアニメを検索していません。先に `/anime query: <タイトル>` を試してください。",
        "MANGA_USAGE": "使い方: `/manga query: <タイトル>`",
        "MANGA_NOT_FOUND": "**{query}** に一致するマンガが見つかりません。",
        "MANGA_NO_DESCRIPTION": "*(説明なし)*",
        "ACHIEVEMENTS_HEADER_OWN": "🏅 あなたの実績",
        "ACHIEVEMENTS_HEADER_OTHER": "🏅 {who} の実績",
        "FOOTER_ANILIST": "AniListより",
    },
    "es": {
        "UNTITLED": "Sin título",
        "ANILIST_FAILURE_DEFAULT": (
            "AniList no responde — inténtalo de nuevo en un momento, o prueba con otra palabra clave."
        ),
        "COOLDOWN_WAIT": "Baja el ritmo — inténtalo de nuevo en {remaining}s.",
        "ANIME_USAGE": "Uso: `/anime query: <título>`",
        "ANIME_NOT_FOUND": "No se encontró ningún anime que coincida con **{query}**.",
        "ANIME_NO_DESCRIPTION": "*(sin descripción)*",
        "DISCOVER_USAGE": "Uso: `/discover genre: <nombre> [sort: popular|trending|score]`",
        "DISCOVER_NO_RESULTS": "No se encontraron animes del género {genre}.",
        "TRENDING_NO_RESULTS": "No hay animes en tendencia esta temporada.",
        "SIMILAR_NO_RECS": "AniList aún no tiene recomendaciones para **{title}**.",
        "SIMILAR_NO_CACHE": (
            "Aún no has buscado un anime. Prueba `/anime query: <título>` primero."
        ),
        "MANGA_USAGE": "Uso: `/manga query: <título>`",
        "MANGA_NOT_FOUND": "No se encontró ningún manga que coincida con **{query}**.",
        "MANGA_NO_DESCRIPTION": "*(sin descripción)*",
        "ACHIEVEMENTS_HEADER_OWN": "🏅 Tus logros",
        "ACHIEVEMENTS_HEADER_OTHER": "🏅 Logros de {who}",
        "FOOTER_ANILIST": "Datos de AniList",
    },
    # Other PREF_LANGUAGE_CHOICES (ko, zh, de, fr) declare the user prefers
    # those locales but have no translations yet. T() falls through to
    # English for any key not present in TRANSLATIONS[lang]. v10.x patches
    # can fill these in without any code change beyond adding entries.
}


def T(key: str, *, lang: str | None = None, **fmt) -> str:
    """Translate a string by `_Strings` attribute name.

    If `lang` has a TRANSLATIONS entry AND that entry has the requested
    key, return the translated value formatted with **fmt. Otherwise fall
    back to the English value from `_Strings`. Missing English keys raise
    AttributeError — that's a caller bug, not a translation gap.
    """
    english = getattr(_Strings, key)
    if lang and lang in TRANSLATIONS and key in TRANSLATIONS[lang]:
        template = TRANSLATIONS[lang][key]
    else:
        template = english
    return template.format(**fmt) if fmt else template


def T_for(ctx: Context, user_id: str, key: str, **fmt) -> str:
    """Per-user translator. Looks up the viewer's language preference
    from KV and routes through T(). The common per-handler shape — call
    this instead of S.KEY where a localised surface is wanted."""
    lang = _get_pref_language(ctx, user_id) if user_id else None
    return T(key, lang=lang, **fmt)


ANILIST_URL = "https://graphql.anilist.co"
ANILIST_COLOR = 0x02A9FF
PER_PAGE = 5
DESC_MAX = 350
COOLDOWN_SECONDS = 2
LAST_ANIME_TTL = 7 * 24 * 60 * 60  # 7 days
ANILIST_CACHE_TTL = 5 * 60          # 5 minutes — short enough that fresh trends still update
ANILIST_CACHE_MAX_ENTRIES = 256     # v9.1: bumped from 128 — multi-source widens the working set

# v9.1.0 — multi-source aggregation. AniList stays primary; MAL (Jikan v4) and
# Kitsu are secondary fallbacks for /anime + /manga search. Each source has
# its own rate-limit posture; the per-source token buckets in _RATE_BUCKETS
# (declared later, near _rate_acquire) keep the plugin honest.
JIKAN_BASE_URL = "https://api.jikan.moe/v4"
KITSU_BASE_URL = "https://kitsu.io/api/edge"

# Per-source rate limits as (max_requests, window_seconds) tuples. v9.1's
# _rate_acquire enforces these in-process; multi-tenant pool workers all
# share the same bucket per-process (which is what we want — the limit is
# per source IP, not per server).
SOURCE_RATE_LIMITS = {
    "anilist": (90, 60),    # AniList: 90 req/min
    "jikan":   (3, 1),      # Jikan v4: 3 req/sec (the tight constraint)
    "kitsu":   (10, 1),     # Kitsu: 10 req/sec (loosest)
}

# Per-source attribution labels surfaced in embed footers + button labels.
SOURCE_LABEL = {
    "anilist": "AniList",
    "mal":     "MyAnimeList",
    "kitsu":   "Kitsu",
}

# Canonical media dict has source: "anilist" | "mal" | "kitsu". (Note: source
# string for Jikan responses is "mal" — Jikan is the API, MAL is the dataset.)

# v2.0.0 — per-user anime tracking.
VALID_STATUSES = ("watching", "completed", "on_hold", "dropped", "plan")
STATUS_EMOJI = {
    "watching":  "📺",
    "completed": "✅",
    "on_hold":   "⏸",
    "dropped":   "❌",
    "plan":      "📌",
}
STATUS_LABEL = {
    "watching":  "Watching",
    "completed": "Completed",
    "on_hold":   "On hold",
    "dropped":   "Dropped",
    "plan":      "Plan to watch",
}

# v4.0.0 — airing notifications.
NOTIFY_CHANNEL_KV = "notify_channel:guild"
# How far ahead the cron looks for airings each run. With an hourly schedule
# this is 60min, but we sweep slightly wider so a delayed cron doesn't miss
# anything. The ephemeral dedup keeps a single airing from being announced twice.
NOTIFY_LOOKAHEAD_SECONDS = 75 * 60
NOTIFY_DEDUP_TTL = 24 * 60 * 60  # 24h — one airing only pings once per day

# Retry budget for AniList transient failures (RpcTimeoutError, 5xx).
# Sleeps 0.5s then 1.5s — total worst case 2s, well under the 15-min followup
# window. RateLimitError is never retried; ValidationError isn't retryable.
ANILIST_RETRY_BACKOFFS_S = (0.5, 1.5)

# Substrings that mean "the user can fix this" — surface these to them instead
# of the generic "AniList didn't answer" line.
_USER_FIXABLE_ANILIST_FRAGMENTS = (
    "must contain at least",
    "must be a string",
    "field name",
    "is not a valid",
)
SORT_MAP = {
    "popular": "POPULARITY_DESC",
    "trending": "TRENDING_DESC",
    "score": "SCORE_DESC",
}

# Shared GraphQL fragment for the fields every embed needs.
_MEDIA_FIELDS = """
  id
  title { romaji english }
  description(asHtml: false)
  coverImage { large }
  bannerImage
  averageScore
  popularity
  format
  episodes
  status
  season
  seasonYear
  genres
  siteUrl
"""

QUERY_SEARCH_ONE = (
    "query ($q: String) {"
    "  Media(search: $q, type: ANIME) {" + _MEDIA_FIELDS + "}"
    "}"
)

QUERY_DISCOVER = (
    "query ($genre: String, $sort: [MediaSort], $page: Int, $perPage: Int) {"
    "  Page(page: $page, perPage: $perPage) {"
    "    pageInfo { hasNextPage currentPage }"
    "    media(type: ANIME, genre: $genre, sort: $sort) {" + _MEDIA_FIELDS + "}"
    "  }"
    "}"
)

QUERY_SEASON = (
    "query ($season: MediaSeason, $year: Int, $sort: [MediaSort], $page: Int, $perPage: Int) {"
    "  Page(page: $page, perPage: $perPage) {"
    "    pageInfo { hasNextPage currentPage }"
    "    media(type: ANIME, season: $season, seasonYear: $year, sort: $sort) {"
    + _MEDIA_FIELDS +
    "    }"
    "  }"
    "}"
)

QUERY_SIMILAR_BY_ID = (
    "query ($id: Int) {"
    "  Media(id: $id, type: ANIME) {"
    "    id"
    "    title { romaji english }"
    "    recommendations(sort: RATING_DESC, perPage: 5) {"
    "      nodes {"
    "        mediaRecommendation {" + _MEDIA_FIELDS + "}"
    "      }"
    "    }"
    "  }"
    "}"
)

QUERY_MEDIA_BY_ID = (
    "query ($id: Int) {"
    "  Media(id: $id, type: ANIME) {" + _MEDIA_FIELDS + "}"
    "}"
)

QUERY_SIMILAR_BY_NAME = (
    "query ($q: String) {"
    "  Media(search: $q, type: ANIME) {"
    "    id"
    "    title { romaji english }"
    "    recommendations(sort: RATING_DESC, perPage: 5) {"
    "      nodes {"
    "        mediaRecommendation {" + _MEDIA_FIELDS + "}"
    "      }"
    "    }"
    "  }"
    "}"
)

# /random — peek at lastPage so we can pick a random page within range.
QUERY_RANDOM_META = (
    "query ($genre: String) {"
    "  Page(page: 1, perPage: 1) {"
    "    pageInfo { lastPage hasNextPage }"
    "    media(type: ANIME, genre: $genre, sort: POPULARITY_DESC) { id }"
    "  }"
    "}"
)

QUERY_RANDOM_PICK = (
    "query ($genre: String, $page: Int) {"
    "  Page(page: $page, perPage: 1) {"
    "    media(type: ANIME, genre: $genre, sort: POPULARITY_DESC) {" + _MEDIA_FIELDS + "}"
    "  }"
    "}"
)

QUERY_GENRES = "query { GenreCollection }"

# v6.2.0 — /genre-trends. Trending-by-genre, used to bridge personalization
# and discovery: filter AniList's trending list to the genres the user
# already gravitates toward.
QUERY_GENRE_TRENDS = (
    "query ($genres: [String], $perPage: Int) {"
    "  Page(page: 1, perPage: $perPage) {"
    "    media(type: ANIME, genre_in: $genres, sort: [TRENDING_DESC]) {"
    + _MEDIA_FIELDS + "}"
    "  }"
    "}"
)

# v6.1.0 — /mood. Two query shapes because AniList rejects empty list
# arguments to its in-filters; when a mood has no tag enrichment we use the
# genre-only variant rather than sending `tag_in: []`.
QUERY_MOOD_WITH_TAGS = (
    "query ($genres: [String], $tags: [String], $page: Int, $perPage: Int) {"
    "  Page(page: $page, perPage: $perPage) {"
    "    pageInfo { hasNextPage currentPage }"
    "    media(type: ANIME, genre_in: $genres, tag_in: $tags,"
    "          sort: [POPULARITY_DESC]) {" + _MEDIA_FIELDS + "}"
    "  }"
    "}"
)

QUERY_MOOD_GENRE_ONLY = (
    "query ($genres: [String], $page: Int, $perPage: Int) {"
    "  Page(page: $page, perPage: $perPage) {"
    "    pageInfo { hasNextPage currentPage }"
    "    media(type: ANIME, genre_in: $genres, sort: [POPULARITY_DESC]) {"
    + _MEDIA_FIELDS + "}"
    "  }"
    "}"
)

# /list and /favorites — batch fetch titles for up to PER_PAGE media_ids in one round trip.
QUERY_MEDIA_BATCH = (
    "query ($ids: [Int]) {"
    "  Page(perPage: 50) {"
    "    media(id_in: $ids, type: ANIME) {" + _MEDIA_FIELDS + "}"
    "  }"
    "}"
)

# v8.0.0 — manga query constants. Parallel to the anime constants above; we
# keep them separate (rather than parameterising on $mediaType) because each
# query family has type-specific fields (anime has episodes+season, manga has
# chapters+volumes+startDate). _MEDIA_FIELDS_MANGA replaces the episodes/season
# trio with chapters/volumes/startDate.year so embed rendering stays clean.
_MEDIA_FIELDS_MANGA = """
  id
  title { romaji english }
  description(asHtml: false)
  coverImage { large }
  bannerImage
  averageScore
  popularity
  format
  chapters
  volumes
  status
  startDate { year }
  genres
  siteUrl
"""

QUERY_MANGA_SEARCH_ONE = (
    "query ($q: String) {"
    "  Media(search: $q, type: MANGA) {" + _MEDIA_FIELDS_MANGA + "}"
    "}"
)

QUERY_MANGA_DISCOVER = (
    "query ($genre: String, $sort: [MediaSort], $page: Int, $perPage: Int) {"
    "  Page(page: $page, perPage: $perPage) {"
    "    pageInfo { hasNextPage currentPage }"
    "    media(type: MANGA, genre: $genre, sort: $sort) {" + _MEDIA_FIELDS_MANGA + "}"
    "  }"
    "}"
)

QUERY_MANGA_BY_ID = (
    "query ($id: Int) {"
    "  Media(id: $id, type: MANGA) {" + _MEDIA_FIELDS_MANGA + "}"
    "}"
)

QUERY_MANGA_BATCH = (
    "query ($ids: [Int]) {"
    "  Page(perPage: 50) {"
    "    media(id_in: $ids, type: MANGA) {" + _MEDIA_FIELDS_MANGA + "}"
    "  }"
    "}"
)

# v4.0.0 — airing schedule lookup for the notification cron.
QUERY_AIRING_WINDOW = (
    "query ($at_gte: Int, $at_lte: Int) {"
    "  Page(page: 1, perPage: 50) {"
    "    pageInfo { hasNextPage }"
    "    airingSchedules(airingAt_greater: $at_gte, airingAt_lesser: $at_lte) {"
    "      id"
    "      episode"
    "      airingAt"
    "      media {"
    "        id"
    "        episodes"
    "        siteUrl"
    "        title { romaji english }"
    "        coverImage { large }"
    "      }"
    "    }"
    "  }"
    "}"
)

# v2.4.0 — paginated user-list import.
QUERY_USER_MEDIALIST_PAGE = (
    "query ($userName: String, $page: Int) {"
    "  Page(page: $page, perPage: 50) {"
    "    pageInfo { hasNextPage currentPage }"
    "    mediaList(userName: $userName, type: ANIME) {"
    "      status progress score(format: POINT_10)"
    "      media { id }"
    "    }"
    "  }"
    "}"
)

QUERY_CHARACTER = (
    "query ($q: String) {"
    "  Character(search: $q) {"
    "    id"
    "    name { full native }"
    "    image { large }"
    "    description(asHtml: false)"
    "    siteUrl"
    "    media(perPage: 5, sort: POPULARITY_DESC) {"
    "      nodes { id title { romaji english } siteUrl }"
    "    }"
    "  }"
    "}"
)

# v8.3.0 — popular-character leaderboard. AniList's Page.characters with
# `sort: FAVOURITES_DESC` gives a global popularity ranking. We fetch the
# parent media's most-popular title so each character row links somewhere
# users recognise.
QUERY_CHARACTER_POPULAR = (
    "query ($page: Int, $perPage: Int) {"
    "  Page(page: $page, perPage: $perPage) {"
    "    pageInfo { hasNextPage currentPage }"
    "    characters(sort: FAVOURITES_DESC) {"
    "      id"
    "      name { full native }"
    "      image { large }"
    "      favourites"
    "      siteUrl"
    "      media(perPage: 1, sort: POPULARITY_DESC) {"
    "        nodes { id title { romaji english } siteUrl }"
    "      }"
    "    }"
    "  }"
    "}"
)


# v8.2.0 — studio lookup. AniList's `Studio` type carries a name and a
# `media` connection sorted by popularity. We fetch the top 10 popular works
# in a single query; the embed renders both popular works AND splits out
# any from the current/recent year as a separate "recent" section.
QUERY_STUDIO = (
    "query ($q: String) {"
    "  Studio(search: $q) {"
    "    id"
    "    name"
    "    isAnimationStudio"
    "    siteUrl"
    "    media(perPage: 10, sort: POPULARITY_DESC, isMain: true) {"
    "      nodes {"
    "        id"
    "        title { romaji english }"
    "        format"
    "        season"
    "        seasonYear"
    "        status"
    "        episodes"
    "        siteUrl"
    "      }"
    "    }"
    "  }"
    "}"
)

# v8.1.0 — staff lookups. AniList's `Staff` type covers both voice actors
# AND production staff (directors, writers, composers). The same query
# powers /voice-actor and /staff; the embed builders pull different fields:
# /voice-actor leans on `characters` (top characters they voice + parent
# media); /staff leans on `staffMedia` (production credits with role).
QUERY_STAFF = (
    "query ($q: String) {"
    "  Staff(search: $q) {"
    "    id"
    "    name { full native }"
    "    image { large }"
    "    description(asHtml: false)"
    "    primaryOccupations"
    "    languageV2"
    "    siteUrl"
    "    characters(perPage: 5, sort: FAVOURITES_DESC) {"
    "      nodes {"
    "        id"
    "        name { full }"
    "        media(perPage: 1, sort: POPULARITY_DESC) {"
    "          nodes { id title { romaji english } siteUrl }"
    "        }"
    "      }"
    "    }"
    "    staffMedia(perPage: 5, sort: POPULARITY_DESC) {"
    "      edges {"
    "        staffRole"
    "        node { id title { romaji english } siteUrl }"
    "      }"
    "    }"
    "  }"
    "}"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_html(text: str | None) -> str:
    """Remove HTML tags and entities from an AniList description."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#039;", "'").replace("&nbsp;", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# v9.3.0 — spoiler redaction. Explicit-marker based to keep false positives
# low. Wraps `SPOILER: …` / `[SPOILER] …` / `(spoiler) …` style lines in
# Discord's `||spoiler||` syntax so the spoiler renders as a click-to-reveal.
# Already-wrapped content is left alone (idempotent). The opt-out is per-
# user via `pref:spoilers:user:<id>` in KV (default "hide"; "show" disables).
_SPOILER_LINE_RE = re.compile(
    r"^(?P<prefix>\s*(?:spoiler|spoilers|\[spoiler\]|\(spoiler\)|#\s*spoiler)\s*[:\-]?\s*)"
    r"(?P<body>.*)",
    re.IGNORECASE,
)


def _redact_spoilers(text: str, *, show_unhidden: bool = False) -> str:
    """Wrap detected spoiler markers in Discord ||...|| syntax.

    If show_unhidden=True (caller has opted out via /preferences), returns
    text unchanged. Detection is explicit-marker based: a line beginning
    with SPOILER:, [SPOILER], (spoiler), or # spoiler (case-insensitive)
    has its body wrapped. Already-wrapped lines are left alone — this
    function is idempotent.

    We deliberately DON'T try content-based heuristics ("dies", "twist",
    "secretly") to keep false positives low. Users who want to mark
    arbitrary content as spoilers can use Discord's `||…||` syntax
    themselves; this function preserves that and never double-wraps.
    """
    if show_unhidden or not text:
        return text
    out: list[str] = []
    for line in text.splitlines():
        m = _SPOILER_LINE_RE.match(line)
        if not m:
            out.append(line)
            continue
        body = m.group("body").strip()
        if not body:
            # Bare "SPOILER:" with no content — keep as-is.
            out.append(line)
            continue
        if "||" in body:
            # User already wrapped something; don't double-wrap.
            out.append(line)
            continue
        out.append(f"{m.group('prefix')}||{body}||")
    return "\n".join(out)


# v9.3.0 — per-user preferences in KV. The scope-locked v9.3 set is just
# language (placeholder for future translation proxy) and spoilers.
PREF_LANGUAGE_KV = "pref:lang:user"      # f"{PREF_LANGUAGE_KV}:{user_id}"
PREF_SPOILERS_KV = "pref:spoilers:user"  # f"{PREF_SPOILERS_KV}:{user_id}"
PREF_LANGUAGE_CHOICES = ("en", "ja", "ko", "zh", "es", "de", "fr")
PREF_SPOILERS_CHOICES = ("hide", "show")
PREF_SPOILERS_DEFAULT = "hide"


def _get_pref_spoilers(ctx: Context, user_id: str) -> str:
    """Return the user's spoiler-hiding preference. Default 'hide'."""
    if not user_id:
        return PREF_SPOILERS_DEFAULT
    try:
        val = ctx.kv.get(f"{PREF_SPOILERS_KV}:{user_id}")
    except Exception:  # noqa: BLE001
        return PREF_SPOILERS_DEFAULT
    if isinstance(val, str) and val in PREF_SPOILERS_CHOICES:
        return val
    return PREF_SPOILERS_DEFAULT


def _get_pref_language(ctx: Context, user_id: str) -> str | None:
    """Return the user's language code, or None if unset."""
    if not user_id:
        return None
    try:
        val = ctx.kv.get(f"{PREF_LANGUAGE_KV}:{user_id}")
    except Exception:  # noqa: BLE001
        return None
    if isinstance(val, str) and val in PREF_LANGUAGE_CHOICES:
        return val
    return None



def _option_map(event: dict) -> dict:
    """Slash command options arrive as a list of {name, value}; flatten.

    v10.0.6: the live host emits `command_options`, but the SDK testing
    helper and the original payload used `options`. Read whichever is
    populated so handlers work in both environments.
    """
    opts = event.get("command_options")
    if not opts:
        opts = event.get("options") or []
    if isinstance(opts, dict):
        return opts
    return {o["name"]: o["value"] for o in opts if isinstance(o, dict) and "name" in o}


def _current_season() -> tuple[str, int]:
    """AniList season string + year for today (UTC)."""
    now = datetime.now(timezone.utc)
    month = now.month
    if month <= 3:
        return "WINTER", now.year
    if month <= 6:
        return "SPRING", now.year
    if month <= 9:
        return "SUMMER", now.year
    return "FALL", now.year


def _format_title(media: dict) -> str:
    title = media.get("title") or {}
    romaji = (title.get("romaji") or "").strip()
    english = (title.get("english") or "").strip()
    if english and romaji and english.lower() != romaji.lower():
        return f"{romaji} ({english})"
    return romaji or english or S.UNTITLED


def _score(media: dict) -> str:
    avg = media.get("averageScore")
    if avg is None:
        return "—"
    return f"{avg / 10:.1f}/10"


def _season_field(media: dict) -> str:
    season = media.get("season")
    year = media.get("seasonYear")
    if season and year:
        return f"{season.title()} {year}"
    if year:
        return str(year)
    return "—"


def _make_anime_embed(
    media: dict,
    *,
    user_progress: int | None = None,
    user_rating: int | None = None,
) -> dict:
    """Full anime card — used by /anime and the expand-from-list select.

    If `user_progress` is a positive int, a "Your progress" field is appended
    so users can see where they left off without running /list. If
    `user_rating` is a SMALLINT (2..20), a "Your rating" field is shown
    alongside.
    """
    title = _format_title(media)
    site_url = media.get("siteUrl") or None
    description = _truncate(_strip_html(media.get("description")), DESC_MAX) or S.ANIME_NO_DESCRIPTION
    genres = (media.get("genres") or [])[:5]

    embed: dict = {
        "title": title,
        "url": site_url,
        "description": description,
        "color": ANILIST_COLOR,
        "fields": [
            {"name": "Score", "value": _score(media), "inline": True},
            {"name": "Popularity", "value": f"{media.get('popularity') or 0:,}", "inline": True},
            {"name": "Format", "value": (media.get("format") or "—").replace("_", " ").title(), "inline": True},
            {"name": "Episodes", "value": str(media.get("episodes") or "—"), "inline": True},
            {"name": "Status", "value": (media.get("status") or "—").replace("_", " ").title(), "inline": True},
            {"name": "Season", "value": _season_field(media), "inline": True},
        ],
        "footer": {"text": S.FOOTER_ANILIST},
    }
    if genres:
        embed["fields"].append({"name": "Genres", "value": ", ".join(genres), "inline": False})
    if user_progress and user_progress > 0:
        total = media.get("episodes")
        if isinstance(total, int) and total > 0:
            value = S.PROGRESS_FIELD_VALUE_BOUNDED.format(episodes=user_progress, total=total)
        else:
            value = S.PROGRESS_FIELD_VALUE_UNBOUNDED.format(episodes=user_progress)
        embed["fields"].append({"name": S.PROGRESS_FIELD_NAME, "value": value, "inline": True})
    if user_rating is not None and user_rating > 0:
        embed["fields"].append({
            "name": S.RATING_FIELD_NAME,
            "value": f"🎯 {_format_rating(user_rating)}/10",
            "inline": True,
        })
    cover = (media.get("coverImage") or {}).get("large")
    if cover:
        embed["thumbnail"] = {"url": cover}
    banner = media.get("bannerImage")
    if banner:
        embed["image"] = {"url": banner}
    return embed


def _make_manga_embed(
    media: dict,
    *,
    user_progress: int | None = None,
    user_rating: int | None = None,
) -> dict:
    """Full manga card — used by /manga and (future) /manga-expand select.

    Mirrors _make_anime_embed but swaps episodes/season for chapters/volumes/
    startDate.year — the dimensions AniList actually populates for manga.
    `user_progress` is interpreted as chapters watched/read; `user_rating`
    keeps the SMALLINT×2 scale from the anime path.
    """
    title = _format_title(media)
    site_url = media.get("siteUrl") or None
    description = _truncate(_strip_html(media.get("description")), DESC_MAX) or S.MANGA_NO_DESCRIPTION
    genres = (media.get("genres") or [])[:5]
    start_year = ((media.get("startDate") or {}).get("year")) or "—"

    embed: dict = {
        "title": title,
        "url": site_url,
        "description": description,
        "color": ANILIST_COLOR,
        "fields": [
            {"name": "Score", "value": _score(media), "inline": True},
            {"name": "Popularity", "value": f"{media.get('popularity') or 0:,}", "inline": True},
            {"name": "Format", "value": (media.get("format") or "—").replace("_", " ").title(), "inline": True},
            {"name": "Chapters", "value": str(media.get("chapters") or "—"), "inline": True},
            {"name": "Volumes", "value": str(media.get("volumes") or "—"), "inline": True},
            {"name": "Started", "value": str(start_year), "inline": True},
        ],
        "footer": {"text": S.FOOTER_ANILIST},
    }
    if genres:
        embed["fields"].append({"name": "Genres", "value": ", ".join(genres), "inline": False})
    if user_progress and user_progress > 0:
        total = media.get("chapters")
        if isinstance(total, int) and total > 0:
            value = S.MANGA_PROGRESS_BOUNDED.format(chapters=user_progress, total=total)
        else:
            value = S.MANGA_PROGRESS_UNBOUNDED.format(chapters=user_progress)
        embed["fields"].append({"name": S.PROGRESS_FIELD_NAME, "value": value, "inline": True})
    if user_rating is not None and user_rating > 0:
        embed["fields"].append({
            "name": S.RATING_FIELD_NAME,
            "value": f"🎯 {_format_rating(user_rating)}/10",
            "inline": True,
        })
    cover = (media.get("coverImage") or {}).get("large")
    if cover:
        embed["thumbnail"] = {"url": cover}
    banner = media.get("bannerImage")
    if banner:
        embed["image"] = {"url": banner}
    return embed


# ── v9.1.0 — canonical media dict + per-source adapters ─────────────────────
#
# The canonical dict reuses AniList's key shape (title.romaji/english,
# coverImage.large, averageScore as 0..100, siteUrl, etc.) so the v1–v8
# embed builders keep working without rewriting. Each adapter annotates
# the dict with a `source` field ("anilist" | "mal" | "kitsu") and a
# `source_id` for the native ID at that source. Embed builders read the
# `source` field to render the right "Open on X" button label and footer.
#
# Why AniList-shaped instead of a fresh snake_case schema: keeps the
# blast radius of v9.1 small. The audit's option 4 — repoint embed
# builders onto a canonical dict — works for either shape; the
# AniList-shape choice means MAL/Kitsu adapters do the rewriting work
# instead of every existing caller.


def _canonicalize_anilist_media(raw: dict | None) -> dict | None:
    """Mark an AniList media dict as canonical-source-tagged.

    AniList responses already satisfy the canonical contract; this just
    stamps the source + source_id annotations so downstream code can
    distinguish anime from MAL/Kitsu fallbacks. Returns None if the
    input is None or doesn't have an `id`.
    """
    if not raw or not isinstance(raw, dict) or raw.get("id") is None:
        return None
    out = dict(raw)
    out["source"] = "anilist"
    out["source_id"] = raw["id"]
    return out


def _canonicalize_jikan_media(raw: dict | None) -> dict | None:
    """Map a Jikan v4 (MyAnimeList) media object onto the AniList-shaped
    canonical dict.

    Jikan REST shape (per https://docs.api.jikan.moe/, anime endpoint):
      {mal_id, url, title, title_english, title_japanese,
       images: {jpg: {large_image_url, ...}, webp: {...}},
       synopsis, score (0..10), members, scored_by, type, episodes,
       status, aired: {from: "YYYY-MM-DD...", to: ...},
       season ("winter"|...), year, genres: [{name}, ...]}

    Manga differs slightly (chapters, volumes; no season/year/episodes).
    The function handles both — type-irrelevant fields just stay None.
    """
    if not raw or not isinstance(raw, dict) or raw.get("mal_id") is None:
        return None

    images = raw.get("images") or {}
    jpg = images.get("jpg") or {}
    cover_url = jpg.get("large_image_url") or jpg.get("image_url")

    # Jikan returns score as a float 0..10. Rescale to AniList's 0..100.
    score = raw.get("score")
    score_pct = int(round(score * 10)) if isinstance(score, (int, float)) and score else None

    # Jikan's `season` is lowercase ("winter"); AniList uses uppercase.
    season = (raw.get("season") or "").upper() or None

    genres = [g.get("name") for g in (raw.get("genres") or []) if isinstance(g, dict)]

    return {
        # Annotate the source.
        "source": "mal",
        "source_id": raw["mal_id"],
        # AniList-shaped fields that the embed builders read.
        "id": raw["mal_id"],
        "title": {
            "romaji":  raw.get("title") or "",
            "english": raw.get("title_english") or "",
            "native":  raw.get("title_japanese") or "",
        },
        "description": raw.get("synopsis") or "",
        "coverImage": {"large": cover_url or ""},
        "bannerImage": None,
        "averageScore": score_pct,
        "popularity":   raw.get("members"),
        "format":       (raw.get("type") or "").upper().replace(" ", "_") or None,
        "episodes":     raw.get("episodes"),
        "chapters":     raw.get("chapters"),
        "volumes":      raw.get("volumes"),
        "status":       (raw.get("status") or "").upper().replace(" ", "_") or None,
        "season":       season,
        "seasonYear":   raw.get("year"),
        "startDate":    {"year": raw.get("year")},
        "genres":       [g for g in genres if g],
        "siteUrl":      raw.get("url") or f"https://myanimelist.net/anime/{raw['mal_id']}",
    }


def _canonicalize_kitsu_media(raw: dict | None) -> dict | None:
    """Map a Kitsu JSON:API media object onto the AniList-shaped canonical dict.

    Kitsu JSON:API shape (per https://kitsu.docs.apiary.io/):
      {id, type: "anime"|"manga",
       attributes: {canonicalTitle, titles: {en, en_jp, ja_jp},
                    synopsis, posterImage: {original, large, medium, ...},
                    coverImage: {original, ...} (banner-like),
                    averageRating (0..100 as string), userCount,
                    showType / mangaType, episodeCount, chapterCount,
                    volumeCount, status, startDate ("YYYY-MM-DD")}}
    """
    if not raw or not isinstance(raw, dict) or raw.get("id") is None:
        return None

    attrs = raw.get("attributes") or {}
    titles = attrs.get("titles") or {}

    poster = attrs.get("posterImage") or {}
    cover_url = poster.get("large") or poster.get("original") or poster.get("medium")
    # Kitsu's coverImage is a banner-style hero image, NOT the thumbnail.
    cover = attrs.get("coverImage") or {}
    banner_url = cover.get("original") or cover.get("large")

    # Kitsu's averageRating is a string "0".."100". Parse defensively.
    rating_raw = attrs.get("averageRating")
    score_pct: int | None
    try:
        score_pct = int(round(float(rating_raw))) if rating_raw else None
    except (TypeError, ValueError):
        score_pct = None

    # Year extraction from "YYYY-MM-DD".
    start_date = (attrs.get("startDate") or "")
    year: int | None
    if isinstance(start_date, str) and len(start_date) >= 4 and start_date[:4].isdigit():
        year = int(start_date[:4])
    else:
        year = None

    show_type = (attrs.get("showType") or attrs.get("mangaType") or "").upper() or None

    return {
        "source": "kitsu",
        "source_id": raw["id"],
        "id": raw["id"],  # Kitsu uses string IDs
        "title": {
            "romaji":  titles.get("en_jp") or attrs.get("canonicalTitle") or "",
            "english": titles.get("en") or "",
            "native":  titles.get("ja_jp") or "",
        },
        "description": attrs.get("synopsis") or "",
        "coverImage": {"large": cover_url or ""},
        "bannerImage": banner_url or None,
        "averageScore": score_pct,
        "popularity":   attrs.get("userCount"),
        "format":       show_type,
        "episodes":     attrs.get("episodeCount"),
        "chapters":     attrs.get("chapterCount"),
        "volumes":      attrs.get("volumeCount"),
        "status":       (attrs.get("status") or "").upper().replace(" ", "_") or None,
        "season":       None,  # Kitsu doesn't expose season directly
        "seasonYear":   year,
        "startDate":    {"year": year},
        "genres":       [],  # Kitsu requires a separate include for genres; v9.1 skips
        # v10.0.12 — JSON:API puts `type` at the resource top level and `slug`
        # inside attributes; the levels were swapped, so manga links pointed
        # at /anime/ and the slug never resolved (id fallback masked it).
        "siteUrl":      f"https://kitsu.io/{(raw.get('type') or 'anime')}/{attrs.get('slug') or raw['id']}",
    }


def _make_character_embed(char: dict) -> dict:
    """AniList character card — name, image, description, top media."""
    name = (char.get("name") or {})
    full = (name.get("full") or "").strip()
    native = (name.get("native") or "").strip()
    if full and native and full != native:
        title = f"{full} ({native})"
    else:
        title = full or native or S.CHARACTER_UNKNOWN_NAME

    description = _truncate(_strip_html(char.get("description")), DESC_MAX) or S.CHARACTER_NO_DESCRIPTION
    site_url = char.get("siteUrl") or None
    embed: dict = {
        "title": title,
        "url": site_url,
        "description": description,
        "color": ANILIST_COLOR,
        "footer": {"text": S.FOOTER_CHARACTER},
    }
    image = (char.get("image") or {}).get("large")
    if image:
        embed["thumbnail"] = {"url": image}

    media_nodes = ((char.get("media") or {}).get("nodes")) or []
    if media_nodes:
        lines = []
        for m in media_nodes[:5]:
            mtitle = _format_title(m)
            url = m.get("siteUrl") or ""
            lines.append(f"• [{mtitle}]({url})" if url else f"• {mtitle}")
        embed["fields"] = [{"name": "Appears in", "value": "\n".join(lines), "inline": False}]
    return embed


def _staff_display_name(staff: dict) -> str:
    """Render a Staff person's display name — `Full (Native)` if both present."""
    name = staff.get("name") or {}
    full = (name.get("full") or "").strip()
    native = (name.get("native") or "").strip()
    if full and native and full != native:
        return f"{full} ({native})"
    return full or native or "Unknown"


def _make_voice_actor_embed(staff: dict) -> dict:
    """Voice-actor card — bio + top character roles (each with parent media)."""
    title = _staff_display_name(staff)
    site_url = staff.get("siteUrl") or None
    description = _truncate(_strip_html(staff.get("description")), DESC_MAX) or S.VOICE_ACTOR_NO_DESCRIPTION

    embed: dict = {
        "title": title,
        "url": site_url,
        "description": description,
        "color": ANILIST_COLOR,
        "footer": {"text": S.FOOTER_CHARACTER},
    }
    image = (staff.get("image") or {}).get("large")
    if image:
        embed["thumbnail"] = {"url": image}

    # Language field — AniList exposes the VA's primary dub language as
    # `languageV2`. Most-common values: Japanese, English, Korean, Mandarin.
    language = (staff.get("languageV2") or "").strip()
    fields: list[dict] = []
    if language:
        fields.append({"name": "Language", "value": language, "inline": True})

    # Notable characters — five most-favourited, each with their single most-
    # popular parent media. Render as "Character (Parent Title)".
    char_nodes = ((staff.get("characters") or {}).get("nodes")) or []
    if char_nodes:
        lines = []
        for c in char_nodes[:5]:
            cname = (c.get("name") or {}).get("full") or "?"
            parent = (((c.get("media") or {}).get("nodes")) or [None])[0]
            if parent:
                ptitle = _format_title(parent)
                purl = parent.get("siteUrl") or ""
                anchor = f"[{ptitle}]({purl})" if purl else ptitle
                lines.append(f"• **{cname}** — {anchor}")
            else:
                lines.append(f"• **{cname}**")
        fields.append({"name": "Notable roles", "value": "\n".join(lines), "inline": False})
    else:
        fields.append({"name": "Notable roles", "value": S.VOICE_ACTOR_NO_ROLES, "inline": False})

    embed["fields"] = fields
    return embed


def _make_staff_embed(staff: dict) -> dict:
    """Production-staff card — bio + production credits with role (director,
    writer, etc.). Same Staff object as /voice-actor; different framing."""
    title = _staff_display_name(staff)
    site_url = staff.get("siteUrl") or None
    description = _truncate(_strip_html(staff.get("description")), DESC_MAX) or S.STAFF_NO_DESCRIPTION

    embed: dict = {
        "title": title,
        "url": site_url,
        "description": description,
        "color": ANILIST_COLOR,
        "footer": {"text": S.FOOTER_CHARACTER},
    }
    image = (staff.get("image") or {}).get("large")
    if image:
        embed["thumbnail"] = {"url": image}

    # Primary occupations — AniList stores a list (e.g. ["Director", "Writer"]).
    occupations = staff.get("primaryOccupations") or []
    fields: list[dict] = []
    if occupations:
        fields.append({
            "name": "Role",
            "value": ", ".join(occupations[:5]),
            "inline": True,
        })

    # Production credits — five most-popular media they worked on, each
    # prefixed by the staffRole at that production.
    edges = ((staff.get("staffMedia") or {}).get("edges")) or []
    if edges:
        lines = []
        for e in edges[:5]:
            role = (e.get("staffRole") or "").strip() or "Worked on"
            node = e.get("node") or {}
            mtitle = _format_title(node)
            url = node.get("siteUrl") or ""
            anchor = f"[{mtitle}]({url})" if url else mtitle
            lines.append(f"• *{role}* — {anchor}")
        fields.append({"name": "Production credits", "value": "\n".join(lines), "inline": False})
    else:
        fields.append({"name": "Production credits", "value": S.STAFF_NO_CREDITS, "inline": False})

    embed["fields"] = fields
    return embed


# v8.2.0 — recency cutoff for the studio embed's "Recent works" split.
# Anything with seasonYear >= (current_year - STUDIO_RECENT_WITHIN_YEARS)
# qualifies; older popular works go in the catalog section.
STUDIO_RECENT_WITHIN_YEARS = 2


def _make_studio_embed(studio: dict) -> dict:
    """Studio card — name + popular works, with a "recent" split (last 2 years)
    when the studio has fresh releases. `isAnimationStudio` switches the
    title prefix between 🎬 (animation house) and 🏢 (other production org)."""
    name = (studio.get("name") or "").strip() or "Unknown studio"
    is_anim = bool(studio.get("isAnimationStudio"))
    header = (
        S.STUDIO_HEADER_ANIM.format(name=name)
        if is_anim
        else S.STUDIO_HEADER_OTHER.format(name=name)
    )
    site_url = studio.get("siteUrl") or None

    media_nodes = ((studio.get("media") or {}).get("nodes")) or []
    current_year = datetime.now(timezone.utc).year
    recent_cutoff = current_year - STUDIO_RECENT_WITHIN_YEARS

    recent: list[dict] = []
    catalog: list[dict] = []
    for m in media_nodes:
        year = m.get("seasonYear")
        if isinstance(year, int) and year >= recent_cutoff:
            recent.append(m)
        else:
            catalog.append(m)

    def _line(m: dict) -> str:
        title = _format_title(m)
        url = m.get("siteUrl") or ""
        year = m.get("seasonYear")
        suffix = f" ({year})" if isinstance(year, int) else ""
        return f"• [{title}]({url}){suffix}" if url else f"• {title}{suffix}"

    fields: list[dict] = []
    if recent:
        fields.append({
            "name": f"Recent (≤ {STUDIO_RECENT_WITHIN_YEARS}y)",
            "value": "\n".join(_line(m) for m in recent[:5]),
            "inline": False,
        })
    if catalog:
        fields.append({
            "name": "Popular works",
            "value": "\n".join(_line(m) for m in catalog[:5]),
            "inline": False,
        })
    if not fields:
        fields.append({
            "name": "Works",
            "value": S.STUDIO_NO_WORKS,
            "inline": False,
        })

    # v10.0.0 accessibility: every embed should carry a meaningful
    # `description` field (the only field Discord exposes to screen readers
    # consistently). For the studio card the description is a one-line
    # summary of what's in the embed below.
    summary = (
        f"Animation studio profile · {len(recent)} recent + {len(catalog)} catalog works"
        if is_anim
        else f"Production studio profile · {len(recent) + len(catalog)} credits listed"
    )
    return {
        "title": header,
        "url": site_url,
        "description": summary,
        "color": ANILIST_COLOR,
        "fields": fields,
        "footer": {"text": S.STUDIO_FOOTER},
    }


def _make_list_embed(media_list: list[dict], header: str, page: int = 1, has_next: bool = False) -> dict:
    """List view — used by /discover, /trending, and /similar."""
    if not media_list:
        return {
            "title": header,
            "description": S.LIST_NO_RESULTS,
            "color": ANILIST_COLOR,
            "footer": {"text": S.FOOTER_ANILIST},
        }
    lines = []
    for i, m in enumerate(media_list, start=1):
        title = _format_title(m)
        score = _score(m)
        genres = ", ".join((m.get("genres") or [])[:3])
        url = m.get("siteUrl") or ""
        tag_line = f" — _{genres}_" if genres else ""
        lines.append(f"**{i}. [{title}]({url})** · ⭐ {score}{tag_line}")
    footer_bits = [f"Page {page}", S.FOOTER_ANILIST]
    if not has_next and page > 1:
        footer_bits.insert(1, S.LIST_LAST_PAGE)
    return {
        "title": header,
        "description": "\n\n".join(lines),
        "color": ANILIST_COLOR,
        "footer": {"text": " · ".join(footer_bits)},
    }


def _make_select_row(media_list: list[dict], select_id: str = "otaku:expand") -> ActionRow | None:
    """Build a select menu that lets the user pick one result to expand."""
    if not media_list:
        return None
    options = []
    for i, m in enumerate(media_list, start=1):
        mid = m.get("id")
        if mid is None:
            continue
        title = _truncate(_format_title(m), 100)
        options.append(SelectOption(label=f"{i}. {title}", value=str(mid)))
    if not options:
        return None
    return ActionRow(SelectMenu(
        custom_id=select_id,
        options=options,
        placeholder="Expand one of these…",
        min_values=1,
        max_values=1,
    ))


def _page_buttons(prev_id: str | None, next_id: str | None) -> ActionRow:
    return ActionRow(
        Button("Prev", custom_id=prev_id or "otaku:noop:prev", style="secondary", emoji="⬅️", disabled=prev_id is None),
        Button("Next", custom_id=next_id or "otaku:noop:next", style="secondary", emoji="➡️", disabled=next_id is None),
    )


def _on_cooldown(ctx: Context, user_id: str) -> bool:
    """Returns True (and replies ephemerally) if the user is rate-limited."""
    key = f"otaku:user:{user_id}"
    state = ctx.ephemeral.cooldown_check(key)
    if state.get("active"):
        remaining = int(state.get("remaining_seconds") or 1) or 1
        ctx.interaction.respond(
            content=S.COOLDOWN_WAIT.format(remaining=remaining),
            ephemeral=True,
        )
        return True
    ctx.ephemeral.cooldown_set(key, ttl_seconds=COOLDOWN_SECONDS)
    return False


# ── In-process AniList response cache ────────────────────────────────────────
# Tiny TTL'd dict so a popular query (e.g. /trending in a single server) doesn't
# hammer AniList. The cache is per-worker-process; in pool mode that means it's
# shared across servers, which is fine since AniList responses are global.
#
# `_anilist_query` keys on a stable hash of (query, sorted-vars). Callers opt in
# with `cache=True`. Per the roadmap, /similar stays uncached.
_CACHE: dict[str, tuple[float, dict]] = {}


def _cache_key(*parts) -> str:
    """Build a stable cache key from arbitrary parts.

    v9.1 changed the signature from `(query, variables)` to `*parts` so the
    same in-process cache can serve AniList GraphQL, Jikan REST, and Kitsu
    JSON:API responses without key collision. AniList callers pass
    ``_cache_key(query, variables)``; Jikan/Kitsu callers pass
    ``_cache_key("jikan", path, params)`` or ``_cache_key("kitsu", path, params)``.

    Dicts inside `parts` are normalised to sorted (key, value) tuples so
    semantically-identical variables hash to the same key.
    """
    normalised = []
    for p in parts:
        if isinstance(p, dict):
            normalised.append(tuple(sorted(p.items())))
        else:
            normalised.append(p)
    return repr(tuple(normalised))


def _cache_get(key: str) -> dict | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    expires_at, data = entry
    if expires_at < time.monotonic():
        _CACHE.pop(key, None)
        return None
    return data


def _cache_clear() -> None:
    """Drop every cached AniList response. Exposed for tests."""
    _CACHE.clear()


def _cache_put(key: str, data: dict) -> None:
    if len(_CACHE) >= ANILIST_CACHE_MAX_ENTRIES:
        # Drop the oldest entry — dict preserves insertion order in 3.7+.
        try:
            oldest = next(iter(_CACHE))
            _CACHE.pop(oldest, None)
        except StopIteration:
            pass
    _CACHE[key] = (time.monotonic() + ANILIST_CACHE_TTL, data)


# Module-level slot for the most recent user-fixable error from AniList.
# Callers consume it via _consume_last_user_error() and surface it ephemerally.
_LAST_USER_ERROR: str | None = None


def _consume_last_user_error() -> str | None:
    """Return and clear the most recent user-fixable AniList error, if any."""
    global _LAST_USER_ERROR
    msg = _LAST_USER_ERROR
    _LAST_USER_ERROR = None
    return msg


def _classify_anilist_errors(errors: list) -> str | None:
    """Return a user-fixable message if any error in the list looks user-facing."""
    for err in errors:
        msg = (err or {}).get("message") if isinstance(err, dict) else None
        if not msg:
            continue
        lower = msg.lower()
        if any(frag in lower for frag in _USER_FIXABLE_ANILIST_FRAGMENTS):
            return msg
    return None


def _sleep_for_retry(seconds: float) -> None:
    """Indirected so tests can patch out the real sleep."""
    time.sleep(seconds)


def _http_json(resp: dict | None):
    """Parse the JSON body out of a ctx.http response dict, tolerating live-host
    field-name/encoding drift (v10.0.11). Returns the parsed object, or None when
    no usable body is present. Never raises.

    The SDK contract is {status, headers, body_bytes (str), truncated}, but this
    host has drifted from its own SDK before ($N→%s placeholders, options→
    command_options, the params= kwarg the proxy rejected). Accepted shapes:
      - str/bytes JSON under body_bytes / body / text (bytes UTF-8-decoded)
      - a pre-parsed dict/list under those keys (host-side JSON passthrough)
      - base64 text under body_b64 / body_base64, or base64 accidentally sent
        under a documented key (decisive: a real JSON body never b64-decodes
        cleanly with validate=True AND re-parses as JSON)
    """
    if not isinstance(resp, dict):
        return None
    # A truncated body is unusable even when the fragment happens to parse —
    # data loss of unknown extent. v10.0.12: explicit-FALSY allowlist. Plain
    # truthiness made a host drifting truncated to the STRING "false" reject
    # every body; but enumerating truthy shapes instead would fail OPEN on the
    # next unknown drift ("on", 2, b"true"). So: known-falsy shapes pass,
    # anything else counts as truncated (fail closed); the anomaly log at the
    # call site surfaces the flag either way.
    trunc = resp.get("truncated")
    if isinstance(trunc, (bytes, bytearray)):
        trunc = bytes(trunc).decode("utf-8", errors="replace")
    if not (
        trunc is None
        or trunc is False
        or (isinstance(trunc, (int, float)) and trunc == 0)
        or (isinstance(trunc, str) and trunc.strip().lower() in ("", "false", "0", "no", "none"))
    ):
        return None
    raw = None
    for key in ("body_bytes", "body", "text"):
        val = resp.get(key)
        if val not in (None, "", b""):
            raw = val
            break
    if raw is None:
        for key in ("body_b64", "body_base64"):
            val = resp.get(key)
            if isinstance(val, str) and val:
                try:
                    raw = base64.b64decode(val, validate=True)
                except (ValueError, TypeError):
                    return None
                break
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        try:
            return json.loads(base64.b64decode(raw, validate=True))
        except (ValueError, TypeError):
            return None


def _http_status(resp: dict | None) -> int:
    """Coerce the HTTP status out of a ctx.http response dict (v10.0.11).

    Tolerates the documented int under "status", the legacy "status_code"
    shape, and numeric strings. Returns 0 when absent or garbled so callers'
    range checks fail closed instead of raising (the pre-v10.0.11 AniList
    retry loop crashed on a non-numeric status via bare int()).
    """
    if not isinstance(resp, dict):
        return 0
    for key in ("status", "status_code"):
        val = resp.get(key)
        if val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return 0


def _log_http_body_anomaly(ctx: Context, source: str, resp: dict | None) -> None:
    """One bounded log line describing an unusable HTTP body (v10.0.11 triage).

    Mirrors the v10.0.8 diagnostic playbook: when the live host's proxy response
    can't be parsed, this single line in the dev-portal log tells us exactly what
    shape arrived (keys, body type/length, truncated flag, first bytes) so the
    next patch can be targeted instead of guessed. Must never raise.
    """
    try:
        r = resp if isinstance(resp, dict) else {}
        # v10.0.12 — pick the raw body with the SAME key-priority scan as
        # _http_json, so the diagnostic describes the value the parser
        # actually saw. The old two-key .get() chain reported a present-but-
        # None body_bytes while hiding a populated `body`, muddying exactly
        # the live triage this line exists for.
        raw = None
        for key in ("body_bytes", "body", "text", "body_b64", "body_base64"):
            val = r.get(key)
            if val not in (None, "", b""):
                raw = val
                break
        if raw is None:
            # All candidate values empty: report the first PRESENT key's
            # value so body_type reflects what actually arrived.
            raw = next(
                (r[k] for k in ("body_bytes", "body", "text", "body_b64", "body_base64") if k in r),
                None,
            )
        ctx.log(
            f"{source} http body anomaly",
            level="error",
            tags=[source, "http", "body-anomaly"],
            keys=",".join(sorted(str(k) for k in r.keys()))[:200],
            body_type=type(raw).__name__,
            body_len=str(len(raw)) if hasattr(raw, "__len__") else "n/a",
            truncated=str(r.get("truncated")),
            head=repr(bytes(raw[:160]) if isinstance(raw, bytearray) else raw[:160])
            if isinstance(raw, (str, bytes, bytearray)) else "",
        )
    except Exception:  # noqa: BLE001 — diagnostics must never raise into the caller
        pass


def _anilist_post_once(ctx: Context, body: str) -> tuple[dict | None, str]:
    """One POST to AniList.

    Returns (response, classification) where classification is one of:
      - "ok"       : got a response dict (which may still be 4xx/5xx)
      - "timeout"  : RpcTimeoutError raised — retryable
      - "rate"     : RateLimitError raised — NOT retryable
      - "validation": ValidationError raised — NOT retryable
      - "error"    : other exception — NOT retryable
    """
    try:
        resp = ctx.http.post(
            ANILIST_URL,
            body=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return resp, "ok"
    except RpcTimeoutError as exc:
        ctx.log(f"anilist timed out: {exc}", level="warning", tags=["anilist", "http"])
        return None, "timeout"
    except RateLimitError as exc:
        ctx.log(
            "anilist rate limited",
            level="warning",
            tags=["anilist", "http"],
            retry_after=getattr(exc, "retry_after", None),
        )
        return None, "rate"
    except ValidationError as exc:
        ctx.log(f"anilist validation error: {exc}", level="error", tags=["anilist", "http"])
        return None, "validation"
    except Exception as exc:  # noqa: BLE001 — last-resort guard around the proxy call
        ctx.log(f"anilist call failed: {exc}", level="error", tags=["anilist", "http"])
        return None, "error"


def _anilist_query(
    ctx: Context,
    query: str,
    variables: dict,
    *,
    cache: bool = False,
) -> dict | None:
    """POST to AniList; return parsed JSON `data` on success, or None on any error.

    On error this function does NOT reply — callers reply (so they can decide
    between respond/followup depending on whether they've deferred). It does
    log the failure with ctx.log. When `cache=True`, responses are memoized for
    ANILIST_CACHE_TTL seconds in-process.

    Transient failures (RpcTimeoutError, 5xx responses) are retried with the
    backoffs in ANILIST_RETRY_BACKOFFS_S. RateLimitError is NEVER retried
    automatically — the caller backs off via the next user request.
    """
    global _LAST_USER_ERROR

    cache_key = _cache_key(query, variables) if cache else None
    if cache_key is not None:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    body = json.dumps({"query": query, "variables": variables})

    resp: dict | None = None
    for attempt, backoff in enumerate((0.0, *ANILIST_RETRY_BACKOFFS_S)):
        if attempt > 0:
            _sleep_for_retry(backoff)
        resp, classification = _anilist_post_once(ctx, body)
        if classification == "timeout":
            continue  # retry
        if classification in ("rate", "validation", "error"):
            return None  # never retry these
        # classification == "ok"
        status = _http_status(resp)
        if 500 <= status < 600:
            ctx.log(
                "anilist 5xx",
                level="warning",
                tags=["anilist", "http"],
                status=str(status),
            )
            continue  # retry
        break  # got a non-5xx response — stop retrying
    else:
        # Exhausted retries on transient failures.
        return None

    if resp is None or _http_status(resp) != 200:
        ctx.log(
            "anilist non-200",
            level="warning",
            tags=["anilist", "http"],
            status=str(resp.get("status") if resp else None),
            # v10.0.12 — also log what the gate actually compared, so the
            # triage line can't diverge from the gate on a drifted shape.
            status_coerced=str(_http_status(resp)),
        )
        return None

    payload = _http_json(resp)
    if not isinstance(payload, dict):
        ctx.log("anilist returned unparseable JSON", level="error", tags=["anilist"])
        _log_http_body_anomaly(ctx, "anilist", resp)
        return None

    if payload.get("errors"):
        errors = payload["errors"]
        ctx.log(
            "anilist returned errors",
            level="warning",
            tags=["anilist"],
            errors=str(errors)[:400],
        )
        user_msg = _classify_anilist_errors(errors)
        if user_msg:
            _LAST_USER_ERROR = user_msg
        return None

    data = payload.get("data")
    if cache_key is not None and data is not None:
        try:
            _cache_put(cache_key, data)
        except Exception:  # noqa: BLE001 — cache must never raise into the caller
            pass
    return data


# ── v9.1.0 — multi-source transport: rate buckets, Jikan, Kitsu ─────────────

# In-process token buckets per source. Keys are source names; values are
# deques of float monotonic-clock timestamps for requests we've made in the
# current window. _rate_acquire trims expired entries, then either:
#   - admits the call immediately (returns 0.0)
#   - sleeps until the oldest in-window timestamp would expire (returns
#     the slept time, > 0)
# The buckets are module-global so they're shared across the (server,
# plugin) tuple — which is what we want, since rate limits are per
# source IP, not per server.
_RATE_BUCKETS: dict[str, list[float]] = {s: [] for s in SOURCE_RATE_LIMITS}


def _rate_acquire(source: str) -> float:
    """Block the caller (via time.sleep) until the source's per-window
    budget admits a new request. Returns the seconds slept (0.0 if no
    wait needed). Sources without a configured limit are admitted
    unconditionally — useful for tests and for keep-AniList-only paths.
    """
    if source not in SOURCE_RATE_LIMITS:
        return 0.0
    max_n, window_s = SOURCE_RATE_LIMITS[source]
    now = time.monotonic()
    bucket = _RATE_BUCKETS.setdefault(source, [])
    # Drop expired timestamps.
    cutoff = now - window_s
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    slept = 0.0
    if len(bucket) >= max_n:
        # Sleep until the oldest in-window entry would expire.
        wait_until = bucket[0] + window_s
        slept = max(0.0, wait_until - now)
        if slept > 0:
            _sleep_for_retry(slept)
        # Re-trim after sleeping.
        now = time.monotonic()
        cutoff = now - window_s
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
    bucket.append(now)
    return slept


def _jikan_query(
    ctx: Context, path: str, params: dict | None = None, *, cache: bool = False
) -> dict | None:
    """REST GET against Jikan v4 (MAL data). Returns the parsed JSON body's
    `data` field on success, or None on any error. Honors the jikan rate
    bucket. cache=True memoizes for ANILIST_CACHE_TTL (the cache key
    namespaces on source so AniList ↔ Jikan responses don't collide).

    Path is relative to JIKAN_BASE_URL (e.g. "/anime?q=Frieren"). The
    caller composes the path; this helper only handles transport, rate
    limiting, and JSON parsing.
    """
    cache_key = _cache_key("jikan", path, params or {}) if cache else None
    if cache_key is not None:
        cached = _cache_get(cache_key)
        if isinstance(cached, dict):
            # v10.0.12 — unwrap the {"data": ...} cache envelope, mirroring
            # _kitsu_query. Returning the envelope raw made every cache HIT
            # return a different shape than a miss, so repeat MAL-fallback
            # searches within the TTL silently degraded to Kitsu/'not found'.
            # v10.0.13 — a non-dict cache entry (future put-site drift,
            # hot-reload remnant) now falls through to a real re-fetch — a
            # TRUE miss — instead of reporting source failure for the TTL.
            return cached.get("data")

    _rate_acquire("jikan")
    # v10.0.7 — SDK's ctx.http.get(url, *, headers=None) does NOT accept a
    # `params` kwarg. v9.1.0 shipped with the wrong signature and the call
    # was silently failing as "TypeError: unexpected keyword argument 'params'"
    # → caught by the bare-Exception clause and surfaced as "jikan call
    # failed" in the logs. v10.0.7 URL-encodes params into the query string
    # directly. Same goes for the response shape: SDK returns `status` /
    # `body_bytes`, not `status_code` / `body`.
    url = f"{JIKAN_BASE_URL}{path}"
    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params, doseq=True)}"
    try:
        resp = ctx.http.get(
            url,
            headers={"Accept": "application/json"},
        )
    except RpcTimeoutError as exc:
        ctx.log(f"jikan timed out: {exc}", level="warning", tags=["jikan", "http"])
        return None
    except RateLimitError as exc:
        ctx.log(
            "jikan rate limited", level="warning",
            tags=["jikan", "http"],
            retry_after=getattr(exc, "retry_after", None),
        )
        return None
    except ValidationError as exc:
        ctx.log(f"jikan validation error: {exc}", level="error", tags=["jikan", "http"])
        return None
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"jikan call failed: {exc}", level="error", tags=["jikan", "http"])
        return None

    # v10.0.11 — status coerced via _http_status (handles status/status_code
    # and numeric strings); non-2xx now LOGS instead of silently dropping, so
    # a host status-shape drift or a real upstream error is visible in the
    # dev-portal log instead of masquerading as "no results".
    status = _http_status(resp)
    if status < 200 or status >= 300:
        ctx.log(
            "jikan non-2xx", level="warning", tags=["jikan", "http"],
            status=str((resp or {}).get("status", (resp or {}).get("status_code"))),
        )
        return None
    payload = _http_json(resp)
    if not isinstance(payload, dict):
        # v10.0.12 — None (no usable body) or parsed-but-non-dict (e.g. a
        # bare JSON list): neither is the documented Jikan envelope. The
        # non-dict case used to fall through silently with no diagnostic.
        _log_http_body_anomaly(ctx, "jikan", resp)
        return None
    # Jikan wraps results in a `data` field; pagination metadata in `pagination`.
    data = payload.get("data")
    if data is None:
        return None
    if cache_key is not None:
        try:
            _cache_put(cache_key, {"data": data})
        except Exception:  # noqa: BLE001
            pass
    return data


def _kitsu_query(
    ctx: Context, path: str, params: dict | None = None, *, cache: bool = False
) -> list[dict] | None:
    """JSON:API GET against Kitsu. Returns the parsed `data` array on
    success (or single resource if the endpoint returns one), or None on
    error. Honors the kitsu rate bucket. cache=True memoizes for
    ANILIST_CACHE_TTL.

    Path is relative to KITSU_BASE_URL (e.g. "/anime?filter[text]=Frieren").
    """
    cache_key = _cache_key("kitsu", path, params or {}) if cache else None
    if cache_key is not None:
        cached = _cache_get(cache_key)
        if isinstance(cached, dict):
            # unwrap the cache envelope; non-dict entries fall through to a
            # true re-fetch (v10.0.13 — was: reported as source failure)
            return cached.get("data")

    _rate_acquire("kitsu")
    # v10.0.7 — see _jikan_query for the same fix rationale: SDK
    # `ctx.http.get(url, *, headers=None)` doesn't take `params`; encode
    # into the URL. Response fields are `status` / `body_bytes`.
    url = f"{KITSU_BASE_URL}{path}"
    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params, doseq=True)}"
    try:
        resp = ctx.http.get(
            url,
            headers={"Accept": "application/vnd.api+json"},
        )
    except RpcTimeoutError as exc:
        ctx.log(f"kitsu timed out: {exc}", level="warning", tags=["kitsu", "http"])
        return None
    except RateLimitError as exc:
        ctx.log(
            "kitsu rate limited", level="warning",
            tags=["kitsu", "http"],
            retry_after=getattr(exc, "retry_after", None),
        )
        return None
    except ValidationError as exc:
        ctx.log(f"kitsu validation error: {exc}", level="error", tags=["kitsu", "http"])
        return None
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"kitsu call failed: {exc}", level="error", tags=["kitsu", "http"])
        return None

    status = _http_status(resp)
    if status < 200 or status >= 300:
        ctx.log(
            "kitsu non-2xx", level="warning", tags=["kitsu", "http"],
            status=str((resp or {}).get("status", (resp or {}).get("status_code"))),
        )
        return None
    payload = _http_json(resp)
    if not isinstance(payload, dict):
        # v10.0.12 — same as _jikan_query: non-dict bodies now get the
        # anomaly diagnostic instead of a silent drop.
        _log_http_body_anomaly(ctx, "kitsu", resp)
        return None
    data = payload.get("data")
    if data is None:
        return None
    # Normalize single-resource responses to a list of one for caller simplicity.
    if isinstance(data, dict):
        data = [data]
    if cache_key is not None:
        try:
            _cache_put(cache_key, {"data": data})
        except Exception:  # noqa: BLE001
            pass
    return data


def _search_media(
    ctx: Context, query: str, *, media_type: str = "anime"
) -> dict | None:
    """Multi-source media search with AniList → Jikan → Kitsu fallback.

    Returns the canonical media dict from whichever source served the result,
    or None if all three sources failed. Used by /anime and /manga (v9.1).
    Other commands (/discover, /similar, /random, /mood, /find, /genres,
    /character, /notify*) stay AniList-only per the v8.0.1 audit cheat sheet
    — those features depend on AniList-specific schemas (recommendation
    graph, tag taxonomy, GenreCollection, airing schedules).

    `media_type` is "anime" or "manga". Returns canonical dicts shaped the
    same as v8.0's `_make_anime_embed` / `_make_manga_embed` expect, with
    `source` / `source_id` annotations added.
    """
    query_lower = query.lower().strip()

    # 1) AniList — primary. Reuse the existing query constants based on type.
    anilist_query = QUERY_SEARCH_ONE if media_type == "anime" else QUERY_MANGA_SEARCH_ONE
    data = _anilist_query(ctx, anilist_query, {"q": query_lower}, cache=True)
    if data is not None:
        media = data.get("Media")
        if media:
            return _canonicalize_anilist_media(media)

    # 2) Jikan (MyAnimeList) — first fallback. Jikan exposes /anime and /manga
    # as separate REST endpoints.
    jikan_path = f"/{media_type}"
    jikan_data = _jikan_query(
        ctx, jikan_path, params={"q": query_lower, "limit": 1}, cache=True,
    )
    if jikan_data is not None:
        results = jikan_data if isinstance(jikan_data, list) else [jikan_data]
        if results:
            canonical = _canonicalize_jikan_media(results[0])
            if canonical is not None:
                return canonical

    # 3) Kitsu — second fallback. JSON:API filter syntax.
    kitsu_data = _kitsu_query(
        ctx,
        f"/{media_type}",
        params={"filter[text]": query_lower, "page[limit]": 1},
        cache=True,
    )
    if kitsu_data:
        canonical = _canonicalize_kitsu_media(kitsu_data[0])
        if canonical is not None:
            return canonical

    return None


def _reply_error(ctx: Context, message: str, *, deferred: bool) -> None:
    """Send an ephemeral error — use the right channel depending on whether we deferred."""
    if deferred:
        ctx.interaction.followup(content=message, ephemeral=True)
    else:
        ctx.interaction.respond(content=message, ephemeral=True)


def _reply_anilist_failure(ctx: Context, *, deferred: bool, fallback: str | None = None) -> None:
    """Reply to the user after `_anilist_query` returned None.

    If the failure was a user-fixable GraphQL error (e.g. "must contain at least
    3 characters"), surface that message verbatim. Otherwise use `fallback`, or
    a default action-suggesting line.
    """
    user_msg = _consume_last_user_error()
    if user_msg:
        _reply_error(ctx, S.ANILIST_PREFIX.format(msg=user_msg), deferred=deferred)
        return
    default = fallback or S.ANILIST_FAILURE_DEFAULT
    _reply_error(ctx, default, deferred=deferred)


# ── Shared rendering for list-style commands ─────────────────────────────────

def _render_discover(
    ctx: Context,
    genre: str,
    sort_key: str,
    page: int,
    *,
    deferred: bool,
    ephemeral_reply: bool = False,
) -> None:
    sort_const = SORT_MAP.get(sort_key, "POPULARITY_DESC")
    # v10.0.1: cache every page (previously page 1 only). AniList answers
    # the same (genre, sort, page) tuple identically for the
    # ANILIST_CACHE_TTL window, so re-fetching when a user clicks next/prev
    # was pure waste.
    data = _anilist_query(
        ctx,
        QUERY_DISCOVER,
        {"genre": genre, "sort": [sort_const], "page": page, "perPage": PER_PAGE},
        cache=True,
    )
    if data is None:
        _reply_anilist_failure(ctx, deferred=deferred)
        return
    page_obj = (data.get("Page") or {})
    media_list = page_obj.get("media") or []
    has_next = bool((page_obj.get("pageInfo") or {}).get("hasNextPage"))

    header = f"📚 {genre.title()} — {sort_key.title()}"
    embed = _make_list_embed(media_list, header, page=page, has_next=has_next)

    if not media_list:
        _reply_error(ctx, S.DISCOVER_NO_RESULTS.format(genre=genre), deferred=deferred)
        return

    prev_id = f"otaku:page:{genre}:{sort_key}:{page - 1}" if page > 1 else None
    next_id = f"otaku:page:{genre}:{sort_key}:{page + 1}" if has_next else None
    components = [_page_buttons(prev_id, next_id)]
    select_row = _make_select_row(media_list)
    if select_row is not None:
        components.append(select_row)

    if deferred:
        ctx.interaction.followup(embeds=[embed], components=components, ephemeral=ephemeral_reply)
    else:
        ctx.interaction.respond(embeds=[embed], components=components, ephemeral=ephemeral_reply)


def _render_trending(ctx: Context, page: int, *, deferred: bool, ephemeral_reply: bool = False) -> None:
    season, year = _current_season()
    # v10.0.1: cache every page (see _render_discover comment).
    data = _anilist_query(
        ctx,
        QUERY_SEASON,
        {"season": season, "year": year, "sort": ["TRENDING_DESC"],
         "page": page, "perPage": PER_PAGE},
        cache=True,
    )
    if data is None:
        _reply_anilist_failure(ctx, deferred=deferred)
        return
    page_obj = (data.get("Page") or {})
    media_list = page_obj.get("media") or []
    has_next = bool((page_obj.get("pageInfo") or {}).get("hasNextPage"))

    header = f"🔥 Trending — {season.title()} {year}"
    embed = _make_list_embed(media_list, header, page=page, has_next=has_next)
    if not media_list:
        _reply_error(ctx, S.TRENDING_NO_RESULTS, deferred=deferred)
        return

    prev_id = f"otaku:trend:{page - 1}" if page > 1 else None
    next_id = f"otaku:trend:{page + 1}" if has_next else None
    components = [_page_buttons(prev_id, next_id)]
    select_row = _make_select_row(media_list)
    if select_row is not None:
        components.append(select_row)

    if deferred:
        ctx.interaction.followup(embeds=[embed], components=components, ephemeral=ephemeral_reply)
    else:
        ctx.interaction.respond(embeds=[embed], components=components, ephemeral=ephemeral_reply)


def _render_similar(ctx: Context, media: dict, *, deferred: bool, ephemeral_reply: bool = False) -> None:
    nodes = ((media.get("recommendations") or {}).get("nodes")) or []
    recs = [n.get("mediaRecommendation") for n in nodes if n and n.get("mediaRecommendation")]
    recs = recs[:5]
    parent_title = _format_title(media)
    header = f"🔁 Similar to {parent_title}"

    if not recs:
        _reply_error(ctx, S.SIMILAR_NO_RECS.format(title=parent_title), deferred=deferred)
        return

    embed = _make_list_embed(recs, header, page=1, has_next=False)
    components = []
    select_row = _make_select_row(recs)
    if select_row is not None:
        components.append(select_row)

    if deferred:
        ctx.interaction.followup(embeds=[embed], components=components or None, ephemeral=ephemeral_reply)
    else:
        ctx.interaction.respond(embeds=[embed], components=components or None, ephemeral=ephemeral_reply)


# ── Schema bootstrap + lifecycle ─────────────────────────────────────────────


_SCHEMA_V8_MIGRATED_KV = "otaku:schema_v8_migrated"
# v10.0.12 — set once the wide PK has been verified (or repaired) on a tenant
# whose bootstrap already completed; gates the one-time post-completion probe.
_SCHEMA_PK_VERIFIED_KV = "otaku:schema_pk_verified"

# Probe COALESCEs to a sentinel so the repair decision never depends on whether
# the host serializes NULL columns into result rows (the response-shape drift
# class this plugin keeps meeting). ::TEXT keeps the value a plain string.
# Probes the TABLE alongside the PK index: a missing table means bootstrap
# simply hasn't created it yet (fresh install / cursor before statement #0) —
# healthy-pending, NOT poisoned. Repairing in that window ALTERed a
# nonexistent relation and crashed bootstrap before CREATE TABLE could run.
_PK_PROBE_SQL = (
    "SELECT COALESCE(to_regclass('otaku_user_media')::TEXT, 'missing') AS tbl, "
    "COALESCE(to_regclass('otaku_user_media_pkey')::TEXT, 'missing') AS pk"
)


def _ensure_wide_pk(ctx: Context) -> bool:
    """Verify otaku_user_media's wide primary key exists; finish the aborted
    v7→v8 dance if it doesn't (v10.0.12).

    Why: under v10.0.8–v10.0.10 a v7 upgrade could rate-limit at the final
    ADD CONSTRAINT, and the next boot's "already v8" branch set the migrated
    marker with the table PK-less — permanently, because every later boot
    short-circuited on the marker. v10.0.11 added a probe inside branch (b),
    but the marker fast-path returned before it, so already-poisoned tenants
    were never healed. This helper is instead called from _bootstrap_schema
    AFTER _migrate_v7_to_v8 returns (covering every marker-skip path), and
    once post-completion via the _SCHEMA_PK_VERIFIED_KV gate.

    Returns True when the PK state was POSITIVELY verified (present, repaired,
    or table-not-yet-created so the inline-PK CREATE TABLE handles it), and
    False when the probe was INDETERMINATE — empty rowset or a row without
    the expected tbl/pk keys (host response-shape drift, or stubbed SQL in
    tests). v10.0.13: indeterminate fails CLOSED — no repair DDL is fired
    (we can't act on what we can't see) and callers must NOT set the
    permanent pk_verified marker, so the probe retries next boot. The
    fail-open draft let drifted rows lock pk_verified in over a genuinely
    missing PK — the exact marker-set-while-broken class this exists to kill.

    Plain SELECT probes are not DDL-rate-limited. A mid-repair RateLimitError
    propagates to the caller's handler and the next boot retries.
    """
    rows = ctx.sql.query(_PK_PROBE_SQL) or []
    row = rows[0] if rows else {}
    tbl, pk = row.get("tbl"), row.get("pk")
    if tbl is None or pk is None:
        # Indeterminate probe: log one bounded line and let the caller skip
        # the marker. One SELECT per boot until the shape is sane.
        try:
            ctx.log(
                "wide-PK probe indeterminate; will re-probe next boot",
                level="warning",
                tags=["bootstrap", "pk-heal"],
                row_keys=",".join(sorted(str(k) for k in row.keys()))[:120],
                rowcount=str(len(rows)),
            )
        except Exception:  # noqa: BLE001
            pass
        return False
    if tbl == "missing":
        # The table doesn't exist yet — bootstrap's first DDL creates it with
        # the wide PK inline. Nothing to repair (and nothing repairable).
        return True
    if pk != "missing":
        return True
    try:
        ctx.log(
            "wide-PK missing on otaku_user_media; running repair DDL",
            level="warning",
            tags=["bootstrap", "pk-heal"],
        )
    except Exception:  # noqa: BLE001 — diagnostics must never block the repair
        pass
    # Finish steps 2–6 of the branch (c) dance (all idempotent except the
    # final ADD CONSTRAINT, which the probe just proved absent). The stale v7
    # index is dropped too: an abort between RENAME and DROP INDEX leaves a
    # duplicate index that CREATE INDEX IF NOT EXISTS can never clean up.
    # (No advisory lock here: each host sql.execute is its own transaction,
    # so a pg_advisory_xact_lock would release immediately — the re-probe
    # below is the real cross-worker race guard.)
    ctx.sql.execute(
        "DROP INDEX IF EXISTS otaku_user_anime_user_status_added_idx"
    )
    ctx.sql.execute(
        "ALTER TABLE otaku_user_media "
        "ADD COLUMN IF NOT EXISTS media_type TEXT NOT NULL DEFAULT 'anime'"
    )
    ctx.sql.execute(
        "ALTER TABLE otaku_user_media "
        "DROP CONSTRAINT IF EXISTS otaku_user_anime_pkey"
    )
    try:
        ctx.sql.execute(
            "ALTER TABLE otaku_user_media "
            "ADD CONSTRAINT otaku_user_media_pkey "
            "PRIMARY KEY (user_id, media_id, media_type)"
        )
    except RateLimitError:
        raise
    except Exception:  # noqa: BLE001
        # Either a cross-worker race (another worker added the PK between our
        # probe and this statement) or a genuine failure. Re-probe to decide:
        # swallow the race, surface the failure. v10.0.13 — an indeterminate
        # re-probe certifies nothing: return False so no marker is set.
        rows = ctx.sql.query(_PK_PROBE_SQL) or []
        pk_recheck = (rows[0] if rows else {}).get("pk")
        if pk_recheck == "missing":
            raise
        if pk_recheck is None:
            return False
    return True


def _migrate_v7_to_v8(ctx: Context) -> None:
    """Rename otaku_user_anime → otaku_user_media and add the media_type column.

    Idempotent at the **step level**: every individual DDL can run repeatedly
    without raising, and a partial failure mid-sequence self-heals on the
    next call. Pool-mode safe via pg_advisory_xact_lock so concurrent workers
    serialize on the migration without serializing every bootstrap pass.

    v8.0.0 shipped this helper without the step-level idempotency or the
    advisory lock; the v8.0.1 audit found two production-safety bugs:
      1. Partial failure (e.g. lock timeout during PK widening) left the
         table half-migrated AND the next call's table-name probe found
         `otaku_user_anime` already renamed, so the early-return triggered
         and the half-state never healed.
      2. Two pool-mode workers could both pass the probe under snapshot
         isolation; the second worker's RENAME then hit "relation does
         not exist" because worker A's RENAME had already committed.
    The v8.0.1 rewrite re-probed for each landmark via information_schema
    so every step was independently safe.

    v10.0.6 replaces those `information_schema` probes — the host now
    blocks the literal pattern `information_schema`, so the v8.0.1 version
    crashed at boot with `Blocked SQL pattern`. The new probes use
    `to_regclass(...)` for table existence (a function call, not a system-
    schema reference) and a KV marker (`otaku:schema_v8_migrated`) to skip
    the unconditional PK rebuild on subsequent boots. The marker is
    re-checked under the advisory lock to defeat TOCTOU between workers.

    Per ROADMAP §regression doctrine, this is the first non-additive
    migration in the repo. The companion `# regression-fix (v8.0.0)`
    comments in tests/regression/test_v2_*.py acknowledge the rename.
    """
    # v10.0.6 — fast path. The KV marker is set after the full migration
    # completes; checking it here avoids taking the advisory lock and
    # re-issuing the PK rebuild on every worker boot.
    try:
        if ctx.kv.get(_SCHEMA_V8_MIGRATED_KV) == "1":
            return
    except Exception:  # noqa: BLE001 — fall through if KV is unavailable
        pass

    # Serialize concurrent pool-mode workers on the migration so the RENAME
    # below can never lose a race. The lock is transactional and auto-releases
    # at COMMIT; the rest of _bootstrap_schema can still parallelize.
    try:
        ctx.sql.execute(
            "SELECT pg_advisory_xact_lock(hashtext('otaku_v7_to_v8_migration'))"
        )
    except Exception:  # noqa: BLE001 — non-Postgres test backends skip silently
        pass

    # v10.0.6 — re-check the marker under the lock (TOCTOU). Another worker
    # may have completed the migration between our pre-lock check and the
    # lock acquisition.
    try:
        if ctx.kv.get(_SCHEMA_V8_MIGRATED_KV) == "1":
            return
    except Exception:  # noqa: BLE001
        pass

    # 1) Probe table existence via `to_regclass()` — a function call (NULL or
    #    OID) rather than an `information_schema` reference, which the host
    #    started blocking at v10.0.6.
    rows = ctx.sql.query(
        "SELECT to_regclass('otaku_user_anime') AS v7, "
        "to_regclass('otaku_user_media') AS v8"
    ) or []
    probe = rows[0] if rows else {}
    has_v7 = probe.get("v7") is not None
    has_v8 = probe.get("v8") is not None

    # v10.0.8 — restructured branching. The three cases:
    #
    #   (a) Fresh install (neither table exists). `_SCHEMA_DDL` later will
    #       create otaku_user_media with the wide PK inline; no migration
    #       work needed. Set marker + return.
    #
    #   (b) Already v8 (v8 exists, v7 doesn't). Could be a true v8 fresh
    #       install where v10.0.6's bootstrap created the table but the
    #       KV marker got wiped (e.g., plugin reinstall keeps SQL state
    #       but clears KV). No migration work needed — _SCHEMA_DDL's
    #       CREATE TABLE IF NOT EXISTS is a no-op and any later ALTER ADD
    #       COLUMN IF NOT EXISTS is a no-op. Set marker + return.
    #
    #   (c) Upgrade from v7 (v7 exists, v8 doesn't). Run the full rename
    #       dance. This is the only branch that issues migration DDL.
    #
    # The fourth combo (both tables exist) is a stuck state from a v8.0.0
    # bug; v8.0.1's doctrine treats it as "v8 is canonical" — same as (b),
    # but we also drop the v7 zombie for cleanliness.
    if not has_v7 and not has_v8:
        # (a) Fresh install.
        try:
            ctx.kv.set(_SCHEMA_V8_MIGRATED_KV, "1")
        except Exception:  # noqa: BLE001
            pass
        return
    if has_v8 and not has_v7:
        # (b) Already v8. Marker may be unset (e.g., KV wipe via reinstall).
        #
        # v10.0.12 — "table renamed" does NOT imply "migration finished" (the
        # rate limit can abort branch (c) at its final ADD CONSTRAINT, leaving
        # the table PK-less). The wide-PK verification/repair that v10.0.11
        # put inline here was unreachable for tenants whose migrated marker
        # was already set, so it moved to `_ensure_wide_pk`, which
        # _bootstrap_schema runs after EVERY _migrate_v7_to_v8 return —
        # including the marker fast-paths above. This branch only marks.
        try:
            ctx.kv.set(_SCHEMA_V8_MIGRATED_KV, "1")
        except Exception:  # noqa: BLE001
            pass
        return
    if has_v7 and has_v8:
        # Edge: both exist (v8.0.0 stuck state). Drop the v7 zombie.
        ctx.sql.execute("DROP TABLE IF EXISTS otaku_user_anime")
        try:
            ctx.kv.set(_SCHEMA_V8_MIGRATED_KV, "1")
        except Exception:  # noqa: BLE001
            pass
        return

    # (c) Upgrade path: has_v7 and not has_v8. Run the rename dance.
    ctx.sql.execute("ALTER TABLE otaku_user_anime RENAME TO otaku_user_media")

    # 2) Clean up the old v7 index name. v10.0.6's `ALTER INDEX … RENAME
    #    TO …` crashed the host on 2026-05-14 with "SQL statement type not
    #    allowed" — the host's allowlist (ALTER TABLE, CREATE INDEX,
    #    CREATE TABLE, DELETE, DROP INDEX, DROP TABLE, INSERT, SELECT,
    #    UPDATE) does NOT include ALTER INDEX. v10.0.8 uses DROP INDEX
    #    (allowed) and relies on `_SCHEMA_INDEX_DDL`'s
    #    `CREATE INDEX IF NOT EXISTS otaku_user_media_user_status_added_idx`
    #    later in `_bootstrap_schema` to recreate under the v8 name.
    ctx.sql.execute(
        "DROP INDEX IF EXISTS otaku_user_anime_user_status_added_idx"
    )

    # 3) Add the media_type column. ADD COLUMN IF NOT EXISTS is idempotent.
    ctx.sql.execute(
        "ALTER TABLE otaku_user_media "
        "ADD COLUMN IF NOT EXISTS media_type TEXT NOT NULL DEFAULT 'anime'"
    )

    # 4) Widen the primary key. Postgres can't widen a PK in place. The
    #    DROP IF EXISTS pair handles (narrow PK | already-wide PK | no PK)
    #    uniformly. The KV marker set at the end short-circuits this on
    #    subsequent boots so the rebuild only runs once per tenant.
    ctx.sql.execute(
        "ALTER TABLE otaku_user_media "
        "DROP CONSTRAINT IF EXISTS otaku_user_anime_pkey"
    )
    ctx.sql.execute(
        "ALTER TABLE otaku_user_media "
        "DROP CONSTRAINT IF EXISTS otaku_user_media_pkey"
    )
    ctx.sql.execute(
        "ALTER TABLE otaku_user_media "
        "ADD CONSTRAINT otaku_user_media_pkey "
        "PRIMARY KEY (user_id, media_id, media_type)"
    )

    # v10.0.6 — mark the full migration complete. The fast-path check at
    # the top of this function will short-circuit subsequent boots.
    try:
        ctx.kv.set(_SCHEMA_V8_MIGRATED_KV, "1")
    except Exception:  # noqa: BLE001
        pass


# v10.0.7 — the host enforces a strict DDL rate limit (5 statements/hour
# per tenant). A fresh install of v10.0.6 hit the cap at the 6th DDL and
# crashed `on_ready` before half the tables existed.
#
# v10.0.7 makes bootstrap **resilient and resumable**:
#   1) A schema-version KV marker (`otaku:schema_version`) short-circuits
#      the whole bootstrap once the install is complete. Subsequent boots
#      issue ZERO DDL.
#   2) On `RateLimitError` mid-bootstrap we log a warning and return
#      cleanly — the marker stays unset, so the next worker boot resumes.
#   3) v2.1/v2.2 ALTER ADD COLUMN statements were folded into the
#      `otaku_user_media` CREATE TABLE for fresh installs (the ALTERs
#      remain in the bootstrap list to cover existing v2.0-era tables
#      that don't yet have the columns).
#
# v10.0.11 — the v10.0.7 design was NOT actually resumable: every boot
# replayed the already-done DDLs from the top as IF NOT EXISTS no-ops, and
# the host's limiter counts statements *received*, not statements that did
# work (proven live 2026-06-11: boot ran DDLs #1-#5, rate-limited at #6;
# the next boot would burn the same 5 slots on #1-#5 again — zero forward
# progress, a fresh install never finished its 16 statements). The fix is a
# KV cursor (`otaku:schema_ddl_idx`) persisted after each successful
# statement; resume starts at the cursor, so each boot spends its budget
# only on DDL that still needs to run (a fresh install heals in
# ceil(16/5) = 4 boots, ~1h apart).
#
# RULE: `_SCHEMA_DDL_SEQUENCE` is APPEND-ONLY. The cursor stores an index
# into it; reordering or inserting mid-list makes old cursors point at the
# wrong statement. New versions append (every statement is IF NOT EXISTS /
# additive, so append order is always safe).
_SCHEMA_VERSION_KV = "otaku:schema_version"
# v10.0.8 keeps this at "10.0.7" deliberately — the v10.0.8 changes are
# pure bug fixes (migration ALTER INDEX → DROP INDEX, /stats refactor,
# /recommend diagnostic). The schema shape didn't change, so existing
# v10.0.7 markers stay valid and those installs short-circuit at the
# top of `_bootstrap_schema` without re-running anything.
_CURRENT_SCHEMA_VERSION = "10.0.7"

# v10.0.11 — resume cursor: index of the next _SCHEMA_DDL_SEQUENCE entry to
# execute. Persisted after every successful statement; absent/garbled values
# fall back to 0 (full replay — the pre-cursor behavior, still correct via
# IF NOT EXISTS, just budget-hungry).
_SCHEMA_DDL_CURSOR_KV = "otaku:schema_ddl_idx"

# Ordered, APPEND-ONLY (see the rule above). Labels feed the rate-limit log
# so operators can see exactly where bootstrap stopped and what remains.
_SCHEMA_DDL_SEQUENCE: tuple = (
    ("v2.0 otaku_user_media", _SCHEMA_DDL),
    ("v2.0 user/status/added index", _SCHEMA_INDEX_DDL),
    ("v2.1 rating column", _SCHEMA_RATING_DDL),
    ("v2.2 episodes_watched column", _SCHEMA_EPISODES_DDL),
    ("v3.0 server watchlist", _SCHEMA_SERVER_WATCHLIST_DDL),
    ("v3.2 watch parties", _SCHEMA_WATCH_PARTY_DDL),
    ("v3.2 watch party members", _SCHEMA_WATCH_PARTY_MEMBERS_DDL),
    ("v4.0 notifications", _SCHEMA_NOTIFICATIONS_DDL),
    ("v7.0 reviews", _SCHEMA_REVIEWS_DDL),
    ("v7.1 aotw polls", _SCHEMA_AOTW_POLLS_DDL),
    ("v7.1 aotw candidates", _SCHEMA_AOTW_CANDIDATES_DDL),
    ("v7.1 aotw votes", _SCHEMA_AOTW_VOTES_DDL),
    ("v7.2 polls", _SCHEMA_POLLS_DDL),
    ("v7.2 poll options", _SCHEMA_POLL_OPTIONS_DDL),
    ("v7.2 poll votes", _SCHEMA_POLL_VOTES_DDL),
    ("v10.0 achievements", _SCHEMA_ACHIEVEMENTS_DDL),
)


def _log_bootstrap_rate_limit(ctx: Context, exc: Exception, *, remaining: int, next_ddl: str) -> None:
    """Log the bootstrap rate-limit checkpoint. Keeps the historic message
    string (dashboards/triage grep for it) and adds where we stopped.

    Note: `retry_after` is the SDK's synthetic 5s default when the host error
    carries no reset timing — the real budget window is the host's ~1 hour;
    `remaining_ddl`/`next_ddl` are the actionable fields.
    """
    try:
        ctx.log(
            "schema bootstrap rate-limited; will resume on next worker boot",
            level="warning",
            tags=["bootstrap", "rate-limit"],
            retry_after=getattr(exc, "retry_after", None),
            remaining_ddl=str(remaining),
            next_ddl=next_ddl,
        )
    except Exception:  # noqa: BLE001
        pass


def _bootstrap_schema(ctx: Context) -> None:
    """Create the otaku_user_media table, its index, and additive columns. Idempotent.

    DDLs land in version order. New columns added in later versions go here
    with ADD COLUMN IF NOT EXISTS so the bootstrap stays a single source of
    truth across every upgrade path. The v7→v8 rename runs first so the
    `CREATE TABLE IF NOT EXISTS otaku_user_media` below is a no-op on
    upgraded installs and a real create on fresh installs.

    v10.0.7 — fast path: if `otaku:schema_version` matches the current
    version, return without touching SQL. The host's DDL rate limit (5
    statements/hour) bites on full re-runs; this gates them to once
    per tenant.
    """
    # v10.0.7 fast path.
    try:
        if ctx.kv.get(_SCHEMA_VERSION_KV) == _CURRENT_SCHEMA_VERSION:
            # v10.0.12 — one-time wide-PK verification for tenants that
            # completed bootstrap while the PK heal was unreachable
            # (v10.0.11): one kv.get per boot, one SELECT probe ever.
            # v10.0.13 — the marker READ gets its own guard so a kv.get
            # failure is not mislabeled "heal failed" (skip the probe this
            # boot; retry when KV recovers).
            try:
                pk_unverified = ctx.kv.get(_SCHEMA_PK_VERIFIED_KV) != "1"
            except Exception:  # noqa: BLE001
                pk_unverified = False
                try:
                    ctx.log(
                        "pk-verified marker read failed; skipping probe this boot",
                        level="warning",
                        tags=["bootstrap", "kv"],
                    )
                except Exception:  # noqa: BLE001
                    pass
            if pk_unverified:
                try:
                    # v10.0.13 — the marker is set ONLY on a positive
                    # verification; an indeterminate probe (False) retries
                    # next boot without certifying anything.
                    if _ensure_wide_pk(ctx):
                        try:
                            ctx.kv.set(_SCHEMA_PK_VERIFIED_KV, "1")
                        except Exception:  # noqa: BLE001 — self-corrects
                            try:
                                ctx.log(
                                    "pk-verified marker write failed; will retry next boot",
                                    level="warning",
                                    tags=["bootstrap", "kv"],
                                )
                            except Exception:  # noqa: BLE001
                                pass
                except Exception as exc:  # noqa: BLE001 — heal must never
                    # block boot; the marker stays unset so the next boot
                    # retries. But never silently (v10.0.12): an unlogged
                    # genuine failure here would re-burn repair DDL every
                    # boot with zero diagnostics.
                    try:
                        ctx.log(
                            "wide-PK heal failed; will retry next boot",
                            level="error",
                            tags=["bootstrap", "pk-heal"],
                            error=f"{type(exc).__name__}: {exc}"[:300],
                        )
                    except Exception:  # noqa: BLE001
                        pass
            return
    except Exception:  # noqa: BLE001
        pass

    # v10.0.12 — cursor read moved above the migration so its rate-limit log
    # can report an accurate remaining count.
    start_idx = 0
    try:
        raw_cursor = ctx.kv.get(_SCHEMA_DDL_CURSOR_KV)
        if isinstance(raw_cursor, str) and raw_cursor.isdigit():
            start_idx = min(int(raw_cursor), len(_SCHEMA_DDL_SEQUENCE))
    except Exception:  # noqa: BLE001 — unreadable cursor → full replay
        pass

    try:
        # v8.0.0 — must run first; later DDLs assume the new table name. Runs
        # outside the cursor: it has its own probe/marker idempotency and its
        # statement count varies by branch.
        _migrate_v7_to_v8(ctx)
    except RateLimitError as exc:
        _log_bootstrap_rate_limit(
            ctx, exc,
            remaining=len(_SCHEMA_DDL_SEQUENCE) - start_idx,
            next_ddl="v7→v8 migration (unfinished)",
        )
        return

    # v10.0.12 — wide-PK heal, AFTER the migration returns so it covers the
    # marker fast-paths that skip the migration body entirely. Guarded
    # separately from the migration so a heal failure can NEVER block table
    # creation in the DDL loop below. v10.0.13 — the outcome is tracked:
    # setting otaku:schema_pk_verified at completion after a FAILED heal
    # would permanently gate off the post-completion retry backstop (the
    # exact marker-set-while-broken lock-in class this machinery exists to
    # eliminate).
    heal_ok = True
    try:
        heal_ok = _ensure_wide_pk(ctx)
    except RateLimitError as exc:
        _log_bootstrap_rate_limit(
            ctx, exc,
            remaining=len(_SCHEMA_DDL_SEQUENCE) - start_idx,
            next_ddl="wide-PK heal (unfinished)",
        )
        return
    except Exception as exc:  # noqa: BLE001 — log and continue into the loop
        heal_ok = False
        try:
            ctx.log(
                "wide-PK heal failed; continuing bootstrap",
                level="error",
                tags=["bootstrap", "pk-heal"],
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
        except Exception:  # noqa: BLE001
            pass

    # v10.0.11 — resumable DDL loop. Start at the persisted cursor so a boot
    # never re-spends rate-limit budget on statements that already succeeded
    # (see the design comment above _SCHEMA_VERSION_KV). The per-version
    # comments from the old inline list live in _SCHEMA_DDL_SEQUENCE's labels.

    for idx in range(start_idx, len(_SCHEMA_DDL_SEQUENCE)):
        label, ddl = _SCHEMA_DDL_SEQUENCE[idx]
        try:
            ctx.sql.execute(ddl)
        except RateLimitError as exc:
            # The host caps DDL at ~5 statements/hour. The cursor already
            # points at this statement, so the next worker boot resumes
            # exactly here with a fresh budget.
            _log_bootstrap_rate_limit(
                ctx, exc,
                remaining=len(_SCHEMA_DDL_SEQUENCE) - idx,
                next_ddl=label,
            )
            return
        try:
            ctx.kv.set(_SCHEMA_DDL_CURSOR_KV, str(idx + 1))
        except Exception:  # noqa: BLE001 — a transiently lost cursor means a
            # budget-hungry replay; a PERSISTENTLY failing cursor write on a
            # fresh install degrades to the v10.0.11 no-progress mode (replay
            # to the same rate-limit point each boot) until KV recovers.
            pass

    # v10.0.7 — bootstrap completed fully; gate subsequent boots.
    try:
        ctx.kv.set(_SCHEMA_VERSION_KV, _CURRENT_SCHEMA_VERSION)
        # v10.0.13 — pk_verified is set ONLY when this boot's heal actually
        # succeeded. After a failed heal the marker stays unset, so the
        # version-marker fast path's one-time probe retries (and logs) on
        # every subsequent boot until the heal lands.
        if heal_ok:
            ctx.kv.set(_SCHEMA_PK_VERIFIED_KV, "1")
    except Exception:  # noqa: BLE001
        pass


@plugin.on_install
def _on_install(ctx: Context) -> None:
    _bootstrap_schema(ctx)


@plugin.on_ready
def _on_ready(ctx: Context) -> None:
    # Pool-mode safety: on_install doesn't fire on v1.x→v2.0 upgrades, but
    # on_ready does (synchronously, before the first event handler).
    _bootstrap_schema(ctx)


# ── SQL helpers ──────────────────────────────────────────────────────────────

def _resolve_last_anime_id(ctx: Context, user_id: str) -> int | None:
    """Read the user's cached last-anime ID and coerce it to int, or None."""
    if not user_id:
        return None
    cached = ctx.kv.get(f"last_anime:user:{user_id}")
    if cached is None:
        return None
    try:
        return int(cached)
    except (TypeError, ValueError):
        return None


def _upsert_user_media(
    ctx: Context,
    user_id: str,
    media_id: int,
    *,
    media_type: str = "anime",
    status: str | None = None,
    is_favorite: bool | None = None,
) -> None:
    """Insert or update a row in otaku_user_media, mutating only the fields passed.

    `media_type` defaults to 'anime' so every v7 caller works unchanged. Manga
    handlers pass `media_type='manga'` explicitly.
    """
    insert_status = status if status is not None else "watching"
    insert_favorite = bool(is_favorite) if is_favorite is not None else False
    # v10.0.5 — replaced the prior `f"ON CONFLICT ... SET {update_sql}"`
    # builder with static SQL + COALESCE. %s/%s are NULL when the caller
    # didn't pass that field, so COALESCE preserves the existing row's
    # value; otherwise it overwrites with the caller's value. Matches
    # the SDK's "no f-string SQL" rule without changing observable behavior.
    update_status = status  # NULL = "don't touch this field"
    update_favorite = bool(is_favorite) if is_favorite is not None else None

    ctx.sql.execute(
        "INSERT INTO otaku_user_media (user_id, media_id, media_type, status, is_favorite) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id, media_id, media_type) DO UPDATE SET "
        "  status = COALESCE(%s, otaku_user_media.status), "
        "  is_favorite = COALESCE(%s, otaku_user_media.is_favorite)",
        [user_id, media_id, media_type, insert_status, insert_favorite,
         update_status, update_favorite],
    )


# Back-compat alias — v7 callers that used the anime-coded name keep working
# without changes. New code paths should call `_upsert_user_media` directly.
_upsert_user_anime = _upsert_user_media


def _is_favorite(ctx: Context, user_id: str, media_id: int, *, media_type: str = "anime") -> bool:
    row = ctx.sql.query_one(
        "SELECT is_favorite FROM otaku_user_media "
        "WHERE user_id = %s AND media_id = %s AND media_type = %s",
        [user_id, media_id, media_type],
    )
    return bool(row and row.get("is_favorite"))


# ── Slash command handlers ───────────────────────────────────────────────────

@plugin.on_slash_command("anime")
def cmd_anime(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    query = (opts.get("query") or "").strip()
    if not query:
        ctx.interaction.respond(content=T_for(ctx, user_id, "ANIME_USAGE"), ephemeral=True)
        return

    ctx.interaction.defer()
    # v9.1: multi-source search. AniList primary; falls through to MAL
    # (Jikan v4) then Kitsu if AniList misses or fails. Returns a canonical
    # media dict with `source` annotation; the embed builders read AniList-
    # shaped fields directly. /similar, /random etc. stay AniList-only
    # because they depend on AniList-specific schemas.
    media = _search_media(ctx, query, media_type="anime")
    if media is None:
        _reply_error(
            ctx,
            T_for(ctx, user_id, "ANIME_NOT_FOUND", query=_truncate(query, 80)),
            deferred=True,
        )
        return

    media_id = media.get("id")
    # Only cache `last_anime` for AniList results — the cache key is keyed
    # on AniList ids, and downstream commands (/similar, /watch, /rate,
    # etc.) all assume AniList ids. MAL/Kitsu fallback results are still
    # rendered but don't propagate to the cache. v9.1 known limitation
    # documented in CHANGELOG.
    source = media.get("source") or "anilist"
    if source == "anilist" and media_id is not None and user_id:
        ctx.kv.set(f"last_anime:user:{user_id}", media_id, ttl_seconds=LAST_ANIME_TTL)

    # Local tracking lookup is anime-only and keyed on AniList ids; skip
    # for non-AniList sources.
    progress, rating = (0, None)
    if source == "anilist":
        progress, rating = _get_user_tracking(ctx, user_id, int(media_id or 0))
    embed = _make_anime_embed(media, user_progress=progress, user_rating=rating)

    # Source attribution in the footer + "Open on X" button label.
    source_label = SOURCE_LABEL.get(source, "AniList")
    embed["footer"] = {"text": f"Data from {source_label}"}

    buttons = []
    # /similar only meaningful for AniList ids — skip for MAL/Kitsu fallbacks.
    if source == "anilist" and media_id is not None:
        buttons.append(Button("Similar", custom_id=f"otaku:similar:{media_id}", style="primary", emoji="🔁"))
    site_url = media.get("siteUrl")
    if site_url:
        buttons.append(Button(f"Open on {source_label}", url=site_url, style="link", emoji="🌐"))
    components = [ActionRow(*buttons)] if buttons else None
    ctx.interaction.followup(embeds=[embed], components=components)


@plugin.on_slash_command("discover")
def cmd_discover(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    genre = (opts.get("genre") or "").strip()
    sort_key = (opts.get("sort") or "popular").strip().lower()
    if sort_key not in SORT_MAP:
        sort_key = "popular"
    if not genre:
        ctx.interaction.respond(content=S.DISCOVER_USAGE, ephemeral=True)
        return

    ctx.interaction.defer()
    _render_discover(ctx, genre, sort_key, page=1, deferred=True)


@plugin.on_slash_command("trending")
def cmd_trending(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    ctx.interaction.defer()
    _render_trending(ctx, page=1, deferred=True)


@plugin.on_slash_command("similar")
def cmd_similar(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    query = (opts.get("anime") or "").strip()

    if query:
        ctx.interaction.defer()
        data = _anilist_query(ctx, QUERY_SIMILAR_BY_NAME, {"q": query})
        if data is None:
            _reply_anilist_failure(ctx, deferred=True)
            return
        media = data.get("Media")
        if not media:
            _reply_error(ctx, S.ANIME_NOT_FOUND.format(query=_truncate(query, 80)), deferred=True)
            return
        # Cache the resolved anime as the user's last lookup too.
        mid = media.get("id")
        if mid is not None and user_id:
            ctx.kv.set(f"last_anime:user:{user_id}", mid, ttl_seconds=LAST_ANIME_TTL)
        _render_similar(ctx, media, deferred=True)
        return

    # No query — look up cached last anime.
    cached = ctx.kv.get(f"last_anime:user:{user_id}") if user_id else None
    if cached is None:
        ctx.interaction.respond(content=S.SIMILAR_NO_CACHE, ephemeral=True)
        return

    ctx.interaction.defer()
    try:
        media_id = int(cached)
    except (TypeError, ValueError):
        _reply_error(ctx, S.SIMILAR_INVALID_CACHED, deferred=True)
        return
    data = _anilist_query(ctx, QUERY_SIMILAR_BY_ID, {"id": media_id})
    if data is None or not data.get("Media"):
        _reply_anilist_failure(
            ctx,
            deferred=True,
            fallback=S.SIMILAR_FETCH_FAIL_CACHED,
        )
        return
    _render_similar(ctx, data["Media"], deferred=True)


# Cap on /random's page roll. Beyond this AniList's results get very obscure,
# and the pageInfo.lastPage for a popular genre easily breaks 1000.
RANDOM_MAX_PAGE = 50


@plugin.on_slash_command("random")
def cmd_random(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    genre_raw = (opts.get("genre") or "").strip()
    genre: str | None = genre_raw or None

    ctx.interaction.defer()
    meta = _anilist_query(ctx, QUERY_RANDOM_META, {"genre": genre}, cache=True)
    if meta is None:
        _reply_anilist_failure(ctx, deferred=True)
        return

    page_info = ((meta.get("Page") or {}).get("pageInfo")) or {}
    last_page = int(page_info.get("lastPage") or 1)
    if last_page < 1:
        last_page = 1
    upper = min(last_page, RANDOM_MAX_PAGE)
    page = random.randint(1, upper) if upper >= 1 else 1

    pick = _anilist_query(ctx, QUERY_RANDOM_PICK, {"genre": genre, "page": page})
    media_list = ((pick or {}).get("Page") or {}).get("media") or []
    if not media_list and page != 1:
        # Niche genre with sparse later pages — fall back to page 1.
        pick = _anilist_query(ctx, QUERY_RANDOM_PICK, {"genre": genre, "page": 1})
        media_list = ((pick or {}).get("Page") or {}).get("media") or []
    if not media_list:
        label = S.RANDOM_FILTER_LABEL.format(genre=genre_raw) if genre else S.RANDOM_NO_FILTER_LABEL
        _reply_error(ctx, S.RANDOM_NO_RESULTS.format(label=label), deferred=True)
        return

    media = media_list[0]
    media_id = media.get("id")
    if media_id is not None and user_id:
        ctx.kv.set(f"last_anime:user:{user_id}", media_id, ttl_seconds=LAST_ANIME_TTL)

    progress, rating = _get_user_tracking(ctx, user_id, int(media_id or 0))
    embed = _make_anime_embed(media, user_progress=progress, user_rating=rating)
    if genre:
        embed["title"] = f"🎲 {embed['title']}"
    buttons = [Button("Similar", custom_id=f"otaku:similar:{media_id}", style="primary", emoji="🔁")]
    site_url = media.get("siteUrl")
    if site_url:
        buttons.append(Button("Open on AniList", url=site_url, style="link", emoji="🌐"))
    ctx.interaction.followup(embeds=[embed], components=[ActionRow(*buttons)])


@plugin.on_slash_command("character")
def cmd_character(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    query = (opts.get("query") or "").strip()
    if not query:
        ctx.interaction.respond(content=S.CHARACTER_USAGE, ephemeral=True)
        return

    ctx.interaction.defer()
    data = _anilist_query(ctx, QUERY_CHARACTER, {"q": query.lower()}, cache=True)
    if data is None:
        _reply_anilist_failure(ctx, deferred=True)
        return
    char = data.get("Character")
    if not char:
        _reply_error(ctx, S.CHARACTER_NOT_FOUND.format(query=_truncate(query, 80)), deferred=True)
        return

    embed = _make_character_embed(char)
    components = None
    site_url = char.get("siteUrl")
    if site_url:
        components = [ActionRow(Button("Open on AniList", url=site_url, style="link", emoji="🌐"))]
    ctx.interaction.followup(embeds=[embed], components=components)


# ── v8.1.0 — /voice-actor and /staff ────────────────────────────────────────
#
# Both commands query AniList's single `Staff` type; the embed builders pull
# different framings of the same record. /voice-actor highlights `characters`
# (notable roles + parent media). /staff highlights `staffMedia` (production
# credits with role). Same first-match-only contract as /character — AniList
# search returns multiple hits but the embed surfaces only the top match.


@plugin.on_slash_command("voice-actor")
def cmd_voice_actor(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    query = (opts.get("query") or "").strip()
    if not query:
        ctx.interaction.respond(content=S.VOICE_ACTOR_USAGE, ephemeral=True)
        return

    ctx.interaction.defer()
    data = _anilist_query(ctx, QUERY_STAFF, {"q": query.lower()}, cache=True)
    if data is None:
        _reply_anilist_failure(ctx, deferred=True)
        return
    staff = data.get("Staff")
    if not staff:
        _reply_error(ctx, S.VOICE_ACTOR_NOT_FOUND.format(query=_truncate(query, 80)), deferred=True)
        return

    embed = _make_voice_actor_embed(staff)
    components = None
    site_url = staff.get("siteUrl")
    if site_url:
        components = [ActionRow(Button("Open on AniList", url=site_url, style="link", emoji="🌐"))]
    ctx.interaction.followup(embeds=[embed], components=components)


@plugin.on_slash_command("staff")
def cmd_staff(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    query = (opts.get("query") or "").strip()
    if not query:
        ctx.interaction.respond(content=S.STAFF_USAGE, ephemeral=True)
        return

    ctx.interaction.defer()
    data = _anilist_query(ctx, QUERY_STAFF, {"q": query.lower()}, cache=True)
    if data is None:
        _reply_anilist_failure(ctx, deferred=True)
        return
    staff = data.get("Staff")
    if not staff:
        _reply_error(ctx, S.STAFF_NOT_FOUND.format(query=_truncate(query, 80)), deferred=True)
        return

    embed = _make_staff_embed(staff)
    components = None
    site_url = staff.get("siteUrl")
    if site_url:
        components = [ActionRow(Button("Open on AniList", url=site_url, style="link", emoji="🌐"))]
    ctx.interaction.followup(embeds=[embed], components=components)


# ── v8.2.0 — /studio ─────────────────────────────────────────────────────────


@plugin.on_slash_command("studio")
def cmd_studio(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    query = (opts.get("query") or "").strip()
    if not query:
        ctx.interaction.respond(content=S.STUDIO_USAGE, ephemeral=True)
        return

    ctx.interaction.defer()
    data = _anilist_query(ctx, QUERY_STUDIO, {"q": query.lower()}, cache=True)
    if data is None:
        _reply_anilist_failure(ctx, deferred=True)
        return
    studio = data.get("Studio")
    if not studio:
        _reply_error(ctx, S.STUDIO_NOT_FOUND.format(query=_truncate(query, 80)), deferred=True)
        return

    embed = _make_studio_embed(studio)
    components = None
    site_url = studio.get("siteUrl")
    if site_url:
        components = [ActionRow(Button("Open on AniList", url=site_url, style="link", emoji="🌐"))]
    ctx.interaction.followup(embeds=[embed], components=components)


# ── v8.3.0 — /character-popular (closes Phase 8) ────────────────────────────
#
# Global leaderboard sorted by AniList `favourites` count. Paginated via
# `otaku:popchar:<page>` custom_ids (same pattern as /trending). 5 per page.

CHARACTER_POPULAR_PER_PAGE = 5


def _render_character_popular(ctx: Context, page: int, *, deferred: bool) -> None:
    # v10.0.1: cache every page (see _render_discover comment).
    data = _anilist_query(
        ctx,
        QUERY_CHARACTER_POPULAR,
        {"page": page, "perPage": CHARACTER_POPULAR_PER_PAGE},
        cache=True,
    )
    if data is None:
        _reply_anilist_failure(ctx, deferred=deferred)
        return

    page_obj = (data.get("Page") or {})
    chars = page_obj.get("characters") or []
    has_next = bool((page_obj.get("pageInfo") or {}).get("hasNextPage"))

    if not chars:
        _reply_error(ctx, S.CHARACTER_POPULAR_EMPTY, deferred=deferred)
        return

    lines = []
    rank_start = (page - 1) * CHARACTER_POPULAR_PER_PAGE + 1
    for idx, c in enumerate(chars):
        rank = rank_start + idx
        name = (c.get("name") or {}).get("full") or "?"
        favs = c.get("favourites") or 0
        site = c.get("siteUrl") or ""
        anchor = f"[{name}]({site})" if site else name
        parent = (((c.get("media") or {}).get("nodes")) or [None])[0]
        if parent:
            ptitle = _format_title(parent)
            purl = parent.get("siteUrl") or ""
            parent_anchor = f"[{ptitle}]({purl})" if purl else ptitle
            lines.append(f"`#{rank:>3}` **{anchor}** · ❤ {favs:,} — {parent_anchor}")
        else:
            lines.append(f"`#{rank:>3}` **{anchor}** · ❤ {favs:,}")

    embed = {
        "title": S.CHARACTER_POPULAR_HEADER,
        "description": "\n".join(lines),
        "color": ANILIST_COLOR,
        "footer": {"text": S.CHARACTER_POPULAR_FOOTER.format(page=page)},
    }

    prev_id = f"otaku:popchar:{page - 1}" if page > 1 else None
    next_id = f"otaku:popchar:{page + 1}" if has_next else None
    components = [_page_buttons(prev_id, next_id)]

    if deferred:
        ctx.interaction.followup(embeds=[embed], components=components)
    else:
        ctx.interaction.respond(embeds=[embed], components=components)


@plugin.on_slash_command("character-popular")
def cmd_character_popular(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    ctx.interaction.defer()
    _render_character_popular(ctx, page=1, deferred=True)


# Static usage examples — one per command. The /help embed merges these with the
# descriptions in manifest.json so the help text never drifts behind a new
# slash command being declared.
_HELP_EXAMPLES = {
    "anime":     "`/anime query: Your Name`",
    "discover":  "`/discover genre: Action sort: trending`",
    "trending":  "`/trending`",
    "similar":   "`/similar` (uses your last lookup) or `/similar anime: Your Name`",
    "random":    "`/random` or `/random genre: Romance`",
    "character":     "`/character query: Edward Elric`",
    "voice-actor":   "`/voice-actor query: Aoi Yuuki`",
    "staff":         "`/staff query: Hayao Miyazaki`",
    "studio":        "`/studio query: Trigger`",
    "character-popular": "`/character-popular`",
    "find":          "`/find description: slow romance set in school with supernatural twist`",
    "preferences":   "`/preferences spoilers: hide` or `/preferences language: ja`",
    "achievements":  "`/achievements` (your own) or `/achievements user: @them`",
    "help":      "`/help`",
    "genres":    "`/genres`",
}


def _load_manifest_slash_commands() -> list[dict]:
    """Read manifest.json at boot. Cached after the first call."""
    global _MANIFEST_COMMANDS_CACHE
    if _MANIFEST_COMMANDS_CACHE is not None:
        return _MANIFEST_COMMANDS_CACHE
    try:
        from pathlib import Path as _Path
        manifest_path = _Path(__file__).resolve().parent / "manifest.json"
        with manifest_path.open() as fh:
            manifest = json.load(fh)
        _MANIFEST_COMMANDS_CACHE = list(manifest.get("slash_commands") or [])
    except Exception:  # noqa: BLE001 — /help must never crash
        _MANIFEST_COMMANDS_CACHE = []
    return _MANIFEST_COMMANDS_CACHE


_MANIFEST_COMMANDS_CACHE: list[dict] | None = None


# ── v8.0.0 — /manga, /manga-discover, /manga-favorites ──────────────────────
#
# Manga commands mirror /anime, /discover, /favorite (subset). The schema is
# the shared otaku_user_media table; manga rows carry media_type='manga' so
# the v7 anime queries stay anime-only via the AND media_type = 'anime'
# filters added across __main__.py. /manga-watch, /manga-rate, /manga-progress,
# /manga-list, /manga-import remain on the roadmap for v8.x patches; v8.0
# ships just search + discover + favorites per the explicit roadmap scope.
#
# KV: last_manga:user:<id> mirrors last_anime:user:<id> (7-day TTL).
# AniList queries: QUERY_MANGA_* constants in the SQL-schema block above.

LAST_MANGA_KV_PREFIX = "last_manga:user"


@plugin.on_slash_command("manga")
def cmd_manga(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    query = (opts.get("query") or "").strip()
    if not query:
        ctx.interaction.respond(content=S.MANGA_USAGE, ephemeral=True)
        return

    ctx.interaction.defer()
    # v9.1: multi-source search (AniList → Jikan → Kitsu). See cmd_anime
    # for the same fallback semantics; manga responses come through the
    # same canonical pipeline.
    media = _search_media(ctx, query, media_type="manga")
    if media is None:
        _reply_error(ctx, S.MANGA_NOT_FOUND.format(query=_truncate(query, 80)), deferred=True)
        return

    media_id = media.get("id")
    source = media.get("source") or "anilist"
    # Only cache `last_manga` for AniList results — same rationale as
    # cmd_anime: the cache key is on AniList ids; v9.1 known limitation.
    if source == "anilist" and media_id is not None and user_id:
        ctx.kv.set(f"{LAST_MANGA_KV_PREFIX}:{user_id}", media_id, ttl_seconds=LAST_ANIME_TTL)

    progress, rating = (0, None)
    if source == "anilist":
        progress, rating = _get_user_tracking(ctx, user_id, int(media_id or 0), media_type="manga")
    embed = _make_manga_embed(media, user_progress=progress, user_rating=rating)

    source_label = SOURCE_LABEL.get(source, "AniList")
    embed["footer"] = {"text": f"Data from {source_label}"}

    buttons = []
    site_url = media.get("siteUrl")
    if site_url:
        buttons.append(Button(f"Open on {source_label}", url=site_url, style="link", emoji="🌐"))
    components = [ActionRow(*buttons)] if buttons else None
    ctx.interaction.followup(embeds=[embed], components=components)


def _render_manga_discover(
    ctx: Context, genre: str, sort_key: str, page: int, *, deferred: bool
) -> None:
    sort_const = SORT_MAP.get(sort_key, "POPULARITY_DESC")
    # v10.0.1: cache every page (see _render_discover comment).
    data = _anilist_query(
        ctx,
        QUERY_MANGA_DISCOVER,
        {"genre": genre, "sort": [sort_const], "page": page, "perPage": PER_PAGE},
        cache=True,
    )
    if data is None:
        _reply_anilist_failure(ctx, deferred=deferred)
        return
    page_obj = data.get("Page") or {}
    media_list = page_obj.get("media") or []
    has_next = bool((page_obj.get("pageInfo") or {}).get("hasNextPage"))

    if not media_list:
        _reply_error(
            ctx, S.MANGA_DISCOVER_NO_RESULTS.format(genre=genre), deferred=deferred
        )
        return

    header = f"📚 {genre.title()} manga — {sort_key.title()}"
    embed = _make_list_embed(media_list, header, page=page, has_next=has_next)
    prev_id = f"otaku:mpage:{genre}:{sort_key}:{page - 1}" if page > 1 else None
    next_id = f"otaku:mpage:{genre}:{sort_key}:{page + 1}" if has_next else None
    components = [_page_buttons(prev_id, next_id)]
    if deferred:
        ctx.interaction.followup(embeds=[embed], components=components)
    else:
        ctx.interaction.respond(embeds=[embed], components=components)


@plugin.on_slash_command("manga-discover")
def cmd_manga_discover(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    genre = (opts.get("genre") or "").strip()
    sort_key = (opts.get("sort") or "popular").strip().lower()
    if sort_key not in SORT_MAP:
        sort_key = "popular"
    if not genre:
        ctx.interaction.respond(content=S.MANGA_DISCOVER_USAGE, ephemeral=True)
        return

    ctx.interaction.defer()
    _render_manga_discover(ctx, genre, sort_key, page=1, deferred=True)


def _resolve_last_manga_id(ctx: Context, user_id: str) -> int | None:
    if not user_id:
        return None
    try:
        cached = ctx.kv.get(f"{LAST_MANGA_KV_PREFIX}:{user_id}")
    except Exception:  # noqa: BLE001
        return None
    if cached is None:
        return None
    try:
        return int(cached)
    except (TypeError, ValueError):
        return None


def _resolve_manga_by_arg(
    ctx: Context, user_id: str, manga_arg: str
) -> tuple[dict | None, str | None]:
    """Resolve the `manga:` option (or cached last_manga) to a manga media dict.

    Returns (media, error). Mirrors _resolve_media_by_anime_arg but uses
    QUERY_MANGA_SEARCH_ONE and the last_manga KV key.
    """
    manga_arg = (manga_arg or "").strip()
    if manga_arg:
        if manga_arg.isdigit():
            data = _anilist_query(ctx, QUERY_MANGA_BY_ID, {"id": int(manga_arg)}, cache=True)
        else:
            data = _anilist_query(ctx, QUERY_MANGA_SEARCH_ONE, {"q": manga_arg.lower()}, cache=True)
        if data is None:
            return None, None
        media = data.get("Media")
        if not media:
            return None, S.MANGA_NOT_FOUND.format(query=_truncate(manga_arg, 80))
        return media, None

    media_id = _resolve_last_manga_id(ctx, user_id)
    if media_id is None:
        return None, S.MANGA_FAVORITE_NO_CACHE
    data = _anilist_query(ctx, QUERY_MANGA_BY_ID, {"id": media_id}, cache=True)
    if data is None:
        return None, None
    media = data.get("Media")
    if not media:
        return None, S.MANGA_FAVORITE_NO_CACHE
    return media, None


@plugin.on_slash_command("manga-favorites")
def cmd_manga_favorites(ctx: Context, event: dict) -> None:
    """Toggle a manga as a favorite, OR (with no args + no `remove:`) list favorites.

    Behaviour table:
    - `manga:<arg>` present → favorite (or unfavorite if `remove: true`)
    - `manga:` empty, `remove:` false → favorite the user's cached /manga lookup
    - `manga:` empty, `remove:` true → unfavorite the cached /manga lookup
    - both empty AND no cache → show the user's manga favorites list
    """
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    manga_arg = (opts.get("manga") or "").strip()
    remove = bool(opts.get("remove"))

    # If neither arg nor remove flag set, AND there's no cached /manga lookup
    # to operate on, fall through to "show this user's manga favorites list".
    if not manga_arg and not remove and _resolve_last_manga_id(ctx, user_id) is None:
        ctx.interaction.defer(ephemeral=True)
        _render_manga_favorites(ctx, target_user_id=user_id, is_self=True)
        return

    ctx.interaction.defer(ephemeral=True)
    media, err = _resolve_manga_by_arg(ctx, user_id, manga_arg)
    if media is None:
        if err is not None:
            _reply_error(ctx, err, deferred=True)
        else:
            _reply_anilist_failure(ctx, deferred=True)
        return

    media_id = int(media.get("id") or 0)
    title = _format_title(media)
    if not media_id:
        _reply_error(ctx, S.MANGA_FAVORITE_NO_CACHE, deferred=True)
        return

    already = _is_favorite(ctx, user_id, media_id, media_type="manga")
    if remove:
        if not already:
            ctx.interaction.followup(
                content=S.MANGA_FAVORITE_NOT_PRESENT.format(title=title), ephemeral=True
            )
            return
        _upsert_user_media(
            ctx, user_id, media_id, media_type="manga", is_favorite=False
        )
        ctx.interaction.followup(
            content=S.MANGA_FAVORITE_REMOVED.format(title=title), ephemeral=True
        )
        ctx.kv.set(f"{LAST_MANGA_KV_PREFIX}:{user_id}", media_id, ttl_seconds=LAST_ANIME_TTL)
        return

    if already:
        ctx.interaction.followup(
            content=S.MANGA_FAVORITE_ALREADY.format(title=title), ephemeral=True
        )
        return
    _upsert_user_media(ctx, user_id, media_id, media_type="manga", is_favorite=True)
    ctx.kv.set(f"{LAST_MANGA_KV_PREFIX}:{user_id}", media_id, ttl_seconds=LAST_ANIME_TTL)
    ctx.interaction.followup(
        content=S.MANGA_FAVORITE_ADDED.format(title=title), ephemeral=True
    )


def _render_manga_favorites(
    ctx: Context, *, target_user_id: str, is_self: bool
) -> None:
    rows = ctx.sql.query(
        "SELECT media_id FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'manga' AND is_favorite = TRUE "
        "ORDER BY added_at DESC LIMIT %s",
        [target_user_id, PER_PAGE],
    ) or []
    if not rows:
        msg = S.MANGA_FAVORITES_EMPTY_OWN if is_self else S.MANGA_FAVORITES_EMPTY_OTHER.format(
            who=f"<@{target_user_id}>"
        )
        _reply_error(ctx, msg, deferred=True)
        return

    ids = [int(r["media_id"]) for r in rows if r.get("media_id") is not None]
    data = _anilist_query(ctx, QUERY_MANGA_BATCH, {"ids": sorted(ids)}, cache=True) if ids else None
    media_by_id: dict[int, dict] = {}
    if data:
        for m in ((data.get("Page") or {}).get("media") or []):
            mid = m.get("id")
            if isinstance(mid, int):
                media_by_id[mid] = m

    header = S.MANGA_FAVORITES_HEADER_OWN if is_self else S.MANGA_FAVORITES_HEADER_OTHER.format(
        who=f"<@{target_user_id}>"
    )
    media_list = [media_by_id[i] for i in ids if i in media_by_id]
    embed = _make_list_embed(media_list, header, page=1, has_next=False)
    ctx.interaction.followup(embeds=[embed], ephemeral=True)


# v10.0.6 — Discord caps an embed description at 4096 chars; the full
# 45-command help body comfortably exceeds that. Chunk well under the
# limit to leave headroom for the title/footer toward the 6000-char total.
_HELP_DESC_CHUNK = 3900
_HELP_MAX_EMBEDS = 10  # Discord's per-message cap


@plugin.on_slash_command("help")
def cmd_help(ctx: Context, event: dict) -> None:
    """List every otaku command, one per line. Reads from manifest.json so it auto-updates."""
    commands = _load_manifest_slash_commands()
    lines = []
    for cmd in commands:
        name = cmd.get("name") or ""
        if not name:
            continue
        desc = cmd.get("description") or ""
        example = _HELP_EXAMPLES.get(name, "")
        line = f"**`/{name}`** — {desc}"
        if example:
            line += f"\n  · Example: {example}"
        lines.append(line)

    if not lines:
        ctx.interaction.respond(
            embeds=[{
                "title": S.HELP_TITLE,
                "description": S.HELP_EMPTY,
                "color": ANILIST_COLOR,
                "footer": {"text": S.HELP_FOOTER},
            }],
            ephemeral=True,
        )
        return

    # Pack lines into ≤_HELP_DESC_CHUNK-char description chunks, one entry
    # per chunk separated by blank lines. Greedy fill — start a new chunk
    # only when the next line would push us over.
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else f"{current}\n\n{line}"
        if len(candidate) > _HELP_DESC_CHUNK and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    chunks = chunks[:_HELP_MAX_EMBEDS]
    total = len(chunks)
    embeds = []
    for i, body in enumerate(chunks):
        embed: dict = {"description": body, "color": ANILIST_COLOR}
        if i == 0:
            embed["title"] = S.HELP_TITLE
        if i == total - 1:
            embed["footer"] = {"text": S.HELP_FOOTER}
        embeds.append(embed)

    ctx.interaction.respond(embeds=embeds, ephemeral=True)


GENRES_KV_KEY = "genres:global"
GENRES_TTL = 24 * 60 * 60  # 24 hours


@plugin.on_slash_command("genres")
def cmd_genres(ctx: Context, event: dict) -> None:
    """Show AniList's canonical genre list. Cached in KV for 24h."""
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return

    cached = None
    try:
        cached = ctx.kv.get(GENRES_KV_KEY)
    except Exception:  # noqa: BLE001 — fall back to HTTP if KV trips
        cached = None

    if cached and isinstance(cached, list) and cached:
        genres = cached
    else:
        ctx.interaction.defer(ephemeral=True)
        data = _anilist_query(ctx, QUERY_GENRES, {}, cache=True)
        if data is None or not data.get("GenreCollection"):
            _reply_anilist_failure(
                ctx,
                deferred=True,
                fallback=S.GENRES_FETCH_FAIL,
            )
            return
        genres = data["GenreCollection"]
        try:
            ctx.kv.set(GENRES_KV_KEY, genres, ttl_seconds=GENRES_TTL)
        except Exception:  # noqa: BLE001 — KV failures don't block the reply
            pass
        embed = _genres_embed(genres)
        ctx.interaction.followup(embeds=[embed], ephemeral=True)
        return

    # Cached path — respond directly, no defer needed.
    ctx.interaction.respond(embeds=[_genres_embed(genres)], ephemeral=True)


def _genres_embed(genres: list[str]) -> dict:
    return {
        "title": "AniList genres",
        "description": "\n".join(f"• {g}" for g in genres) or S.GENRES_EMPTY,
        "color": ANILIST_COLOR,
        "footer": {"text": f"{len(genres)} genres · refreshed daily · use with /discover or /random"},
    }


# ── v2.0.0 — /favorite, /favorites, /watch, /list ───────────────────────────

def _resolve_media_by_anime_arg(
    ctx: Context, user_id: str, anime_arg: str
) -> tuple[dict | None, str | None]:
    """Resolve the `anime:` slash option (or last lookup) to a media dict.

    Returns (media, error_message). On success media is the dict and error is None.
    On failure media is None and error is a user-facing string.
    """
    anime_arg = (anime_arg or "").strip()
    if anime_arg:
        data = _anilist_query(ctx, QUERY_SEARCH_ONE, {"q": anime_arg.lower()}, cache=True)
        if data is None:
            return None, None  # caller surfaces the standard AniList failure
        media = data.get("Media")
        if not media:
            return None, S.ANIME_NOT_FOUND.format(query=_truncate(anime_arg, 80))
        return media, None

    # No argument — fall back to cached last lookup.
    media_id = _resolve_last_anime_id(ctx, user_id)
    if media_id is None:
        return None, S.FAVORITE_NO_CACHE
    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": media_id})
    if data is None:
        return None, None
    media = data.get("Media")
    if not media:
        return None, S.SIMILAR_INVALID_CACHED
    return media, None


@plugin.on_slash_command("favorite")
def cmd_favorite(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    anime_arg = (opts.get("anime") or "").strip()
    remove = bool(opts.get("remove"))

    ctx.interaction.defer(ephemeral=True)
    media, err = _resolve_media_by_anime_arg(ctx, user_id, anime_arg)
    if media is None:
        if err is not None:
            _reply_error(ctx, err, deferred=True)
        else:
            _reply_anilist_failure(ctx, deferred=True)
        return

    media_id = int(media.get("id") or 0)
    title = _format_title(media)
    if not media_id:
        _reply_error(ctx, S.SIMILAR_INVALID_CACHED, deferred=True)
        return

    already_favorite = _is_favorite(ctx, user_id, media_id)
    if remove:
        if not already_favorite:
            ctx.interaction.followup(content=S.FAVORITE_NOT_PRESENT.format(title=title), ephemeral=True)
            return
        _upsert_user_anime(ctx, user_id, media_id, is_favorite=False)
        ctx.interaction.followup(content=S.FAVORITE_REMOVED.format(title=title), ephemeral=True)
        # Keep the user's "last anime" cache fresh so /similar still works.
        ctx.kv.set(f"last_anime:user:{user_id}", media_id, ttl_seconds=LAST_ANIME_TTL)
        return

    if already_favorite:
        ctx.interaction.followup(content=S.FAVORITE_ALREADY.format(title=title), ephemeral=True)
        return
    _upsert_user_anime(ctx, user_id, media_id, is_favorite=True)
    ctx.kv.set(f"last_anime:user:{user_id}", media_id, ttl_seconds=LAST_ANIME_TTL)
    ctx.interaction.followup(content=S.FAVORITE_ADDED.format(title=title), ephemeral=True)


@plugin.on_slash_command("watch")
def cmd_watch(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    status = (opts.get("status") or "").strip().lower()
    if status not in VALID_STATUSES:
        ctx.interaction.respond(
            content=S.WATCH_INVALID_STATUS.format(choices=", ".join(VALID_STATUSES)),
            ephemeral=True,
        )
        return

    ctx.interaction.defer(ephemeral=True)
    media_id = _resolve_last_anime_id(ctx, user_id)
    if media_id is None:
        _reply_error(ctx, S.WATCH_NO_CACHE, deferred=True)
        return

    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": media_id})
    if data is None or not data.get("Media"):
        _reply_anilist_failure(
            ctx,
            deferred=True,
            fallback=S.SIMILAR_FETCH_FAIL_CACHED,
        )
        return
    title = _format_title(data["Media"])
    _upsert_user_anime(ctx, user_id, media_id, status=status)
    ctx.interaction.followup(
        content=S.WATCH_SET.format(title=title, status=STATUS_LABEL[status]),
        ephemeral=True,
    )


def _select_user_anime(
    ctx: Context,
    target_user_id: str,
    scope: str,
    *,
    page: int,
) -> tuple[list[dict], bool]:
    """Read PER_PAGE+1 rows for the given user/scope/page. Returns (rows, has_next).

    v8.0+ filters on media_type='anime'. /manga-favorites uses its own
    cmd_manga_favorites handler that scopes the same way to 'manga'.
    """
    offset = max(0, (page - 1) * PER_PAGE)
    limit = PER_PAGE + 1
    if scope == "favorites":
        sql = (
            "SELECT media_id, status, is_favorite FROM otaku_user_media "
            "WHERE user_id = %s AND media_type = 'anime' AND is_favorite = TRUE "
            "ORDER BY added_at DESC LIMIT %s OFFSET %s"
        )
        params = [target_user_id, limit, offset]
    elif scope == "all":
        sql = (
            "SELECT media_id, status, is_favorite FROM otaku_user_media "
            "WHERE user_id = %s AND media_type = 'anime' "
            "ORDER BY added_at DESC LIMIT %s OFFSET %s"
        )
        params = [target_user_id, limit, offset]
    else:
        sql = (
            "SELECT media_id, status, is_favorite FROM otaku_user_media "
            "WHERE user_id = %s AND media_type = 'anime' AND status = %s "
            "ORDER BY added_at DESC LIMIT %s OFFSET %s"
        )
        params = [target_user_id, scope, limit, offset]

    rows = ctx.sql.query(sql, params) or []
    has_next = len(rows) > PER_PAGE
    return rows[:PER_PAGE], has_next


def _list_scope_label(scope: str) -> str:
    if scope == "all":
        return S.LIST_SCOPE_ALL
    if scope == "favorites":
        return S.LIST_SCOPE_FAVORITES
    return STATUS_LABEL.get(scope, scope)


def _decorate_list_lines(media_list: list[dict], rows_by_id: dict[int, dict]) -> list[str]:
    """Build the body of /list / /favorites with status emoji + favorite ⭐."""
    lines = []
    for i, m in enumerate(media_list, start=1):
        title = _format_title(m)
        score = _score(m)
        url = m.get("siteUrl") or ""
        row = rows_by_id.get(int(m.get("id") or 0)) or {}
        status = row.get("status") or "watching"
        emoji = STATUS_EMOJI.get(status, "•")
        fav = " ⭐" if row.get("is_favorite") else ""
        lines.append(f"{emoji}{fav} **{i}. [{title}]({url})** · ⭐ {score}")
    return lines


def _render_user_list(
    ctx: Context,
    *,
    target_user_id: str,
    target_display: str,
    is_self: bool,
    scope: str,
    page: int,
    deferred: bool,
    ephemeral_reply: bool = True,
) -> None:
    """Shared renderer for /list, /favorites, and their pagination clicks."""
    rows, has_next = _select_user_anime(ctx, target_user_id, scope, page=page)
    scope_label = _list_scope_label(scope)

    if not rows:
        empty = (
            S.LIST_EMPTY_OWN if is_self
            else S.LIST_EMPTY_OTHER.format(who=target_display)
        )
        _reply_error(ctx, empty, deferred=deferred)
        return

    rows_by_id = {int(r["media_id"]): r for r in rows}
    ids = list(rows_by_id.keys())
    data = _anilist_query(ctx, QUERY_MEDIA_BATCH, {"ids": ids}, cache=True)
    if data is None:
        _reply_anilist_failure(ctx, deferred=deferred)
        return
    media_list = ((data.get("Page") or {}).get("media")) or []
    # Preserve SQL order (newest first), not AniList's response order.
    def _sort_key(m: dict) -> int:
        mid = int(m.get("id") or -1)
        return ids.index(mid) if mid in ids else 9999

    ordered = sorted(media_list, key=_sort_key)

    header_template = S.LIST_HEADER_OWN if is_self else S.LIST_HEADER_OTHER
    header = header_template.format(scope=scope_label, who=target_display)
    embed = _make_list_embed(ordered, header, page=page, has_next=has_next)
    # Override the description with status-emoji-decorated lines.
    embed["description"] = "\n\n".join(_decorate_list_lines(ordered, rows_by_id))

    prev_id = f"otaku:list:{target_user_id}:{scope}:{page - 1}" if page > 1 else None
    next_id = f"otaku:list:{target_user_id}:{scope}:{page + 1}" if has_next else None
    components = [_page_buttons(prev_id, next_id)]
    select_row = _make_select_row(ordered)
    if select_row is not None:
        components.append(select_row)

    if deferred:
        ctx.interaction.followup(embeds=[embed], components=components, ephemeral=ephemeral_reply)
    else:
        ctx.interaction.respond(embeds=[embed], components=components, ephemeral=ephemeral_reply)


def _extract_target_user(event: dict, opts: dict) -> tuple[str, str, bool]:
    """Return (target_user_id, display_name, is_self) based on the optional `user` option."""
    caller_id = event.get("user_id") or ""
    target_raw = opts.get("user")
    if target_raw and str(target_raw).strip() and str(target_raw) != caller_id:
        target_id = str(target_raw).strip()
        return target_id, f"<@{target_id}>", False
    return caller_id, "you", True


@plugin.on_slash_command("favorites")
def cmd_favorites(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    target_id, display, is_self = _extract_target_user(event, opts)
    ctx.interaction.defer(ephemeral=True)
    _render_user_list(
        ctx,
        target_user_id=target_id,
        target_display=display,
        is_self=is_self,
        scope="favorites",
        page=1,
        deferred=True,
    )


@plugin.on_slash_command("list")
def cmd_list(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    status = (opts.get("status") or "all").strip().lower()
    if status != "all" and status not in VALID_STATUSES:
        status = "all"
    target_id, display, is_self = _extract_target_user(event, opts)
    ctx.interaction.defer(ephemeral=True)
    _render_user_list(
        ctx,
        target_user_id=target_id,
        target_display=display,
        is_self=is_self,
        scope=status,
        page=1,
        deferred=True,
    )


# ── v2.5.0 — /otaku-reset (self-service data deletion) ──────────────────────

@plugin.on_slash_command("otaku-reset")
def cmd_otaku_reset(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    # Show the confirm prompt right away — no defer needed.
    components = [ActionRow(
        Button(S.RESET_CONFIRM_BUTTON, custom_id=f"otaku:reset-confirm:{user_id}", style="danger", emoji="🗑"),
        Button(S.RESET_CANCEL_BUTTON, custom_id=f"otaku:reset-cancel:{user_id}", style="secondary"),
    )]
    ctx.interaction.respond(content=S.RESET_CONFIRM_PROMPT, components=components, ephemeral=True)


def _handle_reset_confirm(ctx: Context, event: dict) -> None:
    cid = event.get("custom_id") or ""
    caller_id = event.get("user_id") or ""
    # custom_id encodes the original caller — make sure only they can confirm.
    target_user = cid.split(":", 2)[2] if cid.count(":") >= 2 else ""
    if target_user != caller_id:
        ctx.interaction.respond(content=S.RESET_CANCELLED, ephemeral=True)
        return
    rows_affected = ctx.sql.execute(
        "DELETE FROM otaku_user_media WHERE user_id = %s",
        [caller_id],
    )
    if rows_affected and isinstance(rows_affected, int) and rows_affected > 0:
        ctx.interaction.respond(content=S.RESET_DONE.format(rows=rows_affected), ephemeral=True)
    else:
        # MockSql returns 0; treat any non-positive as "nothing to delete." Prod
        # returns the actual row count, but tests should still see a clean reply.
        ctx.interaction.respond(content=S.RESET_NOTHING, ephemeral=True)


def _handle_reset_cancel(ctx: Context, event: dict) -> None:
    ctx.interaction.respond(content=S.RESET_CANCELLED, ephemeral=True)


# ── v2.6.0 — admin gating + /otaku-admin reset-user ─────────────────────────
# Discord permission bitfield constants. Either bit qualifies a user as an admin
# for otaku's purposes — both are server-managing roles in Discord's model.
PERMISSION_ADMINISTRATOR = 0x8       # 1 << 3
PERMISSION_MANAGE_GUILD  = 0x20      # 1 << 5
ADMIN_PERMISSION_MASK = PERMISSION_ADMINISTRATOR | PERMISSION_MANAGE_GUILD

# In-process cache for `list_roles()` — server admin changes are rare.
_ROLE_LIST_CACHE: dict[str, tuple[float, list[dict]]] = {}
ROLE_LIST_CACHE_TTL = 5 * 60  # 5 minutes


def _cached_list_roles(ctx: Context) -> list[dict]:
    """Return ctx.discord.list_roles() with a 5-minute per-server cache."""
    key = str(getattr(ctx, "server_id", "") or "")
    entry = _ROLE_LIST_CACHE.get(key)
    if entry is not None and entry[0] > time.monotonic():
        return entry[1]
    roles = ctx.discord.list_roles() or []
    _ROLE_LIST_CACHE[key] = (time.monotonic() + ROLE_LIST_CACHE_TTL, roles)
    return roles


def _clear_role_cache() -> None:
    """Test hook — empties the role cache so per-test scenarios don't bleed."""
    _ROLE_LIST_CACHE.clear()


def _role_has_admin_bits(role: dict) -> bool:
    """True if the role's permissions bitfield has ADMINISTRATOR or MANAGE_GUILD."""
    raw = role.get("permissions")
    if raw is None:
        return False
    try:
        bits = int(raw)
    except (TypeError, ValueError):
        return False
    return bool(bits & ADMIN_PERMISSION_MASK)


def _caller_is_admin(ctx: Context, user_id: str) -> bool:
    """Check whether the caller has server-admin powers.

    The guild owner is always admin. Otherwise we look up the caller's roles
    and check each one's permission bitfield for ADMINISTRATOR or MANAGE_GUILD.
    A failed lookup returns False and callers fall through to S.ADMIN_DENIED.
    """
    if not user_id:
        return False
    try:
        guild = ctx.discord.get_guild() or {}
    except Exception:  # noqa: BLE001 — admin check must never crash
        return False
    if guild.get("owner_id") == user_id:
        return True
    try:
        member = ctx.discord.get_member(user_id=user_id) or {}
    except Exception:  # noqa: BLE001
        return False
    caller_role_ids = set(member.get("roles") or [])
    if not caller_role_ids:
        return False
    try:
        all_roles = _cached_list_roles(ctx)
    except Exception:  # noqa: BLE001
        return False
    for role in all_roles:
        if str(role.get("id")) in {str(r) for r in caller_role_ids} and _role_has_admin_bits(role):
            return True
    return False


def _otaku_admin_reset_user(ctx: Context, user_id: str, sub_opts: dict) -> None:
    target_user = str(sub_opts.get("user") or "").strip()
    if not target_user:
        ctx.interaction.respond(content=S.ADMIN_USER_REQUIRED, ephemeral=True)
        return

    ctx.interaction.defer(ephemeral=True)

    if not _caller_is_admin(ctx, user_id):
        ctx.interaction.followup(content=S.ADMIN_DENIED, ephemeral=True)
        return

    rows_affected = ctx.sql.execute(
        "DELETE FROM otaku_user_media WHERE user_id = %s",
        [target_user],
    )
    if rows_affected and isinstance(rows_affected, int) and rows_affected > 0:
        ctx.interaction.followup(
            content=S.ADMIN_RESET_DONE.format(rows=rows_affected, user=target_user),
            ephemeral=True,
        )
    else:
        ctx.interaction.followup(
            content=S.ADMIN_RESET_NOTHING.format(user=target_user),
            ephemeral=True,
        )


def _otaku_admin_set_channel(ctx: Context, user_id: str, sub_opts: dict) -> None:
    ctx.interaction.defer(ephemeral=True)
    if not _caller_is_admin(ctx, user_id):
        ctx.interaction.followup(content=S.ADMIN_DENIED, ephemeral=True)
        return

    channel_raw = sub_opts.get("channel")
    if channel_raw is None or str(channel_raw).strip() == "":
        # Empty argument → clear the channel.
        try:
            ctx.kv.delete(NOTIFY_CHANNEL_KV)
        except Exception:  # noqa: BLE001
            pass
        ctx.interaction.followup(content=S.ADMIN_CHANNEL_CLEARED, ephemeral=True)
        return

    channel_id = str(channel_raw).strip()
    ctx.kv.set(NOTIFY_CHANNEL_KV, channel_id)
    ctx.interaction.followup(
        content=S.ADMIN_CHANNEL_SET.format(channel_id=channel_id),
        ephemeral=True,
    )


@plugin.on_slash_command("otaku-admin")
def cmd_otaku_admin(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return

    # Slash sub-commands arrive as a nested options list.
    raw_options = event.get("options") or []
    if not raw_options:
        ctx.interaction.respond(content=S.ADMIN_USER_REQUIRED, ephemeral=True)
        return
    first = raw_options[0] if isinstance(raw_options, list) else {}
    subcommand = (first.get("name") or "").strip()
    sub_options = first.get("options") or []
    sub_opts = {o["name"]: o["value"] for o in sub_options if isinstance(o, dict) and "name" in o}

    if subcommand == "reset-user":
        _otaku_admin_reset_user(ctx, user_id, sub_opts)
        return
    if subcommand == "set-channel":
        _otaku_admin_set_channel(ctx, user_id, sub_opts)
        return

    ctx.interaction.respond(content=S.ADMIN_USER_REQUIRED, ephemeral=True)


# ── v4.0.0 — airing notifications ───────────────────────────────────────────

def _fetch_next_airing(ctx: Context, media_id: int) -> dict | None:
    """Best-effort lookup of the next airing for a single media id."""
    now = int(datetime.now(timezone.utc).timestamp())
    data = _anilist_query(
        ctx, QUERY_AIRING_WINDOW,
        {"at_gte": now, "at_lte": now + 60 * 60 * 24 * 14},  # 14 days ahead
        cache=False,
    )
    if not data:
        return None
    schedules = ((data.get("Page") or {}).get("airingSchedules")) or []
    for s in schedules:
        if int((s.get("media") or {}).get("id") or 0) == int(media_id):
            return s
    return None


def _airing_eta_str(airing_at: int | None) -> str:
    """Human-readable countdown like '3h 21m' or 'now'."""
    if airing_at is None:
        return S.NOTIFY_NO_NEXT
    delta = int(airing_at) - int(datetime.now(timezone.utc).timestamp())
    if delta <= 0:
        return "now"
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


@plugin.on_slash_command("notify")
def cmd_notify(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    channel_id = str(event.get("channel_id") or "")
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    raw = str(opts.get("anime") or "").strip()
    if not raw:
        ctx.interaction.respond(content=S.NOTIFY_USAGE, ephemeral=True)
        return

    ctx.interaction.defer(ephemeral=True)
    media, err = _resolve_anime_arg_for_swl(ctx, raw)
    if media is None:
        if err is None:
            _reply_anilist_failure(ctx, deferred=True)
        else:
            _reply_error(ctx, err, deferred=True)
        return

    media_id = int(media.get("id") or 0)
    title = _format_title(media)

    existing = ctx.sql.query_one(
        "SELECT 1 FROM otaku_notifications WHERE user_id = %s AND media_id = %s",
        [user_id, media_id],
    )
    if existing:
        ctx.interaction.followup(content=S.NOTIFY_ALREADY.format(title=title), ephemeral=True)
        return

    ctx.sql.execute(
        "INSERT INTO otaku_notifications (user_id, media_id, channel_id) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, media_id) DO UPDATE SET channel_id = EXCLUDED.channel_id",
        [user_id, media_id, channel_id or None],
    )
    ctx.interaction.followup(
        content=S.NOTIFY_SUBSCRIBED.format(title=title),
        ephemeral=True,
    )


@plugin.on_slash_command("unnotify")
def cmd_unnotify(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    raw = str(opts.get("anime") or "").strip()
    if not raw:
        ctx.interaction.respond(content=S.NOTIFY_USAGE, ephemeral=True)
        return

    ctx.interaction.defer(ephemeral=True)
    media, err = _resolve_anime_arg_for_swl(ctx, raw)
    if media is None:
        if err is None:
            _reply_anilist_failure(ctx, deferred=True)
        else:
            _reply_error(ctx, err, deferred=True)
        return

    media_id = int(media.get("id") or 0)
    title = _format_title(media)

    existing = ctx.sql.query_one(
        "SELECT 1 FROM otaku_notifications WHERE user_id = %s AND media_id = %s",
        [user_id, media_id],
    )
    if not existing:
        ctx.interaction.followup(content=S.NOTIFY_NOT_SUBSCRIBED.format(title=title), ephemeral=True)
        return

    ctx.sql.execute(
        "DELETE FROM otaku_notifications WHERE user_id = %s AND media_id = %s",
        [user_id, media_id],
    )
    ctx.interaction.followup(content=S.NOTIFY_REMOVED.format(title=title), ephemeral=True)


@plugin.on_slash_command("notify-list")
def cmd_notify_list(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    ctx.interaction.defer(ephemeral=True)

    rows = ctx.sql.query(
        "SELECT media_id FROM otaku_notifications WHERE user_id = %s "
        "ORDER BY added_at DESC LIMIT 25",
        [user_id],
    ) or []
    if not rows:
        _reply_error(ctx, S.NOTIFY_LIST_EMPTY, deferred=True)
        return

    ids = [int(r["media_id"]) for r in rows if r.get("media_id") is not None]
    data = _anilist_query(ctx, QUERY_MEDIA_BATCH, {"ids": ids}, cache=True)
    media_by_id: dict[int, dict] = {}
    if data:
        for m in ((data.get("Page") or {}).get("media") or []):
            mid = m.get("id")
            if isinstance(mid, int):
                media_by_id[mid] = m

    lines = []
    for mid in ids:
        m = media_by_id.get(mid)
        title = _format_title(m) if m else f"#{mid}"
        url = (m or {}).get("siteUrl") or ""
        next_airing = _fetch_next_airing(ctx, mid)
        next_eta = _airing_eta_str((next_airing or {}).get("airingAt"))
        if url:
            lines.append(S.NOTIFY_LIST_LINE.format(title=title, url=url, next_eta=next_eta))
        else:
            lines.append(f"• {title} · next: {next_eta}")

    embed = {
        "title": S.NOTIFY_LIST_HEADER,
        "description": "\n".join(lines),
        "color": ANILIST_COLOR,
        "footer": {"text": f"{len(ids)} subscription(s) · Data from AniList"},
    }
    ctx.interaction.followup(embeds=[embed], ephemeral=True)


def _resolve_announcement_channel(ctx: Context) -> str | None:
    """Return the per-server announcement channel id, or None if not configured."""
    try:
        val = ctx.kv.get(NOTIFY_CHANNEL_KV)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _build_airing_embed(media: dict, episode: int) -> dict:
    title = _format_title(media)
    total = media.get("episodes") if isinstance(media, dict) else None
    of_total = f" / {total}" if isinstance(total, int) and total > 0 else ""
    embed = {
        "title": S.NOTIFY_ANNOUNCEMENT_TITLE.format(title=title),
        "description": f"Episode **{episode}**{of_total} is airing now.",
        "color": ANILIST_COLOR,
        "footer": {"text": S.FOOTER_ANILIST},
    }
    cover = (media.get("coverImage") or {}).get("large") if isinstance(media, dict) else None
    if cover:
        embed["thumbnail"] = {"url": cover}
    url = media.get("siteUrl") if isinstance(media, dict) else None
    if url:
        embed["url"] = url
    return embed


def _airing_dedup_key(media_id: int, episode: int) -> str:
    return f"otaku:airing:{media_id}:{episode}"


def _dispatch_airing_announcements(ctx: Context) -> int:
    """Look at the next NOTIFY_LOOKAHEAD_SECONDS for airings and post pings.

    Returns the number of announcements actually sent. Deduped via ephemeral
    so a re-fired cron doesn't double-ping. Safe to call from a slash handler
    as a fallback in pool mode.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    data = _anilist_query(
        ctx, QUERY_AIRING_WINDOW,
        {"at_gte": now - 5 * 60, "at_lte": now + NOTIFY_LOOKAHEAD_SECONDS},
    )
    if not data:
        return 0
    schedules = ((data.get("Page") or {}).get("airingSchedules")) or []
    if not schedules:
        return 0

    fallback_channel = _resolve_announcement_channel(ctx)
    sent = 0

    for s in schedules:
        media = s.get("media") or {}
        media_id = int(media.get("id") or 0)
        episode = int(s.get("episode") or 0)
        if not media_id or not episode:
            continue

        # v10.0.11 — query subscribers BEFORE claiming the dedup key. The old
        # order burned the per-episode claim and THEN ran the SQL; any SQL
        # failure (e.g. otaku_notifications not yet bootstrapped, transient
        # host error) permanently skipped that episode's pings for the TTL.
        subs = ctx.sql.query(
            "SELECT user_id, channel_id FROM otaku_notifications WHERE media_id = %s",
            [media_id],
        ) or []
        if not subs:
            continue

        # Dedup: one airing only pings once. Claimed only once we know there
        # is something to send.
        dedup_key = _airing_dedup_key(media_id, episode)
        try:
            if not ctx.ephemeral.dedup(dedup_key, ttl_seconds=NOTIFY_DEDUP_TTL):
                continue
        except Exception:  # noqa: BLE001 — ephemeral down → still try once
            pass

        # Group subscribers by which channel we'd post to.
        by_channel: dict[str, list[str]] = {}
        for r in subs:
            target = fallback_channel or (r.get("channel_id") or "")
            if not target:
                continue
            by_channel.setdefault(target, []).append(str(r.get("user_id") or ""))

        embed = _build_airing_embed(media, episode)
        for channel_id, user_ids in by_channel.items():
            mentions = " ".join(f"<@{uid}>" for uid in user_ids if uid)
            content = S.NOTIFY_ANNOUNCEMENT_BODY.format(
                episode=episode,
                of_total=(f" / {media.get('episodes')}" if media.get("episodes") else ""),
                mentions=mentions,
            )
            try:
                ctx.discord.send_message(
                    channel_id=channel_id,
                    content=content,
                    embeds=[embed],
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001
                ctx.log(
                    f"airing announce failed: {exc}",
                    level="warning",
                    tags=["notify"],
                    channel_id=channel_id,
                    media_id=str(media_id),
                )
    return sent


# ── v4.2.0 — seasonal premieres ─────────────────────────────────────────────

# Season transitions roughly align with the calendar quarters.
_SEASON_ORDER = ("WINTER", "SPRING", "SUMMER", "FALL")
_SEASON_START_MONTHS = {"WINTER": 1, "SPRING": 4, "SUMMER": 7, "FALL": 10}
# Per-server KV remembers the most-recent season we auto-digested, so the cron
# only posts the seasonal digest once per season per server.
PREMIERES_DIGEST_KV = "premieres_digest_last:guild"
PREMIERES_DIGEST_WINDOW_DAYS = 7  # post on or before day 7 of a new season


def _next_season(now: datetime | None = None) -> tuple[str, int]:
    """Return the (season, year) immediately AFTER the current one (UTC)."""
    now = now or datetime.now(timezone.utc)
    cur, year = _current_season_at(now)
    idx = _SEASON_ORDER.index(cur)
    if idx == 3:  # FALL → next is WINTER of year+1
        return _SEASON_ORDER[0], year + 1
    return _SEASON_ORDER[idx + 1], year


def _current_season_at(now: datetime) -> tuple[str, int]:
    """Variant of _current_season that takes an explicit `now` for tests."""
    m = now.month
    if m <= 3:
        return "WINTER", now.year
    if m <= 6:
        return "SPRING", now.year
    if m <= 9:
        return "SUMMER", now.year
    return "FALL", now.year


def _season_is_fresh(now: datetime | None = None) -> bool:
    """True if `now` lands inside the first PREMIERES_DIGEST_WINDOW_DAYS of a season."""
    now = now or datetime.now(timezone.utc)
    season, _year = _current_season_at(now)
    start_month = _SEASON_START_MONTHS[season]
    # Day-of-season = (today − first day of start_month).days + 1, but only for
    # the season's own months. Outside those months we can't be "fresh."
    if now.month < start_month or now.month > start_month + 2:
        return False
    season_start = now.replace(month=start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
    delta_days = (now - season_start).days
    return 0 <= delta_days < PREMIERES_DIGEST_WINDOW_DAYS


def _render_premieres(
    ctx: Context, season: str, year: int, page: int, *, deferred: bool
) -> None:
    # v10.0.1: cache every page (see _render_discover comment).
    data = _anilist_query(
        ctx,
        QUERY_SEASON,
        {"season": season, "year": year, "sort": ["POPULARITY_DESC"],
         "page": page, "perPage": PER_PAGE},
        cache=True,
    )
    if data is None:
        _reply_anilist_failure(ctx, deferred=deferred)
        return
    page_obj = (data.get("Page") or {})
    media_list = page_obj.get("media") or []
    has_next = bool((page_obj.get("pageInfo") or {}).get("hasNextPage"))

    header = S.PREMIERES_HEADER.format(season=season.title(), year=year)
    if not media_list:
        _reply_error(ctx, S.PREMIERES_EMPTY.format(season=season.title(), year=year), deferred=deferred)
        return

    embed = _make_list_embed(media_list, header, page=page, has_next=has_next)
    prev_id = f"otaku:premieres:{season}:{year}:{page - 1}" if page > 1 else None
    next_id = f"otaku:premieres:{season}:{year}:{page + 1}" if has_next else None
    components = [_page_buttons(prev_id, next_id)]
    select_row = _make_select_row(media_list)
    if select_row is not None:
        components.append(select_row)

    if deferred:
        ctx.interaction.followup(embeds=[embed], components=components)
    else:
        ctx.interaction.respond(embeds=[embed], components=components)


@plugin.on_slash_command("season-premieres")
def cmd_season_premieres(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    raw_season = (opts.get("season") or "").strip().upper()
    raw_year = opts.get("year")

    if raw_season and raw_year is not None:
        season = raw_season if raw_season in _SEASON_ORDER else None
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            year = None
        if season is None or year is None:
            ctx.interaction.respond(content=S.PREMIERES_USAGE, ephemeral=True)
            return
    else:
        season, year = _next_season()

    ctx.interaction.defer()
    _render_premieres(ctx, season, year, page=1, deferred=True)


def _dispatch_premieres_digest(ctx: Context) -> bool:
    """Post the seasonal digest to the announcement channel, once per season.

    Returns True if a digest was actually posted. Skipped silently if:
    - we're not in the season's freshness window
    - no announcement channel is configured
    - we've already posted this season's digest (KV-dedup'd per server)
    """
    if not _season_is_fresh():
        return False
    channel_id = _resolve_announcement_channel(ctx)
    if not channel_id:
        return False

    season, year = _current_season_at(datetime.now(timezone.utc))
    digest_key = f"{season}_{year}"
    try:
        prev = ctx.kv.get(PREMIERES_DIGEST_KV)
    except Exception:  # noqa: BLE001
        prev = None
    if prev == digest_key:
        return False

    data = _anilist_query(
        ctx, QUERY_SEASON,
        {"season": season, "year": year, "sort": ["POPULARITY_DESC"],
         "page": 1, "perPage": PER_PAGE},
        cache=True,
    )
    media_list = ((data or {}).get("Page") or {}).get("media") or []
    if not media_list:
        return False

    header = S.PREMIERES_DIGEST_TITLE.format(season=season.title(), year=year)
    embed = _make_list_embed(media_list, header, page=1, has_next=False)
    embed["footer"] = {"text": S.PREMIERES_DIGEST_FOOTER}
    try:
        ctx.discord.send_message(channel_id=channel_id, embeds=[embed])
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"premieres digest send failed: {exc}", level="warning", tags=["notify", "digest"])
        return False

    try:
        ctx.kv.set(PREMIERES_DIGEST_KV, digest_key)
    except Exception:  # noqa: BLE001 — failing to mark digest done just means we'll try again next cron
        pass
    return True


@plugin.cron("5 * * * *")  # every hour at :05 UTC (single-tenant only)
def cron_airing_check(ctx: Context) -> None:
    """In pool mode this never fires. See CHANGELOG v4.0.0 — fallback runs from
    /notify-list so users still see fresh data."""
    try:
        n = _dispatch_airing_announcements(ctx)
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"cron_airing_check failed: {exc}", level="error", tags=["notify", "cron"])
        return
    if n:
        ctx.log(f"airing cron sent {n} announcement(s)", level="info", tags=["notify", "cron"])

    # Seasonal-premieres digest piggy-backs on the same cron. Idempotent —
    # we only post once per season per server thanks to KV dedup.
    try:
        if _dispatch_premieres_digest(ctx):
            ctx.log("posted seasonal premieres digest", level="info", tags=["notify", "digest"])
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"premieres digest failed: {exc}", level="error", tags=["notify", "digest"])


# ── v5.0.0 — plugin dashboard (manifest mode) ───────────────────────────────
# Every handler must finish in <10s per the SDK contract. All of the SQL here
# is single-query and indexed; the table widget makes one AniList batch call
# which is cached.

DASHBOARD_TOP_TRACKED_LIMIT = 5


@plugin.on_dashboard("get_total_tracked")
def dash_total_tracked(ctx: Context, params: dict) -> dict:
    # v5.0 contract was "total tracked anime rows"; v8.0 filters explicitly
    # so manga rows from /manga-favorites don't inflate the anime widget.
    val = ctx.sql.scalar("SELECT COUNT(*) FROM otaku_user_media WHERE media_type = 'anime'")
    return {"value": int(val or 0), "change": ""}


@plugin.on_dashboard("get_active_users_30d")
def dash_active_users_30d(ctx: Context, params: dict) -> dict:
    val = ctx.sql.scalar(
        "SELECT COUNT(DISTINCT user_id) FROM otaku_user_media "
        "WHERE media_type = 'anime' AND added_at > NOW() - INTERVAL '30 days'"
    )
    return {"value": int(val or 0), "change": ""}


@plugin.on_dashboard("get_total_episodes")
def dash_total_episodes(ctx: Context, params: dict) -> dict:
    val = ctx.sql.scalar(
        "SELECT COALESCE(SUM(episodes_watched), 0) FROM otaku_user_media "
        "WHERE media_type = 'anime'"
    )
    return {"value": int(val or 0), "change": ""}


@plugin.on_dashboard("get_total_subscriptions")
def dash_total_subscriptions(ctx: Context, params: dict) -> dict:
    val = ctx.sql.scalar("SELECT COUNT(*) FROM otaku_notifications")
    return {"value": int(val or 0), "change": ""}


@plugin.on_dashboard("get_status_distribution")
def dash_status_distribution(ctx: Context, params: dict) -> dict:
    rows = ctx.sql.query(
        "SELECT status, COUNT(*) AS n FROM otaku_user_media "
        "WHERE media_type = 'anime' GROUP BY status"
    ) or []
    counts = {s: 0 for s in VALID_STATUSES}
    for r in rows:
        s = r.get("status") or ""
        if s in counts:
            counts[s] = int(r.get("n") or 0)
    # Preserve a stable order so the chart bars don't shuffle between loads.
    return {
        "labels": [STATUS_LABEL[s] for s in VALID_STATUSES],
        "series": [{
            "name": "Rows",
            "data": [counts[s] for s in VALID_STATUSES],
        }],
    }


@plugin.on_dashboard("get_top_tracked")
def dash_top_tracked(ctx: Context, params: dict) -> dict:
    rows = ctx.sql.query(
        "SELECT media_id, "
        "       COUNT(*) AS trackers, "
        "       COALESCE(SUM(CASE WHEN is_favorite THEN 1 ELSE 0 END), 0) AS favorites "
        "FROM otaku_user_media "
        "WHERE media_type = 'anime' "
        "GROUP BY media_id "
        "ORDER BY trackers DESC, favorites DESC "
        "LIMIT %s",
        [DASHBOARD_TOP_TRACKED_LIMIT],
    ) or []
    if not rows:
        return {"rows": [], "total": 0}

    ids = [int(r["media_id"]) for r in rows if r.get("media_id") is not None]
    data = _anilist_query(ctx, QUERY_MEDIA_BATCH, {"ids": ids}, cache=True)
    media_by_id: dict[int, dict] = {}
    if data:
        for m in ((data.get("Page") or {}).get("media") or []):
            mid = m.get("id")
            if isinstance(mid, int):
                media_by_id[mid] = m

    table_rows = []
    for r in rows:
        mid = int(r["media_id"])
        m = media_by_id.get(mid)
        title = _format_title(m) if m else f"#{mid}"
        table_rows.append({
            "title":     title,
            "trackers":  int(r.get("trackers") or 0),
            "favorites": int(r.get("favorites") or 0),
        })
    return {"rows": table_rows, "total": len(table_rows)}


@plugin.on_dashboard("get_settings")
def dash_get_settings(ctx: Context, params: dict) -> dict:
    return {
        "values": {
            "announce_channel_id": _resolve_announcement_channel(ctx) or "",
        },
    }


@plugin.on_dashboard("save_settings")
def dash_save_settings(ctx: Context, params: dict) -> dict:
    vals = (params or {}).get("values") or {}
    channel_id = str(vals.get("announce_channel_id") or "").strip()
    if channel_id:
        ctx.kv.set(NOTIFY_CHANNEL_KV, channel_id)
    else:
        try:
            ctx.kv.delete(NOTIFY_CHANNEL_KV)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


# ── v3.3.0 — /leaderboard ───────────────────────────────────────────────────

LEADERBOARD_TOP_N = 10
LEADERBOARD_SCORE_MIN_RATED = 3  # require at least N rated rows to qualify for the score board


def _leaderboard_completed(ctx: Context) -> list[dict]:
    """Top users by COUNT(*) where status='completed'. Anime-only per the v3.3 contract."""
    return ctx.sql.query(
        "SELECT user_id, COUNT(*) AS n FROM otaku_user_media "
        "WHERE media_type = 'anime' AND status = 'completed' "
        "GROUP BY user_id ORDER BY n DESC LIMIT %s",
        [LEADERBOARD_TOP_N],
    ) or []


def _leaderboard_score(ctx: Context) -> list[dict]:
    """Top users by mean rating, gated to those with ≥ N rated rows. Anime-only."""
    return ctx.sql.query(
        "SELECT user_id, AVG(rating) AS avg_rating, COUNT(*) AS rated "
        "FROM otaku_user_media WHERE media_type = 'anime' AND rating IS NOT NULL "
        "GROUP BY user_id HAVING COUNT(*) >= %s "
        "ORDER BY avg_rating DESC, rated DESC LIMIT %s",
        [LEADERBOARD_SCORE_MIN_RATED, LEADERBOARD_TOP_N],
    ) or []


def _leaderboard_hours(ctx: Context) -> list[dict]:
    """Top users by total episodes (presented as hours via the 24min/ep heuristic). Anime-only."""
    return ctx.sql.query(
        "SELECT user_id, COALESCE(SUM(episodes_watched), 0) AS episodes "
        "FROM otaku_user_media "
        "WHERE media_type = 'anime' "
        "GROUP BY user_id HAVING COALESCE(SUM(episodes_watched), 0) > 0 "
        "ORDER BY episodes DESC LIMIT %s",
        [LEADERBOARD_TOP_N],
    ) or []


def _format_leaderboard_lines(rows: list[dict], metric: str) -> str:
    if not rows:
        return S.LIST_NO_RESULTS
    medals = ("🥇", "🥈", "🥉")
    lines = []
    for i, r in enumerate(rows, start=1):
        prefix = medals[i - 1] if i <= 3 else f"**{i}.**"
        uid = r.get("user_id") or "?"
        if metric == "completed":
            value = f"{int(r.get('n') or 0)} completed"
        elif metric == "score":
            avg = float(r.get("avg_rating") or 0)
            rated = int(r.get("rated") or 0)
            value = f"{avg / 2:.1f}/10 ({rated} rated)"
        else:  # hours
            eps = int(r.get("episodes") or 0)
            hours = (eps * STATS_MINUTES_PER_EPISODE) / 60
            value = f"{hours:.1f} hours ({eps} eps)"
        lines.append(f"{prefix} <@{uid}> — {value}")
    return "\n".join(lines)


@plugin.on_slash_command("leaderboard")
def cmd_leaderboard(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    metric = (opts.get("metric") or "completed").strip().lower()
    if metric not in ("completed", "score", "hours"):
        metric = "completed"

    ctx.interaction.defer()

    if metric == "completed":
        rows = _leaderboard_completed(ctx)
        header = S.LEADERBOARD_HEADER_COMPLETED
        footer = S.LEADERBOARD_FOOTER_COMPLETED.format(n=len(rows))
    elif metric == "score":
        rows = _leaderboard_score(ctx)
        header = S.LEADERBOARD_HEADER_SCORE
        footer = S.LEADERBOARD_FOOTER_SCORE.format(
            n=len(rows), min_rated=LEADERBOARD_SCORE_MIN_RATED
        )
    else:  # hours
        rows = _leaderboard_hours(ctx)
        header = S.LEADERBOARD_HEADER_HOURS
        footer = S.LEADERBOARD_FOOTER_HOURS.format(n=len(rows))

    if not rows:
        _reply_error(ctx, S.LEADERBOARD_EMPTY, deferred=True)
        return

    embed = {
        "title": header,
        "description": _format_leaderboard_lines(rows, metric),
        "color": ANILIST_COLOR,
        "footer": {"text": footer},
    }
    ctx.interaction.followup(embeds=[embed], ephemeral=False)


# ── v3.2.0 — /wp (watch parties) ────────────────────────────────────────────

WATCH_PARTY_STATUS_LABEL = {
    "active":    S.WP_STATUS_ACTIVE,
    "completed": S.WP_STATUS_COMPLETED,
    "abandoned": S.WP_STATUS_ABANDONED,
}


def _wp_subcommand(event: dict) -> tuple[str, dict]:
    raw = event.get("options") or []
    if not isinstance(raw, list) or not raw:
        return "", {}
    first = raw[0] if isinstance(raw[0], dict) else {}
    name = (first.get("name") or "").strip()
    sub_opts = {
        o["name"]: o["value"]
        for o in (first.get("options") or [])
        if isinstance(o, dict) and "name" in o
    }
    return name, sub_opts


def _get_watch_party(ctx: Context, party_id: int) -> dict | None:
    if not party_id:
        return None
    row = ctx.sql.query_one(
        "SELECT party_id, media_id, created_by, status FROM otaku_watch_parties "
        "WHERE party_id = %s",
        [party_id],
    )
    return row or None


def _get_party_member(ctx: Context, party_id: int, user_id: str) -> dict | None:
    row = ctx.sql.query_one(
        "SELECT episodes_watched FROM otaku_watch_party_members "
        "WHERE party_id = %s AND user_id = %s",
        [party_id, user_id],
    )
    return row or None


def _wp_create_embed_and_buttons(
    party_id: int, media: dict, creator_id: str
) -> tuple[dict, list]:
    title = _format_title(media)
    body = S.WP_CREATED_BODY.format(party_id=party_id, user=creator_id)
    embed = {
        "title": S.WP_CREATED_TITLE.format(title=title),
        "description": body,
        "color": ANILIST_COLOR,
        "footer": {"text": S.FOOTER_ANILIST},
    }
    cover = (media.get("coverImage") or {}).get("large")
    if cover:
        embed["thumbnail"] = {"url": cover}
    components = [ActionRow(
        Button(S.WP_JOIN_BUTTON, custom_id=f"otaku:wp-join:{party_id}", style="primary", emoji="🎬"),
    )]
    return embed, components


def _wp_create(ctx: Context, event: dict, opts: dict) -> None:
    user_id = event.get("user_id") or ""
    raw_anime = str(opts.get("anime") or "").strip()
    if not raw_anime:
        ctx.interaction.respond(content=S.WP_CREATE_USAGE, ephemeral=True)
        return

    ctx.interaction.defer()
    media, err = _resolve_anime_arg_for_swl(ctx, raw_anime)
    if media is None:
        if err is None:
            _reply_anilist_failure(ctx, deferred=True)
        else:
            _reply_error(ctx, err, deferred=True)
        return

    media_id = int(media.get("id") or 0)
    # INSERT ... RETURNING is the canonical "give me the new party_id" path.
    new_party = ctx.sql.query_one(
        "INSERT INTO otaku_watch_parties (media_id, created_by, status) "
        "VALUES (%s, %s, 'active') RETURNING party_id",
        [media_id, user_id],
    )
    party_id = 0
    if isinstance(new_party, dict) and new_party.get("party_id") is not None:
        try:
            party_id = int(new_party["party_id"])
        except (TypeError, ValueError):
            party_id = 0

    # Auto-add the creator as a member.
    ctx.sql.execute(
        "INSERT INTO otaku_watch_party_members (party_id, user_id, episodes_watched) "
        "VALUES (%s, %s, 0) "
        "ON CONFLICT (party_id, user_id) DO NOTHING",
        [party_id, user_id],
    )

    embed, components = _wp_create_embed_and_buttons(party_id, media, user_id)
    ctx.interaction.followup(embeds=[embed], components=components, ephemeral=False)


def _wp_join_internal(ctx: Context, party_id: int, user_id: str, *, deferred: bool) -> None:
    """Shared logic for `/wp join` and the [Join party] button."""
    party = _get_watch_party(ctx, party_id)
    if party is None:
        _reply_error(ctx, S.WP_NOT_FOUND.format(party_id=party_id), deferred=deferred)
        return
    existing = _get_party_member(ctx, party_id, user_id)
    if existing is not None:
        _reply_error(ctx, S.WP_ALREADY_JOINED.format(party_id=party_id), deferred=deferred)
        return
    ctx.sql.execute(
        "INSERT INTO otaku_watch_party_members (party_id, user_id, episodes_watched) "
        "VALUES (%s, %s, 0) "
        "ON CONFLICT (party_id, user_id) DO NOTHING",
        [party_id, user_id],
    )
    # Fetch the title so the confirmation isn't just an integer.
    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": int(party["media_id"])})
    title = _format_title((data or {}).get("Media") or {}) if data else f"#{party['media_id']}"
    msg = S.WP_JOINED.format(party_id=party_id, title=title)
    if deferred:
        ctx.interaction.followup(content=msg, ephemeral=True)
    else:
        ctx.interaction.respond(content=msg, ephemeral=True)


def _wp_join(ctx: Context, event: dict, opts: dict) -> None:
    user_id = event.get("user_id") or ""
    try:
        party_id = int(opts.get("id"))
    except (TypeError, ValueError):
        ctx.interaction.respond(content=S.WP_ID_USAGE, ephemeral=True)
        return
    ctx.interaction.defer(ephemeral=True)
    _wp_join_internal(ctx, party_id, user_id, deferred=True)


def _wp_status(ctx: Context, event: dict, opts: dict) -> None:
    try:
        party_id = int(opts.get("id"))
    except (TypeError, ValueError):
        ctx.interaction.respond(content=S.WP_ID_USAGE, ephemeral=True)
        return
    ctx.interaction.defer()

    party = _get_watch_party(ctx, party_id)
    if party is None:
        _reply_error(ctx, S.WP_NOT_FOUND.format(party_id=party_id), deferred=True)
        return

    members = ctx.sql.query(
        "SELECT user_id, episodes_watched FROM otaku_watch_party_members "
        "WHERE party_id = %s ORDER BY episodes_watched DESC, joined_at ASC",
        [party_id],
    ) or []

    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": int(party["media_id"])})
    media = (data or {}).get("Media") or {}
    title = _format_title(media) if media else f"#{party['media_id']}"
    total = media.get("episodes") if isinstance(media, dict) else None

    if members:
        lines = []
        for r in members:
            uid = r.get("user_id") or "?"
            eps = int(r.get("episodes_watched") or 0)
            of_total = f" / {total}" if isinstance(total, int) and total > 0 else ""
            lines.append(f"• <@{uid}> — episode {eps}{of_total}")
        members_body = "\n".join(lines)
    else:
        members_body = S.WP_STATUS_EMPTY_MEMBERS

    status_label = WATCH_PARTY_STATUS_LABEL.get(party.get("status") or "active", S.WP_STATUS_ACTIVE)
    embed = {
        "title": S.WP_STATUS_HEADER.format(party_id=party_id, title=title),
        "description": members_body,
        "color": ANILIST_COLOR,
        "fields": [
            {"name": "Status", "value": status_label, "inline": True},
            {"name": "Started by", "value": f"<@{party.get('created_by') or '?'}>", "inline": True},
        ],
        "footer": {"text": S.FOOTER_ANILIST},
    }
    cover = (media.get("coverImage") or {}).get("large") if isinstance(media, dict) else None
    if cover:
        embed["thumbnail"] = {"url": cover}

    ctx.interaction.followup(embeds=[embed], ephemeral=False)


def _wp_progress(ctx: Context, event: dict, opts: dict) -> None:
    user_id = event.get("user_id") or ""
    try:
        party_id = int(opts.get("id"))
        episodes_raw = int(opts.get("episode"))
    except (TypeError, ValueError):
        ctx.interaction.respond(content=S.WP_ID_USAGE, ephemeral=True)
        return
    if episodes_raw < 0:
        ctx.interaction.respond(content=S.WP_PROGRESS_NEGATIVE, ephemeral=True)
        return

    ctx.interaction.defer(ephemeral=True)

    party = _get_watch_party(ctx, party_id)
    if party is None:
        _reply_error(ctx, S.WP_NOT_FOUND.format(party_id=party_id), deferred=True)
        return

    existing = _get_party_member(ctx, party_id, user_id)
    if existing is None:
        _reply_error(ctx, S.WP_PROGRESS_NOT_MEMBER.format(party_id=party_id), deferred=True)
        return

    # Look up the show to cap at total episode count.
    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": int(party["media_id"])})
    media = (data or {}).get("Media") or {}
    total = media.get("episodes") if isinstance(media, dict) else None

    capped = episodes_raw
    cap_warning = None
    if isinstance(total, int) and total > 0 and capped > total:
        capped = total
        cap_warning = S.WP_PROGRESS_OVER_TOTAL.format(total=total)

    ctx.sql.execute(
        "UPDATE otaku_watch_party_members SET episodes_watched = %s "
        "WHERE party_id = %s AND user_id = %s",
        [capped, party_id, user_id],
    )

    # If everyone is at the same episode (and the party still has more than one
    # member), announce. If everyone has hit `total`, promote to completed.
    members_now = ctx.sql.query(
        "SELECT episodes_watched FROM otaku_watch_party_members WHERE party_id = %s",
        [party_id],
    ) or []
    sync_announce = None
    if len(members_now) >= 2:
        eps_values = {int(r.get("episodes_watched") or 0) for r in members_now}
        if len(eps_values) == 1:
            sync_announce = S.WP_SYNC_ANNOUNCE.format(party_id=party_id, episode=capped)
    if isinstance(total, int) and total > 0 and capped == total:
        # Mark the party completed if every member is at total.
        if all(int(r.get("episodes_watched") or 0) >= total for r in members_now):
            ctx.sql.execute(
                "UPDATE otaku_watch_parties SET status = 'completed' WHERE party_id = %s",
                [party_id],
            )

    of_total = f" / {total}" if isinstance(total, int) and total > 0 else ""
    main_msg = S.WP_PROGRESS_UPDATED.format(
        party_id=party_id, episodes=capped, of_total=of_total
    )
    if cap_warning:
        main_msg = f"{cap_warning}\n{main_msg}"
    ctx.interaction.followup(content=main_msg, ephemeral=True)

    # The sync announcement goes out as a public follow-up so everyone sees it.
    if sync_announce:
        ctx.interaction.followup(content=sync_announce, ephemeral=False)


@plugin.on_slash_command("wp")
def cmd_wp(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    subcommand, sub_opts = _wp_subcommand(event)
    if subcommand == "create":
        _wp_create(ctx, event, sub_opts)
        return
    if subcommand == "join":
        _wp_join(ctx, event, sub_opts)
        return
    if subcommand == "status":
        _wp_status(ctx, event, sub_opts)
        return
    if subcommand == "progress":
        _wp_progress(ctx, event, sub_opts)
        return
    ctx.interaction.respond(content=S.WP_ID_USAGE, ephemeral=True)


# ── v3.1.0 — /compare ───────────────────────────────────────────────────────

COMPARE_LIST_LIMIT = 5  # cap each field's bullet list at five entries


def _user_rows_keyed_by_media(ctx: Context, user_id: str) -> dict[int, dict]:
    """Return {media_id: {status, is_favorite, rating}} for every anime row a user has."""
    rows = ctx.sql.query(
        "SELECT media_id, status, is_favorite, rating FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime'",
        [user_id],
    ) or []
    return {
        int(r["media_id"]): {
            "status": r.get("status") or "watching",
            "is_favorite": bool(r.get("is_favorite")),
            "rating": int(r["rating"]) if r.get("rating") is not None else None,
        }
        for r in rows
        if r.get("media_id") is not None
    }


def _compare_users(my_rows: dict[int, dict], their_rows: dict[int, dict]) -> dict:
    """Compute shared favorites / divergent ratings / completion-gap recs."""
    my_ids = set(my_rows.keys())
    their_ids = set(their_rows.keys())

    shared_favs = sorted(
        mid for mid in my_ids & their_ids
        if my_rows[mid]["is_favorite"] and their_rows[mid]["is_favorite"]
    )

    divergent: list[tuple[int, int, int]] = []  # (media_id, my_rating, their_rating)
    for mid in sorted(my_ids & their_ids):
        my_r = my_rows[mid]["rating"]
        their_r = their_rows[mid]["rating"]
        if my_r is None or their_r is None:
            continue
        if abs(my_r - their_r) >= 4:  # ≥ 2 points apart on the 1–10 scale (rating is ×2)
            divergent.append((mid, my_r, their_r))
    # Order by the largest gap first so the spiciest disagreements lead.
    divergent.sort(key=lambda t: abs(t[1] - t[2]), reverse=True)

    completion_recs = sorted(
        mid for mid in their_ids - my_ids
        if their_rows[mid]["status"] == "completed"
    )

    return {
        "shared_favorites": shared_favs[:COMPARE_LIST_LIMIT],
        "divergent_ratings": divergent[:COMPARE_LIST_LIMIT],
        "completion_recs": completion_recs[:COMPARE_LIST_LIMIT],
        "my_total": len(my_ids),
        "their_total": len(their_ids),
        "shared_total": len(my_ids & their_ids),
    }


def _format_compare_lines(
    media_ids: list[int],
    media_by_id: dict[int, dict],
) -> str:
    """Bullet list of titles. Falls back to `#<id>` if AniList didn't return the title."""
    if not media_ids:
        return S.COMPARE_NONE
    lines = []
    for mid in media_ids:
        m = media_by_id.get(int(mid))
        if m:
            title = _format_title(m)
            url = m.get("siteUrl") or ""
            lines.append(f"• [{title}]({url})" if url else f"• {title}")
        else:
            lines.append(f"• #{mid}")
    return "\n".join(lines)


def _format_divergent_lines(
    divergent: list[tuple[int, int, int]],
    media_by_id: dict[int, dict],
) -> str:
    if not divergent:
        return S.COMPARE_NONE
    lines = []
    for mid, mine, theirs in divergent:
        m = media_by_id.get(int(mid))
        title = _format_title(m) if m else f"#{mid}"
        url = (m or {}).get("siteUrl") or ""
        anchor = f"[{title}]({url})" if url else title
        lines.append(f"• {anchor} — you: {_format_rating(mine)} · them: {_format_rating(theirs)}")
    return "\n".join(lines)


@plugin.on_slash_command("compare")
def cmd_compare(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    other_id = str(opts.get("user") or "").strip()
    if not other_id or other_id == user_id:
        ctx.interaction.respond(content=S.COMPARE_SELF, ephemeral=True)
        return

    ctx.interaction.defer(ephemeral=True)
    my_rows = _user_rows_keyed_by_media(ctx, user_id)
    their_rows = _user_rows_keyed_by_media(ctx, other_id)

    if not my_rows and not their_rows:
        _reply_error(ctx, S.COMPARE_EMPTY_BOTH, deferred=True)
        return
    if not my_rows:
        _reply_error(ctx, S.COMPARE_EMPTY_YOU, deferred=True)
        return
    if not their_rows:
        _reply_error(ctx, S.COMPARE_EMPTY_THEM.format(other=other_id), deferred=True)
        return

    result = _compare_users(my_rows, their_rows)

    # Collect every media_id we want a title for, then one AniList batch call.
    title_ids: set[int] = set(result["shared_favorites"])
    title_ids.update(mid for mid, _, _ in result["divergent_ratings"])
    title_ids.update(result["completion_recs"])
    media_by_id: dict[int, dict] = {}
    if title_ids:
        data = _anilist_query(
            ctx, QUERY_MEDIA_BATCH, {"ids": sorted(title_ids)}, cache=True
        )
        if data is not None:
            for m in ((data.get("Page") or {}).get("media") or []):
                mid = m.get("id")
                if isinstance(mid, int):
                    media_by_id[mid] = m

    embed = {
        "title": S.COMPARE_HEADER.format(other=other_id),
        "color": ANILIST_COLOR,
        "fields": [
            {
                "name": S.COMPARE_FIELD_TOTALS,
                "value": (
                    f"You: **{result['my_total']}** · Them: **{result['their_total']}** · "
                    f"Both: **{result['shared_total']}**"
                ),
                "inline": False,
            },
            {
                "name": S.COMPARE_FIELD_SHARED,
                "value": _format_compare_lines(result["shared_favorites"], media_by_id),
                "inline": False,
            },
            {
                "name": S.COMPARE_FIELD_DIVERGENT,
                "value": _format_divergent_lines(result["divergent_ratings"], media_by_id),
                "inline": False,
            },
            {
                "name": S.COMPARE_FIELD_RECS,
                "value": _format_compare_lines(result["completion_recs"], media_by_id),
                "inline": False,
            },
        ],
        "footer": {"text": S.FOOTER_ANILIST},
    }
    ctx.interaction.followup(embeds=[embed], ephemeral=True)


# ── v3.0.0 — /server-watchlist ──────────────────────────────────────────────

SWL_PAGE_SIZE = PER_PAGE  # 5; consistent with /list and /discover


def _swl_subcommand(event: dict) -> tuple[str, dict]:
    """Pull (subcommand_name, options_dict) out of a /server-watchlist event."""
    raw = event.get("options") or []
    if not isinstance(raw, list) or not raw:
        return "", {}
    first = raw[0]
    if not isinstance(first, dict):
        return "", {}
    name = (first.get("name") or "").strip()
    sub_opts = {
        o["name"]: o["value"]
        for o in (first.get("options") or [])
        if isinstance(o, dict) and "name" in o
    }
    return name, sub_opts


def _resolve_anime_arg_for_swl(
    ctx: Context, anime_arg: str
) -> tuple[dict | None, str | None]:
    """Resolve an `anime:` option to a media dict.

    Returns (media, error). On AniList failure media is None and error is None
    (the caller should surface the standard AniList failure). On a clean
    no-match, error contains the user-facing message.
    """
    anime_arg = (anime_arg or "").strip()
    if not anime_arg:
        return None, S.SWL_ADD_USAGE
    # Accept either a numeric AniList media ID or a search string.
    try:
        media_id = int(anime_arg)
    except ValueError:
        media_id = None
    if media_id is not None:
        data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": media_id})
    else:
        data = _anilist_query(ctx, QUERY_SEARCH_ONE, {"q": anime_arg.lower()}, cache=True)
    if data is None:
        return None, None
    media = data.get("Media")
    if not media:
        return None, S.ANIME_NOT_FOUND.format(query=_truncate(anime_arg, 80))
    return media, None


def _swl_add(ctx: Context, event: dict, opts: dict) -> None:
    user_id = event.get("user_id") or ""
    ctx.interaction.defer(ephemeral=True)
    if not _caller_is_admin(ctx, user_id):
        ctx.interaction.followup(content=S.SWL_ADMIN_DENIED, ephemeral=True)
        return

    media, err = _resolve_anime_arg_for_swl(ctx, str(opts.get("anime") or ""))
    if media is None:
        if err is None:
            _reply_anilist_failure(ctx, deferred=True)
        else:
            _reply_error(ctx, err, deferred=True)
        return

    media_id = int(media.get("id") or 0)
    title = _format_title(media)
    note = (opts.get("note") or "").strip() or None

    # Check existence so we can give a clean "already on the watchlist" message.
    existing = ctx.sql.query_one(
        "SELECT 1 FROM otaku_server_watchlist WHERE media_id = %s",
        [media_id],
    )
    if existing:
        ctx.interaction.followup(content=S.SWL_ALREADY.format(title=title), ephemeral=True)
        return

    ctx.sql.execute(
        "INSERT INTO otaku_server_watchlist (media_id, added_by, note) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (media_id) DO NOTHING",
        [media_id, user_id, note],
    )
    ctx.interaction.followup(content=S.SWL_ADDED.format(title=title), ephemeral=True)


def _swl_remove(ctx: Context, event: dict, opts: dict) -> None:
    user_id = event.get("user_id") or ""
    ctx.interaction.defer(ephemeral=True)
    if not _caller_is_admin(ctx, user_id):
        ctx.interaction.followup(content=S.SWL_ADMIN_DENIED, ephemeral=True)
        return

    media, err = _resolve_anime_arg_for_swl(ctx, str(opts.get("anime") or ""))
    if media is None:
        if err is None:
            _reply_anilist_failure(ctx, deferred=True)
        else:
            _reply_error(ctx, err, deferred=True)
        return

    media_id = int(media.get("id") or 0)
    title = _format_title(media)

    existing = ctx.sql.query_one(
        "SELECT 1 FROM otaku_server_watchlist WHERE media_id = %s",
        [media_id],
    )
    if not existing:
        ctx.interaction.followup(content=S.SWL_NOT_PRESENT.format(title=title), ephemeral=True)
        return

    ctx.sql.execute(
        "DELETE FROM otaku_server_watchlist WHERE media_id = %s",
        [media_id],
    )
    ctx.interaction.followup(content=S.SWL_REMOVED.format(title=title), ephemeral=True)


def _render_server_watchlist(ctx: Context, *, page: int, deferred: bool) -> None:
    """Shared renderer for /server-watchlist view + pagination clicks."""
    offset = max(0, (page - 1) * SWL_PAGE_SIZE)
    limit = SWL_PAGE_SIZE + 1
    rows = ctx.sql.query(
        "SELECT media_id, added_by, note FROM otaku_server_watchlist "
        "ORDER BY added_at DESC LIMIT %s OFFSET %s",
        [limit, offset],
    ) or []
    has_next = len(rows) > SWL_PAGE_SIZE
    rows = rows[:SWL_PAGE_SIZE]

    if not rows:
        _reply_error(ctx, S.SWL_EMPTY, deferred=deferred)
        return

    rows_by_id = {int(r["media_id"]): r for r in rows}
    ids = list(rows_by_id.keys())
    data = _anilist_query(ctx, QUERY_MEDIA_BATCH, {"ids": ids}, cache=True)
    if data is None:
        _reply_anilist_failure(ctx, deferred=deferred)
        return
    media_list = ((data.get("Page") or {}).get("media")) or []

    def _sort_key(m: dict) -> int:
        mid = int(m.get("id") or -1)
        return ids.index(mid) if mid in ids else 9999

    ordered = sorted(media_list, key=_sort_key)

    lines = []
    for i, m in enumerate(ordered, start=1):
        title = _format_title(m)
        url = m.get("siteUrl") or ""
        row = rows_by_id.get(int(m.get("id") or 0)) or {}
        note = (row.get("note") or "").strip()
        added_by = row.get("added_by") or ""
        suffix = f" — *{note}*" if note else ""
        lines.append(f"**{i}. [{title}]({url})**{suffix}\n  · added by <@{added_by}>")

    embed = _make_list_embed(ordered, S.SWL_HEADER, page=page, has_next=has_next)
    embed["description"] = "\n\n".join(lines)

    prev_id = f"otaku:swl:{page - 1}" if page > 1 else None
    next_id = f"otaku:swl:{page + 1}" if has_next else None
    components = [_page_buttons(prev_id, next_id)]
    select_row = _make_select_row(ordered)
    if select_row is not None:
        components.append(select_row)

    if deferred:
        ctx.interaction.followup(embeds=[embed], components=components, ephemeral=False)
    else:
        ctx.interaction.respond(embeds=[embed], components=components, ephemeral=False)


@plugin.on_slash_command("server-watchlist")
def cmd_server_watchlist(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return

    subcommand, sub_opts = _swl_subcommand(event)
    if subcommand == "add":
        _swl_add(ctx, event, sub_opts)
        return
    if subcommand == "remove":
        _swl_remove(ctx, event, sub_opts)
        return
    # Default (subcommand == "view" or empty) — public, non-ephemeral browse.
    ctx.interaction.defer()
    _render_server_watchlist(ctx, page=1, deferred=True)


# ── v2.4.0 — /import anilist ────────────────────────────────────────────────

# AniList MediaList.status → our status column.
ANILIST_STATUS_MAP = {
    "CURRENT":   "watching",
    "REPEATING": "watching",
    "COMPLETED": "completed",
    "PAUSED":    "on_hold",
    "DROPPED":   "dropped",
    "PLANNING":  "plan",
}

# Cap the number of pages we'll pull from AniList in a single /import call.
# At 50 entries per page, 100 pages = 5000 anime — well past anyone's actual list.
IMPORT_MAX_PAGES = 100


def _row_from_medialist(entry: dict) -> tuple[int, str, int, int | None] | None:
    """Convert one AniList MediaList entry to our row tuple, or None if it's garbage.

    Returns (media_id, status, episodes_watched, rating_or_none) where rating is the
    encoded SMALLINT (2..20) or None if unrated.
    """
    media = entry.get("media") or {}
    media_id = media.get("id")
    if not isinstance(media_id, int) or media_id <= 0:
        return None
    raw_status = entry.get("status") or "CURRENT"
    status = ANILIST_STATUS_MAP.get(str(raw_status).upper(), "watching")
    progress = entry.get("progress")
    try:
        episodes = max(0, int(progress or 0))
    except (TypeError, ValueError):
        episodes = 0
    score = entry.get("score")
    rating: int | None = None
    if score is not None:
        try:
            s = float(score)
        except (TypeError, ValueError):
            s = 0.0
        if s > 0:
            rating = max(2, min(20, int(round(s * 2))))
    return (media_id, status, episodes, rating)


@plugin.on_slash_command("import")
def cmd_import(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    username = (opts.get("anilist") or "").strip()
    if not username:
        ctx.interaction.respond(content=S.IMPORT_USERNAME_BLANK, ephemeral=True)
        return

    ctx.interaction.defer(ephemeral=True)

    total = 0
    new_count = 0
    updated = 0
    pages_pulled = 0
    aborted = False
    user_found = True

    for page in range(1, IMPORT_MAX_PAGES + 1):
        data = _anilist_query(
            ctx, QUERY_USER_MEDIALIST_PAGE, {"userName": username, "page": page}
        )
        if data is None:
            # First page: surface as "user not found" since the typical 4xx path
            # for this query is "no such user". Subsequent pages: partial.
            if page == 1:
                user_found = False
            else:
                aborted = True
            break

        page_obj = data.get("Page") or {}
        entries = page_obj.get("mediaList") or []
        if not entries and page == 1:
            user_found = False
            break

        pages_pulled = page
        for entry in entries:
            row = _row_from_medialist(entry)
            if row is None:
                continue
            media_id, status, episodes, rating = row

            # Check whether the row already exists so we can report new vs updated.
            # `/import anilist` only touches anime rows; v8.x can layer a
            # parallel manga importer if AniList ever exposes manga lists.
            existing = ctx.sql.query_one(
                "SELECT 1 FROM otaku_user_media "
                "WHERE user_id = %s AND media_id = %s AND media_type = 'anime'",
                [user_id, media_id],
            )

            # We only touch the columns the import provides. is_favorite is left alone.
            ctx.sql.execute(
                "INSERT INTO otaku_user_media (user_id, media_id, media_type, status, episodes_watched, rating) "
                "VALUES (%s, %s, 'anime', %s, %s, %s) "
                "ON CONFLICT (user_id, media_id, media_type) DO UPDATE SET "
                "  status = EXCLUDED.status, "
                "  episodes_watched = EXCLUDED.episodes_watched, "
                "  rating = COALESCE(EXCLUDED.rating, otaku_user_media.rating)",
                [user_id, media_id, status, episodes, rating],
            )
            total += 1
            if existing:
                updated += 1
            else:
                new_count += 1

        if not (page_obj.get("pageInfo") or {}).get("hasNextPage"):
            break

    if not user_found:
        _reply_error(ctx, S.IMPORT_USER_NOT_FOUND.format(username=username), deferred=True)
        return

    if aborted:
        ctx.interaction.followup(
            content=S.IMPORT_PARTIAL.format(pages=pages_pulled, total=total),
            ephemeral=True,
        )
        return

    ctx.interaction.followup(
        content=S.IMPORT_SUMMARY.format(
            username=username, total=total, new=new_count, updated=updated
        ),
        ephemeral=True,
    )


# ── v5.1.0 — /my-stats (richer self-view) ───────────────────────────────────

MY_STATS_TOP_RATED_LIMIT = 5
MY_STATS_TOP_FAVORITES_LIMIT = 5
MY_STATS_RECENT_COMPLETED_LIMIT = 5


def _my_stats_top_rated(ctx: Context, user_id: str) -> list[dict]:
    return ctx.sql.query(
        "SELECT media_id, rating FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' AND rating IS NOT NULL "
        "ORDER BY rating DESC, added_at DESC LIMIT %s",
        [user_id, MY_STATS_TOP_RATED_LIMIT],
    ) or []


def _my_stats_top_favorites(ctx: Context, user_id: str) -> list[dict]:
    return ctx.sql.query(
        "SELECT media_id FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' AND is_favorite = TRUE "
        "ORDER BY added_at DESC LIMIT %s",
        [user_id, MY_STATS_TOP_FAVORITES_LIMIT],
    ) or []


def _my_stats_recently_completed(ctx: Context, user_id: str) -> list[dict]:
    return ctx.sql.query(
        "SELECT media_id FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' AND status = 'completed' "
        "ORDER BY added_at DESC LIMIT %s",
        [user_id, MY_STATS_RECENT_COMPLETED_LIMIT],
    ) or []


def _format_titled_lines(
    media_ids: list[int],
    media_by_id: dict[int, dict],
    suffixes: dict[int, str] | None = None,
) -> str:
    """Bulleted list of titles. Optional per-id suffix (e.g. rating string)."""
    if not media_ids:
        return S.MY_STATS_NONE
    lines = []
    for mid in media_ids:
        m = media_by_id.get(int(mid))
        title = _format_title(m) if m else f"#{mid}"
        url = (m or {}).get("siteUrl") or ""
        anchor = f"[{title}]({url})" if url else title
        suffix = (suffixes or {}).get(int(mid), "")
        lines.append(f"• {anchor}{(' ' + suffix) if suffix else ''}")
    return "\n".join(lines)


@plugin.on_slash_command("my-stats")
def cmd_my_stats(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    ctx.interaction.defer(ephemeral=True)

    agg = _aggregate_user_stats(ctx, user_id)
    if not agg:
        _reply_error(ctx, S.MY_STATS_EMPTY, deferred=True)
        return

    top_rated = _my_stats_top_rated(ctx, user_id)
    top_favs = _my_stats_top_favorites(ctx, user_id)
    recent_done = _my_stats_recently_completed(ctx, user_id)

    # Collect every media_id we need a title for, then ONE AniList batch call.
    title_ids: set[int] = set()
    title_ids.update(int(r["media_id"]) for r in top_rated)
    title_ids.update(int(r["media_id"]) for r in top_favs)
    title_ids.update(int(r["media_id"]) for r in recent_done)

    media_by_id: dict[int, dict] = {}
    if title_ids:
        data = _anilist_query(
            ctx, QUERY_MEDIA_BATCH, {"ids": sorted(title_ids)}, cache=True
        )
        if data:
            for m in ((data.get("Page") or {}).get("media") or []):
                mid = m.get("id")
                if isinstance(mid, int):
                    media_by_id[mid] = m

    top_rated_ids = [int(r["media_id"]) for r in top_rated]
    rating_suffix = {int(r["media_id"]): f"· 🎯 {_format_rating(int(r['rating']))}/10" for r in top_rated}
    fav_ids = [int(r["media_id"]) for r in top_favs]
    done_ids = [int(r["media_id"]) for r in recent_done]

    by_status = agg["by_status"]
    completion_pct = (by_status.get("completed", 0) / agg["total"] * 100) if agg["total"] else 0
    fields = [
        {"name": "Total tracked", "value": f"{agg['total']:,}", "inline": True},
        {"name": "✅ Completed", "value": f"{by_status.get('completed', 0)} ({completion_pct:.0f}%)", "inline": True},
        {"name": "📺 Watching", "value": str(by_status.get("watching", 0)), "inline": True},
        {"name": "Episodes", "value": f"{agg['total_episodes']:,}", "inline": True},
        {"name": "Est. hours", "value": f"{agg['total_hours']:.1f}", "inline": True},
        {
            "name": "Mean score",
            "value": (
                f"{agg['mean_rating'] / 2:.1f}/10 ({agg['rated_count']})"
                if agg["mean_rating"] is not None else "—"
            ),
            "inline": True,
        },
        {
            "name": "🎯 Top rated",
            "value": _format_titled_lines(top_rated_ids, media_by_id, rating_suffix),
            "inline": False,
        },
        {
            "name": "⭐ Top favorites",
            "value": _format_titled_lines(fav_ids, media_by_id),
            "inline": False,
        },
        {
            "name": "✅ Recently completed",
            "value": _format_titled_lines(done_ids, media_by_id),
            "inline": False,
        },
    ]

    embed = {
        "title": S.MY_STATS_HEADER,
        "color": ANILIST_COLOR,
        "fields": fields,
        "footer": {"text": f"{STATS_MINUTES_PER_EPISODE}min/episode heuristic · Data from AniList"},
    }
    ctx.interaction.followup(embeds=[embed], ephemeral=True)


# ── v2.3.0 — /stats ─────────────────────────────────────────────────────────

# Heuristic episode length for the "total hours" estimate. Real anime episodes
# vary 11–24 min; 24 lines up with AniList's standard TV slot length.
STATS_MINUTES_PER_EPISODE = 24
STATS_TOP_GENRE_SAMPLE = 50  # Most-recent N anime sampled to pick the top genre.


def _aggregate_user_stats(ctx: Context, user_id: str) -> dict:
    """Return a dict of aggregate stats for a user's anime rows. Empty dict
    if the user has no anime rows.

    v10.0.8 — was a single SQL aggregate (`GROUP BY status` with
    `COUNT/SUM/AVG`). The live host rejects it with the generic
    `SQL execution failed. Check your query syntax and parameters`;
    cause not yet isolated (same error fires for the simpler
    `_recommend_user_vector` query, so it isn't aggregates per se).
    v10.0.8 splits this into a plain row-shape SELECT and aggregates in
    Python — sidesteps any complex-query parser issue and keeps `/stats`
    serving the typical user (≤1 000 rows; capped at the SDK ceiling).
    """
    rows = ctx.sql.query(
        "SELECT status, episodes_watched, rating "
        "FROM otaku_user_media WHERE user_id = %s AND media_type = 'anime'",
        [user_id],
    ) or []
    if not rows:
        return {}

    by_status: dict[str, int] = {s: 0 for s in VALID_STATUSES}
    total_episodes = 0
    rating_acc = 0.0
    rated_count = 0
    for r in rows:
        status = r.get("status") or "watching"
        by_status[status] = by_status.get(status, 0) + 1
        ep = r.get("episodes_watched")
        if ep is not None:
            total_episodes += int(ep)
        rating = r.get("rating")
        if rating is not None:
            rating_acc += float(rating)
            rated_count += 1

    overall_mean = (rating_acc / rated_count) if rated_count else None
    total = sum(by_status.values())
    total_hours = round((total_episodes * STATS_MINUTES_PER_EPISODE) / 60, 1)
    return {
        "total": total,
        "by_status": by_status,
        "total_episodes": total_episodes,
        "total_hours": total_hours,
        "mean_rating": overall_mean,
        "rated_count": rated_count,
    }


def _top_genre_for_user(ctx: Context, user_id: str) -> str | None:
    """Look up the user's most-recent N anime on AniList and return their top genre."""
    rows = ctx.sql.query(
        "SELECT media_id FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' "
        "ORDER BY added_at DESC LIMIT %s",
        [user_id, STATS_TOP_GENRE_SAMPLE],
    ) or []
    if not rows:
        return None
    ids = [int(r["media_id"]) for r in rows if r.get("media_id") is not None]
    if not ids:
        return None
    data = _anilist_query(ctx, QUERY_MEDIA_BATCH, {"ids": ids}, cache=True)
    if data is None:
        return None
    media_list = ((data.get("Page") or {}).get("media")) or []
    counts: dict[str, int] = {}
    for m in media_list:
        for g in (m.get("genres") or []):
            counts[g] = counts.get(g, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


@plugin.on_slash_command("stats")
def cmd_stats(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    target_id, display, is_self = _extract_target_user(event, opts)
    ctx.interaction.defer(ephemeral=True)

    agg = _aggregate_user_stats(ctx, target_id)
    if not agg:
        empty = S.STATS_EMPTY_OWN if is_self else S.STATS_EMPTY_OTHER.format(who=display)
        _reply_error(ctx, empty, deferred=True)
        return

    top_genre = _top_genre_for_user(ctx, target_id)

    by_status = agg["by_status"]
    fields = [
        {"name": "Total tracked", "value": f"{agg['total']:,}", "inline": True},
        {"name": "Episodes",      "value": f"{agg['total_episodes']:,}", "inline": True},
        {"name": "Est. hours",    "value": f"{agg['total_hours']:.1f}", "inline": True},
        {"name": "📺 Watching",    "value": str(by_status.get("watching", 0)),  "inline": True},
        {"name": "✅ Completed",   "value": str(by_status.get("completed", 0)), "inline": True},
        {"name": "❌ Dropped",     "value": str(by_status.get("dropped", 0)),   "inline": True},
        {"name": "⏸ On hold",     "value": str(by_status.get("on_hold", 0)),   "inline": True},
        {"name": "📌 Plan",        "value": str(by_status.get("plan", 0)),      "inline": True},
        {
            "name": "Mean score",
            "value": (
                f"{agg['mean_rating'] / 2:.1f}/10 ({agg['rated_count']} rated)"
                if agg["mean_rating"] is not None else "—"
            ),
            "inline": True,
        },
    ]
    if top_genre:
        fields.append({"name": "Top genre", "value": top_genre, "inline": False})

    header = S.STATS_HEADER_OWN if is_self else S.STATS_HEADER_OTHER.format(who=display)
    embed = {
        "title": header,
        "color": ANILIST_COLOR,
        "fields": fields,
        "footer": {"text": f"{STATS_MINUTES_PER_EPISODE}min/episode heuristic · Data from AniList"},
    }
    ctx.interaction.followup(embeds=[embed], ephemeral=True)


# ── v2.2.0 — episode progress ───────────────────────────────────────────────

def _get_user_tracking(
    ctx: Context, user_id: str, media_id: int, *, media_type: str = "anime"
) -> tuple[int, int | None]:
    """Look up the user's recorded (episodes_watched, rating) for a media id.

    Returns (0, None) when there's no row. `rating` is the SMALLINT (2..20)
    that v2.1 stored, or None if unrated. v8.0 added the media_type filter
    so anime and manga rows with the same media_id don't shadow each other.
    """
    if not user_id or not media_id:
        return 0, None
    row = ctx.sql.query_one(
        "SELECT episodes_watched, rating FROM otaku_user_media "
        "WHERE user_id = %s AND media_id = %s AND media_type = %s",
        [user_id, media_id, media_type],
    )
    if not row:
        return 0, None
    try:
        progress = max(0, int(row.get("episodes_watched") or 0))
    except (TypeError, ValueError):
        progress = 0
    raw_rating = row.get("rating")
    rating: int | None
    try:
        rating = int(raw_rating) if raw_rating is not None else None
    except (TypeError, ValueError):
        rating = None
    return progress, rating


@plugin.on_slash_command("progress")
def cmd_progress(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    raw = opts.get("episodes")
    try:
        episodes = int(raw)
    except (TypeError, ValueError):
        ctx.interaction.respond(content=S.PROGRESS_NEGATIVE, ephemeral=True)
        return
    if episodes < 0:
        ctx.interaction.respond(content=S.PROGRESS_NEGATIVE, ephemeral=True)
        return

    ctx.interaction.defer(ephemeral=True)
    media_id = _resolve_last_anime_id(ctx, user_id)
    if media_id is None:
        _reply_error(ctx, S.PROGRESS_NO_CACHE, deferred=True)
        return
    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": media_id})
    if data is None or not data.get("Media"):
        _reply_anilist_failure(ctx, deferred=True, fallback=S.SIMILAR_FETCH_FAIL_CACHED)
        return
    media = data["Media"]
    title = _format_title(media)
    total = media.get("episodes")

    # Cap episodes to the total if known. Surface the cap to the user.
    capped = episodes
    capped_msg = None
    if isinstance(total, int) and total > 0 and episodes > total:
        capped = total
        capped_msg = S.PROGRESS_OVER_TOTAL.format(title=title, total=total)

    # If marking complete, also flip status to 'completed' for nicer /list filtering.
    upsert_status = "completed" if (isinstance(total, int) and total > 0 and capped == total) else "watching"

    # /progress is anime-only in v8.0; a v8.x patch could mirror this for
    # manga chapters (the column is generic enough).
    ctx.sql.execute(
        "INSERT INTO otaku_user_media (user_id, media_id, media_type, status, episodes_watched) "
        "VALUES (%s, %s, 'anime', %s, %s) "
        "ON CONFLICT (user_id, media_id, media_type) DO UPDATE SET "
        "  episodes_watched = EXCLUDED.episodes_watched, "
        "  status = CASE WHEN EXCLUDED.status = 'completed' THEN 'completed' "
        "               ELSE otaku_user_media.status END",
        [user_id, media_id, upsert_status, capped],
    )

    of_total = S.PROGRESS_OF_TOTAL.format(total=total) if isinstance(total, int) and total > 0 else ""
    main_msg = S.PROGRESS_SET.format(title=title, episodes=capped, of_total=of_total)
    if capped_msg:
        main_msg = f"{capped_msg}\n{main_msg}"
    ctx.interaction.followup(content=main_msg, ephemeral=True)


# ── v2.1.0 — ratings ────────────────────────────────────────────────────────

def _encode_rating(score: float) -> int | None:
    """Convert a 1.0–10.0 user score to the SMALLINT we store (2..20). None if out of range."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s < 1.0 or s > 10.0:
        return None
    return int(round(s * 2))


def _format_rating(stored: int | None) -> str:
    """Render the SMALLINT back to a user-visible score like '7.5'."""
    if stored is None:
        return "—"
    return f"{stored / 2:.1f}"


@plugin.on_slash_command("rate")
def cmd_rate(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    encoded = _encode_rating(opts.get("score"))
    if encoded is None:
        ctx.interaction.respond(content=S.RATE_OUT_OF_RANGE, ephemeral=True)
        return

    ctx.interaction.defer(ephemeral=True)
    media_id = _resolve_last_anime_id(ctx, user_id)
    if media_id is None:
        _reply_error(ctx, S.RATE_NO_CACHE, deferred=True)
        return
    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": media_id})
    if data is None or not data.get("Media"):
        _reply_anilist_failure(ctx, deferred=True, fallback=S.SIMILAR_FETCH_FAIL_CACHED)
        return
    title = _format_title(data["Media"])

    # Upsert the row, setting rating only (status default for new rows is 'watching').
    # /rate is anime-only in v8.0; v8.x patches can layer a manga variant.
    ctx.sql.execute(
        "INSERT INTO otaku_user_media (user_id, media_id, media_type, status, rating) "
        "VALUES (%s, %s, 'anime', %s, %s) "
        "ON CONFLICT (user_id, media_id, media_type) DO UPDATE SET rating = EXCLUDED.rating",
        [user_id, media_id, "watching", encoded],
    )
    ctx.interaction.followup(
        content=S.RATE_SET.format(title=title, score=_format_rating(encoded)),
        ephemeral=True,
    )


@plugin.on_slash_command("ratings")
def cmd_ratings(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    target_id, display, is_self = _extract_target_user(event, opts)
    ctx.interaction.defer(ephemeral=True)

    rows = ctx.sql.query(
        "SELECT media_id, rating, status, is_favorite FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' AND rating IS NOT NULL "
        "ORDER BY rating DESC, added_at DESC LIMIT 25",
        [target_id],
    ) or []

    if not rows:
        empty = S.RATINGS_EMPTY_OWN if is_self else S.RATINGS_EMPTY_OTHER.format(who=display)
        _reply_error(ctx, empty, deferred=True)
        return

    ids = [int(r["media_id"]) for r in rows]
    data = _anilist_query(ctx, QUERY_MEDIA_BATCH, {"ids": ids}, cache=True)
    if data is None:
        _reply_anilist_failure(ctx, deferred=True)
        return
    media_list = ((data.get("Page") or {}).get("media")) or []
    media_by_id = {int(m.get("id") or -1): m for m in media_list}

    lines = []
    for i, row in enumerate(rows, start=1):
        mid = int(row["media_id"])
        m = media_by_id.get(mid)
        title = _format_title(m) if m else f"#{mid}"
        url = (m or {}).get("siteUrl") or ""
        rating = _format_rating(row.get("rating"))
        fav = " ⭐" if row.get("is_favorite") else ""
        if url:
            lines.append(f"**{i}. [{title}]({url})** · 🎯 {rating}/10{fav}")
        else:
            lines.append(f"**{i}. {title}** · 🎯 {rating}/10{fav}")

    header = S.RATINGS_HEADER_OWN if is_self else S.RATINGS_HEADER_OTHER.format(who=display)
    embed = {
        "title": header,
        "description": "\n".join(lines),
        "color": ANILIST_COLOR,
        "footer": {"text": f"{len(rows)} rated · top 25 · Data from AniList"},
    }
    ctx.interaction.followup(embeds=[embed], ephemeral=True)


# ── Component handlers ──────────────────────────────────────────────────────

# Components with dynamic args (otaku:similar:<id>, otaku:page:<g>:<s>:<p>,
# otaku:trend:<p>) can't use @plugin.on_component (it matches exact custom_id),
# so we hook the raw event and dispatch ourselves. The static otaku:expand
# select is handled by its own @plugin.on_component registration below.

def _component_dispatch(ctx: Context, event: dict) -> None:
    cid = event.get("custom_id") or ""
    user_id = event.get("user_id") or ""

    if cid.startswith("otaku:page:"):
        if _on_cooldown(ctx, user_id):
            return
        # otaku:page:<genre>:<sort>:<page>
        parts = cid.split(":", 4)
        if len(parts) < 5:
            ctx.interaction.respond(content=S.PAGE_MALFORMED, ephemeral=True)
            return
        _, _, genre, sort_key, page_s = parts
        try:
            page = max(1, int(page_s))
        except ValueError:
            ctx.interaction.respond(content=S.PAGE_MALFORMED, ephemeral=True)
            return
        ctx.interaction.defer(ephemeral=True)
        _render_discover(ctx, genre, sort_key, page=page, deferred=True, ephemeral_reply=True)
        return

    if cid.startswith("otaku:mpage:"):
        if _on_cooldown(ctx, user_id):
            return
        # otaku:mpage:<genre>:<sort>:<page> — v8.0 /manga-discover pagination.
        parts = cid.split(":", 4)
        if len(parts) < 5:
            ctx.interaction.respond(content=S.PAGE_MALFORMED, ephemeral=True)
            return
        _, _, genre, sort_key, page_s = parts
        try:
            page = max(1, int(page_s))
        except ValueError:
            ctx.interaction.respond(content=S.PAGE_MALFORMED, ephemeral=True)
            return
        ctx.interaction.defer()
        _render_manga_discover(ctx, genre, sort_key, page=page, deferred=True)
        return

    if cid.startswith("otaku:trend:"):
        if _on_cooldown(ctx, user_id):
            return
        try:
            page = max(1, int(cid.split(":", 2)[2]))
        except (ValueError, IndexError):
            ctx.interaction.respond(content=S.PAGE_MALFORMED, ephemeral=True)
            return
        ctx.interaction.defer(ephemeral=True)
        _render_trending(ctx, page=page, deferred=True, ephemeral_reply=True)
        return

    if cid.startswith("otaku:popchar:"):
        if _on_cooldown(ctx, user_id):
            return
        # otaku:popchar:<page> — v8.3 popular-characters pagination.
        try:
            page = max(1, int(cid.split(":", 2)[2]))
        except (ValueError, IndexError):
            ctx.interaction.respond(
                content=S.CHARACTER_POPULAR_PAGE_MALFORMED, ephemeral=True
            )
            return
        ctx.interaction.defer()
        _render_character_popular(ctx, page=page, deferred=True)
        return

    if cid.startswith("otaku:similar:"):
        if _on_cooldown(ctx, user_id):
            return
        try:
            media_id = int(cid.split(":", 2)[2])
        except (ValueError, IndexError):
            ctx.interaction.respond(content=S.SIMILAR_BTN_MALFORMED, ephemeral=True)
            return
        ctx.interaction.defer(ephemeral=True)
        data = _anilist_query(ctx, QUERY_SIMILAR_BY_ID, {"id": media_id})
        if data is None or not data.get("Media"):
            _reply_anilist_failure(
                ctx,
                deferred=True,
                fallback=S.SIMILAR_FETCH_FAIL_BUTTON,
            )
            return
        if user_id:
            ctx.kv.set(f"last_anime:user:{user_id}", media_id, ttl_seconds=LAST_ANIME_TTL)
        _render_similar(ctx, data["Media"], deferred=True, ephemeral_reply=True)
        return

    if cid.startswith("otaku:list:"):
        if _on_cooldown(ctx, user_id):
            return
        # otaku:list:<target_user_id>:<scope>:<page>
        parts = cid.split(":", 4)
        if len(parts) < 5:
            ctx.interaction.respond(content=S.LIST_PAGE_MALFORMED, ephemeral=True)
            return
        _, _, target_user_id, scope, page_s = parts
        try:
            page = max(1, int(page_s))
        except ValueError:
            ctx.interaction.respond(content=S.LIST_PAGE_MALFORMED, ephemeral=True)
            return
        is_self = (target_user_id == user_id)
        display = "you" if is_self else f"<@{target_user_id}>"
        ctx.interaction.defer(ephemeral=True)
        _render_user_list(
            ctx,
            target_user_id=target_user_id,
            target_display=display,
            is_self=is_self,
            scope=scope,
            page=page,
            deferred=True,
            ephemeral_reply=True,
        )
        return

    if cid.startswith("otaku:reset-confirm:"):
        if _on_cooldown(ctx, user_id):
            return
        _handle_reset_confirm(ctx, event)
        return

    if cid.startswith("otaku:reset-cancel:"):
        if _on_cooldown(ctx, user_id):
            return
        _handle_reset_cancel(ctx, event)
        return

    if cid.startswith("otaku:swl:"):
        if _on_cooldown(ctx, user_id):
            return
        try:
            page = max(1, int(cid.split(":", 2)[2]))
        except (ValueError, IndexError):
            ctx.interaction.respond(content=S.SWL_PAGE_MALFORMED, ephemeral=True)
            return
        ctx.interaction.defer()
        _render_server_watchlist(ctx, page=page, deferred=True)
        return

    if cid.startswith("otaku:wp-join:"):
        if _on_cooldown(ctx, user_id):
            return
        try:
            party_id = int(cid.split(":", 2)[2])
        except (ValueError, IndexError):
            ctx.interaction.respond(content=S.WP_ID_USAGE, ephemeral=True)
            return
        ctx.interaction.defer(ephemeral=True)
        _wp_join_internal(ctx, party_id, user_id, deferred=True)
        return

    if cid.startswith("otaku:premieres:"):
        if _on_cooldown(ctx, user_id):
            return
        # otaku:premieres:<season>:<year>:<page>
        parts = cid.split(":", 4)
        if len(parts) < 5:
            ctx.interaction.respond(content=S.PREMIERES_PAGE_MALFORMED, ephemeral=True)
            return
        _, _, season, year_s, page_s = parts
        try:
            year = int(year_s)
            page = max(1, int(page_s))
        except ValueError:
            ctx.interaction.respond(content=S.PREMIERES_PAGE_MALFORMED, ephemeral=True)
            return
        ctx.interaction.defer()
        _render_premieres(ctx, season, year, page=page, deferred=True)
        return

    if cid.startswith("otaku:mood:"):
        if _on_cooldown(ctx, user_id):
            return
        # otaku:mood:<feeling>:<page>
        parts = cid.split(":", 3)
        if len(parts) < 4:
            ctx.interaction.respond(content=S.MOOD_PAGE_MALFORMED, ephemeral=True)
            return
        _, _, feeling, page_s = parts
        if feeling not in MOODS:
            ctx.interaction.respond(content=S.MOOD_PAGE_MALFORMED, ephemeral=True)
            return
        try:
            page = max(1, int(page_s))
        except ValueError:
            ctx.interaction.respond(content=S.MOOD_PAGE_MALFORMED, ephemeral=True)
            return
        ctx.interaction.defer()
        _render_mood(ctx, feeling, page=page, deferred=True)
        return

    if cid.startswith("otaku:reviews:"):
        if _on_cooldown(ctx, user_id):
            return
        # otaku:reviews:<media_id>:<page>
        parts = cid.split(":", 3)
        if len(parts) < 4:
            ctx.interaction.respond(content=S.REVIEWS_PAGE_MALFORMED, ephemeral=True)
            return
        _, _, mid_s, page_s = parts
        try:
            media_id = int(mid_s)
            page = max(1, int(page_s))
        except ValueError:
            ctx.interaction.respond(content=S.REVIEWS_PAGE_MALFORMED, ephemeral=True)
            return
        ctx.interaction.defer()
        _render_reviews(ctx, media_id, page=page, deferred=True, viewer_id=user_id)
        return

    if cid.startswith("otaku:aotw-vote:"):
        _handle_aotw_vote(ctx, event)
        return

    if cid.startswith("otaku:poll-vote:"):
        _handle_poll_vote(ctx, event)
        return


# Register the dispatcher under every dynamic prefix we use. The SDK matches on
# exact custom_id, so we hook the raw interaction_create event and filter ourselves.
@plugin.on_event("interaction_create")
def _route_components(ctx: Context, event: dict) -> None:
    itype = event.get("interaction_type")
    cid = event.get("custom_id") or ""

    # Modal submits (v7.0.0+) route through a separate dispatcher because they
    # don't share the cooldown / defer dance of the component buttons.
    if itype == 5:  # MODAL_SUBMIT
        if cid.startswith("otaku:review-modal:"):
            _handle_review_submit(ctx, event)
        return

    if itype != 3:  # MESSAGE_COMPONENT
        return
    if cid == "otaku:expand":
        return  # handled by @plugin.on_component above
    if (
        cid.startswith("otaku:page:")
        or cid.startswith("otaku:mpage:")
        or cid.startswith("otaku:popchar:")
        or cid.startswith("otaku:trend:")
        or cid.startswith("otaku:similar:")
        or cid.startswith("otaku:list:")
        or cid.startswith("otaku:reset-confirm:")
        or cid.startswith("otaku:reset-cancel:")
        or cid.startswith("otaku:swl:")
        or cid.startswith("otaku:wp-join:")
        or cid.startswith("otaku:premieres:")
        or cid.startswith("otaku:mood:")
        or cid.startswith("otaku:reviews:")
        or cid.startswith("otaku:aotw-vote:")
        or cid.startswith("otaku:poll-vote:")
    ):
        _component_dispatch(ctx, event)


@plugin.on_component("otaku:expand")
def comp_expand(ctx: Context, event: dict) -> None:
    """Select-menu pick → fetch the chosen anime by ID and show the full card."""
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    values = event.get("values") or []
    if not values:
        ctx.interaction.respond(content=S.EXPAND_NO_SELECTION, ephemeral=True)
        return
    try:
        media_id = int(values[0])
    except (TypeError, ValueError):
        ctx.interaction.respond(content=S.EXPAND_INVALID, ephemeral=True)
        return

    ctx.interaction.defer(ephemeral=True)
    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": media_id})
    if data is None or not data.get("Media"):
        _reply_anilist_failure(
            ctx,
            deferred=True,
            fallback=S.EXPAND_FETCH_FAIL,
        )
        return
    media = data["Media"]
    if user_id:
        ctx.kv.set(f"last_anime:user:{user_id}", media.get("id"), ttl_seconds=LAST_ANIME_TTL)

    progress, rating = _get_user_tracking(ctx, user_id, int(media.get("id") or 0))
    embed = _make_anime_embed(media, user_progress=progress, user_rating=rating)
    buttons = [Button("Similar", custom_id=f"otaku:similar:{media.get('id')}", style="primary", emoji="🔁")]
    site_url = media.get("siteUrl")
    if site_url:
        buttons.append(Button("Open on AniList", url=site_url, style="link", emoji="🌐"))
    ctx.interaction.followup(embeds=[embed], components=[ActionRow(*buttons)], ephemeral=True)


# ── v7.0.0 — /review and /reviews (per-anime user reviews) ──────────────────
#
# /review opens a modal pre-filled with the caller's existing review (if any)
# for their cached last_anime. /reviews paginates this server's reviews for a
# given anime (lookup arg accepts a title OR numeric AniList ID; defaults to
# the caller's cached last_anime). One review per user per anime — submitting
# again upserts. The 3-second pre-modal Discord wall clock is why /review
# doesn't accept a title arg: we'd need an AniList lookup before send_modal,
# and that can't reliably finish in time. Users who want to review a specific
# anime do `/anime query: <title>` first.

REVIEW_TITLE_MAX = 100        # Discord short-input max
REVIEW_BODY_MAX = 2000        # Discord paragraph practical max
REVIEWS_PAGE_SIZE = 3         # reviews per /reviews page (each has title + body)


def _get_review(ctx: Context, user_id: str, media_id: int) -> dict | None:
    rows = ctx.sql.query(
        "SELECT title, body, created_at, updated_at FROM otaku_reviews "
        "WHERE user_id = %s AND media_id = %s",
        [user_id, media_id],
    ) or []
    return rows[0] if rows else None


def _upsert_review(
    ctx: Context, user_id: str, media_id: int, *, title: str, body: str
) -> bool:
    """Insert or update the review atomically. Returns True for a new row,
    False if an existing one was updated.

    v10.0.1: collapsed read-then-write into a single INSERT...ON CONFLICT
    DO UPDATE — the prior pattern had a TOCTOU window where two concurrent
    modal submits for the same (user, media) could both see "no existing"
    and race the INSERT, with the loser raising IntegrityError out of
    `_handle_review_submit`. `(xmax = 0)` distinguishes the freshly
    inserted row from the updated one so the success message stays correct.
    """
    rows = ctx.sql.query(
        "INSERT INTO otaku_reviews (user_id, media_id, title, body) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (user_id, media_id) DO UPDATE SET "
        "title = EXCLUDED.title, body = EXCLUDED.body, updated_at = NOW() "
        "RETURNING (xmax = 0) AS inserted",
        [user_id, media_id, title, body],
    ) or [{"inserted": True}]
    return bool((rows[0] or {}).get("inserted", True))


@plugin.on_slash_command("review")
def cmd_review(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return

    # Resolve via cached last_anime only. Discord's 3-second pre-modal wall
    # clock makes an AniList title lookup unsafe before send_modal.
    media_id = _resolve_last_anime_id(ctx, user_id)
    if media_id is None:
        ctx.interaction.respond(content=S.REVIEW_NO_CACHE, ephemeral=True)
        return

    # Cached fetch for the title — the cached path is in-process and instant.
    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": media_id}, cache=True)
    if data is None or not data.get("Media"):
        ctx.interaction.respond(content=S.REVIEW_NO_DATA, ephemeral=True)
        return
    title = _truncate(_format_title(data["Media"]), 40)

    existing = _get_review(ctx, user_id, media_id)
    title_value = (existing or {}).get("title") or ""
    body_value = (existing or {}).get("body") or ""

    ctx.interaction.send_modal(
        title=_truncate(S.REVIEW_MODAL_TITLE.format(title=title), 45),
        custom_id=f"otaku:review-modal:{media_id}",
        fields=[
            TextInput(
                S.REVIEW_FIELD_TITLE,
                "title",
                style="short",
                placeholder=S.REVIEW_PLACEHOLDER_TITLE,
                value=title_value,
                required=True,
                max_length=REVIEW_TITLE_MAX,
            ),
            TextInput(
                S.REVIEW_FIELD_BODY,
                "body",
                style="paragraph",
                placeholder=S.REVIEW_PLACEHOLDER_BODY,
                value=body_value,
                required=True,
                max_length=REVIEW_BODY_MAX,
            ),
        ],
    )


def _handle_review_submit(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    cid = event.get("custom_id") or ""
    parts = cid.split(":", 2)
    if len(parts) < 3:
        ctx.interaction.respond(content=S.REVIEW_SUBMIT_MALFORMED, ephemeral=True)
        return
    try:
        media_id = int(parts[2])
    except ValueError:
        ctx.interaction.respond(content=S.REVIEW_SUBMIT_MALFORMED, ephemeral=True)
        return

    values = event.get("modal_values") or {}
    review_title = (values.get("title") or "").strip()
    review_body = (values.get("body") or "").strip()
    if not review_title or not review_body:
        ctx.interaction.respond(content=S.REVIEW_SUBMIT_EMPTY, ephemeral=True)
        return
    # Cap defensively in case the client sneaks past max_length.
    review_title = review_title[:REVIEW_TITLE_MAX]
    review_body = review_body[:REVIEW_BODY_MAX]

    is_new = _upsert_review(
        ctx, user_id, media_id, title=review_title, body=review_body
    )

    # Resolve the display title (cached — instant on the happy path).
    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": media_id}, cache=True)
    if data and data.get("Media"):
        display_title = _format_title(data["Media"])
    else:
        display_title = f"anime #{media_id}"

    msg = (S.REVIEW_SAVED_NEW if is_new else S.REVIEW_SAVED_EDIT).format(title=display_title)
    ctx.interaction.respond(content=msg, ephemeral=True)


def _select_reviews_page(
    ctx: Context, media_id: int, page: int
) -> tuple[list[dict], int]:
    """Returns (rows_for_page, total_count). Rows sorted updated_at DESC."""
    total_rows = ctx.sql.query(
        "SELECT COUNT(*) AS n FROM otaku_reviews WHERE media_id = %s",
        [media_id],
    ) or [{"n": 0}]
    total = int((total_rows[0] or {}).get("n") or 0)
    if total == 0:
        return [], 0
    offset = (page - 1) * REVIEWS_PAGE_SIZE
    rows = ctx.sql.query(
        "SELECT user_id, title, body, created_at, updated_at "
        "FROM otaku_reviews WHERE media_id = %s "
        "ORDER BY updated_at DESC LIMIT %s OFFSET %s",
        [media_id, REVIEWS_PAGE_SIZE, max(0, offset)],
    ) or []
    return rows, total


def _render_reviews(
    ctx: Context,
    media_id: int,
    page: int,
    *,
    deferred: bool,
    ephemeral_reply: bool = False,
    viewer_id: str = "",
) -> None:
    data = _anilist_query(ctx, QUERY_MEDIA_BY_ID, {"id": media_id}, cache=True)
    if data is None:
        _reply_anilist_failure(ctx, deferred=deferred)
        return
    media = data.get("Media") or {}
    display_title = _format_title(media) if media else f"anime #{media_id}"

    rows, total = _select_reviews_page(ctx, media_id, page)
    if total == 0:
        _reply_error(
            ctx, S.REVIEWS_EMPTY.format(title=display_title), deferred=deferred
        )
        return

    total_pages = max(1, (total + REVIEWS_PAGE_SIZE - 1) // REVIEWS_PAGE_SIZE)
    page = max(1, min(page, total_pages))

    # v9.3.0: per-viewer spoiler preference. Default "hide" wraps detected
    # spoiler markers in ||...||; "show" renders raw text. The author's
    # submitted text stays intact in the DB — only the render layer changes.
    show_unhidden = (_get_pref_spoilers(ctx, viewer_id) == "show") if viewer_id else False

    fields = []
    for r in rows:
        author = r.get("user_id") or "?"
        rev_title = _redact_spoilers(
            _truncate(r.get("title") or "", REVIEW_TITLE_MAX),
            show_unhidden=show_unhidden,
        )
        rev_body = _redact_spoilers(
            _truncate(r.get("body") or "", 1024),  # embed-field cap
            show_unhidden=show_unhidden,
        )
        fields.append({
            "name": f"<@{author}> · {rev_title}",
            "value": rev_body,
            "inline": False,
        })

    embed = {
        "title": S.REVIEWS_HEADER.format(title=display_title),
        "color": ANILIST_COLOR,
        "fields": fields,
        "footer": {
            "text": S.REVIEWS_FOOTER_PAGE.format(page=page, total=total_pages, n=total)
        },
    }
    cover = (media.get("coverImage") or {}).get("large")
    if cover:
        embed["thumbnail"] = {"url": cover}

    prev_id = f"otaku:reviews:{media_id}:{page - 1}" if page > 1 else None
    next_id = f"otaku:reviews:{media_id}:{page + 1}" if page < total_pages else None
    components = [_page_buttons(prev_id, next_id)]

    if deferred:
        ctx.interaction.followup(
            embeds=[embed], components=components, ephemeral=ephemeral_reply
        )
    else:
        ctx.interaction.respond(
            embeds=[embed], components=components, ephemeral=ephemeral_reply
        )


@plugin.on_slash_command("reviews")
def cmd_reviews(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    anime_arg = (opts.get("anime") or "").strip()

    ctx.interaction.defer()

    # Numeric arg → use directly. String arg → AniList search. No arg →
    # cached last_anime fallback.
    if anime_arg.isdigit():
        media_id = int(anime_arg)
    elif anime_arg:
        data = _anilist_query(ctx, QUERY_SEARCH_ONE, {"q": anime_arg.lower()}, cache=True)
        if data is None:
            _reply_anilist_failure(ctx, deferred=True)
            return
        media = data.get("Media")
        if not media:
            _reply_error(
                ctx, S.ANIME_NOT_FOUND.format(query=_truncate(anime_arg, 80)),
                deferred=True,
            )
            return
        media_id = int(media.get("id") or 0)
        if not media_id:
            _reply_error(ctx, S.REVIEWS_USAGE, deferred=True)
            return
    else:
        cached = _resolve_last_anime_id(ctx, user_id)
        if cached is None:
            _reply_error(ctx, S.REVIEWS_USAGE, deferred=True)
            return
        media_id = cached

    _render_reviews(ctx, media_id, page=1, deferred=True, viewer_id=user_id)


# ── v7.2.0 — /poll (generic server polls) ───────────────────────────────────
#
# Free-form question + 2-to-4 options. Unlike /aotw, multiple polls can be
# open at once per server, each addressed by its poll_id (returned at create
# time). Admins create and close; voting is open to everyone. PK on votes
# is (poll_id, user_id) — re-voting UPDATEs.

POLL_MIN_OPTIONS = 2
POLL_MAX_OPTIONS = 4
POLL_OPTION_KEYS = ["a", "b", "c", "d"]
POLL_OPTION_LABELS = ["A", "B", "C", "D"]
POLL_QUESTION_MAX = 200
POLL_OPTION_TEXT_MAX = 100


def _poll_row(ctx: Context, poll_id: int) -> dict | None:
    rows = ctx.sql.query(
        "SELECT poll_id, started_by, question, started_at, ended_at, status "
        "FROM otaku_polls WHERE poll_id = %s",
        [poll_id],
    ) or []
    return rows[0] if rows else None


def _poll_options(ctx: Context, poll_id: int) -> list[dict]:
    return ctx.sql.query(
        "SELECT option_key, text FROM otaku_poll_options "
        "WHERE poll_id = %s ORDER BY option_key ASC",
        [poll_id],
    ) or []


def _poll_vote_counts(ctx: Context, poll_id: int) -> dict[str, int]:
    rows = ctx.sql.query(
        "SELECT option_key, COUNT(*) AS n FROM otaku_poll_votes "
        "WHERE poll_id = %s GROUP BY option_key",
        [poll_id],
    ) or []
    return {str(r["option_key"]): int(r["n"]) for r in rows}


def _poll_existing_vote(ctx: Context, poll_id: int, user_id: str) -> str | None:
    rows = ctx.sql.query(
        "SELECT option_key FROM otaku_poll_votes "
        "WHERE poll_id = %s AND user_id = %s",
        [poll_id, user_id],
    ) or []
    if not rows:
        return None
    key = rows[0].get("option_key")
    return str(key) if key is not None else None


def _poll_render_embed(
    ctx: Context, poll_id: int, *, header_template: str, ended: bool = False
) -> dict | None:
    poll = _poll_row(ctx, poll_id)
    if poll is None:
        return None
    options = _poll_options(ctx, poll_id)
    counts = _poll_vote_counts(ctx, poll_id)
    total = sum(counts.values())
    lines = []
    for opt in options:
        key = opt["option_key"]
        text = opt["text"]
        n = counts.get(key, 0)
        bar = "🟩" * min(10, n) if n else "—"
        suffix = "vote" if n == 1 else "votes"
        lines.append(f"**{key.upper()}**) {text} · {n} {suffix} {bar}")
    footer_template = S.POLL_FOOTER_ENDED if ended else S.POLL_FOOTER_LIVE
    return {
        "title": header_template.format(poll_id=poll_id, question=_truncate(poll["question"], 200)),
        "description": "\n".join(lines) if lines else "*(no options)*",
        "color": ANILIST_COLOR,
        "footer": {"text": footer_template.format(n=total, poll_id=poll_id)},
    }


def _poll_vote_buttons(poll_id: int, options: list[dict]) -> ActionRow:
    buttons = []
    for opt in options[:POLL_MAX_OPTIONS]:
        key = opt["option_key"]
        label_idx = POLL_OPTION_KEYS.index(key) if key in POLL_OPTION_KEYS else 0
        buttons.append(
            Button(
                POLL_OPTION_LABELS[label_idx],
                custom_id=f"otaku:poll-vote:{poll_id}:{key}",
                style="primary",
            )
        )
    return ActionRow(*buttons)


def _subopts(event_or_sub: dict) -> dict:
    """Flatten subcommand options into a dict."""
    opts = event_or_sub.get("options") or []
    if isinstance(opts, dict):
        return opts
    return {o["name"]: o.get("value") for o in opts if isinstance(o, dict)}


def _cmd_poll_create(ctx: Context, user_id: str, sub_opts: dict) -> None:
    if not _caller_is_admin(ctx, user_id):
        ctx.interaction.respond(
            content=S.POLL_NOT_ADMIN.format(action="create"), ephemeral=True
        )
        return
    question = (sub_opts.get("question") or "").strip()
    if not question:
        ctx.interaction.respond(content=S.POLL_QUESTION_REQUIRED, ephemeral=True)
        return
    question = question[:POLL_QUESTION_MAX]

    options: list[tuple[str, str]] = []
    for key in POLL_OPTION_KEYS:
        text = (sub_opts.get(key) or "").strip()
        if text:
            options.append((key, text[:POLL_OPTION_TEXT_MAX]))
    if len(options) < POLL_MIN_OPTIONS:
        ctx.interaction.respond(content=S.POLL_OPTIONS_REQUIRED, ephemeral=True)
        return

    ctx.interaction.defer()

    ctx.sql.execute(
        "INSERT INTO otaku_polls (started_by, question) VALUES (%s, %s)",
        [user_id, question],
    )
    poll_rows = ctx.sql.query(
        "SELECT poll_id FROM otaku_polls "
        "WHERE started_by = %s AND question = %s AND status = 'active' "
        "ORDER BY started_at DESC LIMIT 1",
        [user_id, question],
    ) or []
    if not poll_rows or poll_rows[0].get("poll_id") is None:
        _reply_error(ctx, S.SQL_FAIL, deferred=True)
        return
    poll_id = int(poll_rows[0]["poll_id"])
    # v10.0.3: single multi-row INSERT (was one INSERT per option).
    # v10.0.5: UNNEST(...) syntax replaces the f-string VALUES (...) builder
    # to comply with the SDK's "no f-string SQL" rule. Pure static SQL with
    # three parameters (poll_id + two parallel arrays); PostgreSQL expands
    # the arrays into rows server-side.
    if options:
        ctx.sql.execute(
            "INSERT INTO otaku_poll_options (poll_id, option_key, text) "
            "SELECT %s, k, t FROM UNNEST(%s::TEXT[], %s::TEXT[]) AS u(k, t)",
            [poll_id, [k for k, _ in options], [t for _, t in options]],
        )

    embed = _poll_render_embed(ctx, poll_id, header_template=S.POLL_STATUS_HEADER)
    if embed is None:
        _reply_error(ctx, S.SQL_FAIL, deferred=True)
        return
    option_rows = _poll_options(ctx, poll_id)
    ctx.interaction.followup(
        embeds=[embed], components=[_poll_vote_buttons(poll_id, option_rows)]
    )


def _cmd_poll_status(ctx: Context, sub_opts: dict) -> None:
    ctx.interaction.defer()
    try:
        poll_id = int(sub_opts.get("id") or 0)
    except (TypeError, ValueError):
        _reply_error(ctx, S.POLL_NOT_FOUND.format(poll_id=sub_opts.get("id")), deferred=True)
        return
    poll = _poll_row(ctx, poll_id)
    if poll is None:
        _reply_error(ctx, S.POLL_NOT_FOUND.format(poll_id=poll_id), deferred=True)
        return
    ended = (poll.get("status") != "active")
    embed = _poll_render_embed(
        ctx, poll_id, header_template=S.POLL_STATUS_HEADER, ended=ended
    )
    if embed is None:
        _reply_error(ctx, S.POLL_NOT_FOUND.format(poll_id=poll_id), deferred=True)
        return
    ctx.interaction.followup(embeds=[embed])


def _cmd_poll_end(ctx: Context, user_id: str, sub_opts: dict) -> None:
    if not _caller_is_admin(ctx, user_id):
        ctx.interaction.respond(
            content=S.POLL_NOT_ADMIN.format(action="end"), ephemeral=True
        )
        return
    ctx.interaction.defer(ephemeral=True)
    try:
        poll_id = int(sub_opts.get("id") or 0)
    except (TypeError, ValueError):
        _reply_error(ctx, S.POLL_NOT_FOUND.format(poll_id=sub_opts.get("id")), deferred=True)
        return
    poll = _poll_row(ctx, poll_id)
    if poll is None:
        _reply_error(ctx, S.POLL_NOT_FOUND.format(poll_id=poll_id), deferred=True)
        return
    if poll.get("status") != "active":
        ctx.interaction.followup(
            content=S.POLL_ENDED_OK.format(poll_id=poll_id, n=0), ephemeral=True
        )
        return
    counts = _poll_vote_counts(ctx, poll_id)
    total = sum(counts.values())
    ctx.sql.execute(
        "UPDATE otaku_polls SET status = 'ended', ended_at = NOW() "
        "WHERE poll_id = %s",
        [poll_id],
    )
    ctx.interaction.followup(
        content=S.POLL_ENDED_OK.format(poll_id=poll_id, n=total), ephemeral=True
    )


@plugin.on_slash_command("poll")
def cmd_poll(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = event.get("options") or []
    if not opts or not isinstance(opts, list):
        ctx.interaction.respond(content=S.POLL_USAGE, ephemeral=True)
        return
    sub = opts[0] if isinstance(opts[0], dict) else {}
    subcommand = sub.get("name") or ""
    sub_opts = _subopts(sub)
    if subcommand == "create":
        _cmd_poll_create(ctx, user_id, sub_opts)
    elif subcommand == "status":
        _cmd_poll_status(ctx, sub_opts)
    elif subcommand == "end":
        _cmd_poll_end(ctx, user_id, sub_opts)
    else:
        ctx.interaction.respond(content=S.POLL_USAGE, ephemeral=True)


def _handle_poll_vote(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    cid = event.get("custom_id") or ""
    parts = cid.split(":", 3)
    if len(parts) < 4:
        ctx.interaction.respond(content=S.POLL_VOTE_BUTTON_MALFORMED, ephemeral=True)
        return
    try:
        poll_id = int(parts[2])
    except ValueError:
        ctx.interaction.respond(content=S.POLL_VOTE_BUTTON_MALFORMED, ephemeral=True)
        return
    option_key = parts[3]
    if option_key not in POLL_OPTION_KEYS:
        ctx.interaction.respond(content=S.POLL_VOTE_BUTTON_MALFORMED, ephemeral=True)
        return

    # v10.0.12 — defer before the first SQL round trip (3-second rule). The
    # parse-failure exits above are in-memory and keep instant respond()s;
    # every reply below this line is a followup.
    ctx.interaction.defer(ephemeral=True)
    try:
        _poll_vote_deferred(ctx, poll_id, option_key, user_id)
    except Exception as exc:  # noqa: BLE001 — never strand a deferred interaction
        # v10.0.13 — the user-facing reply comes FIRST and each step has its
        # own guard: with both in one try, a log failure skipped the followup
        # and recreated the eternal-spinner this guard exists to prevent.
        try:
            ctx.interaction.followup(content=S.VOTE_FAILED, ephemeral=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.log(
                f"poll vote failed: {f'{type(exc).__name__}: {exc}'[:300]}",
                level="error", tags=["poll", "vote"],
            )
        except Exception:  # noqa: BLE001
            pass


def _poll_vote_deferred(ctx: Context, poll_id: int, option_key: str, user_id: str) -> None:
    """Post-defer body of _handle_poll_vote (v10.0.12) — every reply is a
    followup; the caller guards exceptions so a failure (e.g. the concurrent
    duplicate-vote INSERT race, or a missing table mid-bootstrap) can't
    strand the deferred interaction in an eternal "thinking…" state."""
    poll = _poll_row(ctx, poll_id)
    if poll is None or poll.get("status") != "active":
        ctx.interaction.followup(
            content=S.POLL_VOTE_CLOSED.format(poll_id=poll_id), ephemeral=True
        )
        return

    options = _poll_options(ctx, poll_id)
    valid_keys = {o["option_key"] for o in options}
    if option_key not in valid_keys:
        ctx.interaction.followup(content=S.POLL_VOTE_BUTTON_MALFORMED, ephemeral=True)
        return
    chosen_text = next((o["text"] for o in options if o["option_key"] == option_key), option_key)
    label = f"{option_key.upper()}) {_truncate(chosen_text, 80)}"

    existing = _poll_existing_vote(ctx, poll_id, user_id)
    if existing == option_key:
        ctx.interaction.followup(
            content=S.POLL_VOTE_NOOP.format(label=label), ephemeral=True
        )
        return
    if existing is None:
        ctx.sql.execute(
            "INSERT INTO otaku_poll_votes (poll_id, user_id, option_key) "
            "VALUES (%s, %s, %s)",
            [poll_id, user_id, option_key],
        )
        msg = S.POLL_VOTE_RECORDED.format(label=label)
    else:
        ctx.sql.execute(
            "UPDATE otaku_poll_votes SET option_key = %s, voted_at = NOW() "
            "WHERE poll_id = %s AND user_id = %s",
            [option_key, poll_id, user_id],
        )
        msg = S.POLL_VOTE_CHANGED.format(label=label)
    ctx.interaction.followup(content=msg, ephemeral=True)


# ── v7.1.0 — /aotw (anime of the week voting) ────────────────────────────────
#
# Candidates come from otaku_server_watchlist (v3.0.0) — top
# AOTW_CANDIDATE_LIMIT (5) entries by recency. Discord allows 5 buttons in a
# row, which conveniently caps the candidate count at 5 too. One active poll
# per server is enforced in /aotw start; concurrent polls would make button
# routing ambiguous.

AOTW_CANDIDATE_LIMIT = 5
AOTW_CANDIDATE_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def _aotw_active_poll_id(ctx: Context) -> int | None:
    rows = ctx.sql.query(
        "SELECT poll_id FROM otaku_aotw_polls WHERE status = 'active' "
        "ORDER BY started_at DESC LIMIT 1"
    ) or []
    if not rows:
        return None
    pid = rows[0].get("poll_id")
    return int(pid) if pid is not None else None


def _aotw_poll_row(ctx: Context, poll_id: int) -> dict | None:
    rows = ctx.sql.query(
        "SELECT poll_id, started_by, started_at, ended_at, winner_id, status "
        "FROM otaku_aotw_polls WHERE poll_id = %s",
        [poll_id],
    ) or []
    return rows[0] if rows else None


def _aotw_candidates(ctx: Context, poll_id: int) -> list[int]:
    rows = ctx.sql.query(
        "SELECT media_id FROM otaku_aotw_candidates WHERE poll_id = %s "
        "ORDER BY media_id ASC",
        [poll_id],
    ) or []
    return [int(r["media_id"]) for r in rows if r.get("media_id") is not None]


def _aotw_vote_counts(ctx: Context, poll_id: int) -> dict[int, int]:
    rows = ctx.sql.query(
        "SELECT media_id, COUNT(*) AS n FROM otaku_aotw_votes "
        "WHERE poll_id = %s GROUP BY media_id",
        [poll_id],
    ) or []
    return {int(r["media_id"]): int(r["n"]) for r in rows}


def _aotw_existing_vote(ctx: Context, poll_id: int, user_id: str) -> int | None:
    rows = ctx.sql.query(
        "SELECT media_id FROM otaku_aotw_votes "
        "WHERE poll_id = %s AND user_id = %s",
        [poll_id, user_id],
    ) or []
    if not rows:
        return None
    mid = rows[0].get("media_id")
    return int(mid) if mid is not None else None


def _aotw_render_embed(
    ctx: Context,
    poll_id: int,
    candidates: list[int],
    media_by_id: dict[int, dict],
    *,
    header: str,
) -> dict:
    counts = _aotw_vote_counts(ctx, poll_id)
    total_votes = sum(counts.values())
    lines = []
    for idx, mid in enumerate(candidates[:AOTW_CANDIDATE_LIMIT]):
        m = media_by_id.get(mid) or {}
        title = _format_title(m) if m else f"anime #{mid}"
        url = m.get("siteUrl") or ""
        anchor = f"[{title}]({url})" if url else title
        n = counts.get(mid, 0)
        suffix = "vote" if n == 1 else "votes"
        lines.append(f"{AOTW_CANDIDATE_NUMBERS[idx]} {anchor} — **{n}** {suffix}")
    return {
        "title": header,
        "description": "\n".join(lines) if lines else "*(no candidates)*",
        "color": ANILIST_COLOR,
        "footer": {
            "text": S.AOTW_FOOTER_VOTES.format(n=total_votes, poll_id=poll_id)
        },
    }


def _aotw_vote_buttons(poll_id: int, candidates: list[int]) -> ActionRow:
    buttons = []
    for idx, mid in enumerate(candidates[:AOTW_CANDIDATE_LIMIT]):
        buttons.append(
            Button(
                AOTW_CANDIDATE_NUMBERS[idx],
                custom_id=f"otaku:aotw-vote:{poll_id}:{mid}",
                style="primary",
            )
        )
    return ActionRow(*buttons)


def _aotw_resolve_media(ctx: Context, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    data = _anilist_query(ctx, QUERY_MEDIA_BATCH, {"ids": sorted(ids)}, cache=True)
    if data is None:
        return {}
    out: dict[int, dict] = {}
    for m in ((data.get("Page") or {}).get("media") or []):
        mid = m.get("id")
        if isinstance(mid, int):
            out[mid] = m
    return out


def _cmd_aotw_start(ctx: Context, user_id: str) -> None:
    if not _caller_is_admin(ctx, user_id):
        ctx.interaction.respond(
            content=S.AOTW_NOT_ADMIN.format(action="start"), ephemeral=True
        )
        return
    ctx.interaction.defer()
    active = _aotw_active_poll_id(ctx)
    if active is not None:
        _reply_error(
            ctx, S.AOTW_ALREADY_ACTIVE.format(poll_id=active), deferred=True
        )
        return

    rows = ctx.sql.query(
        "SELECT media_id FROM otaku_server_watchlist "
        "ORDER BY added_at DESC LIMIT %s",
        [AOTW_CANDIDATE_LIMIT],
    ) or []
    candidate_ids = [int(r["media_id"]) for r in rows if r.get("media_id") is not None]
    if len(candidate_ids) < 2:
        _reply_error(ctx, S.AOTW_NO_CANDIDATES, deferred=True)
        return

    ctx.sql.execute(
        "INSERT INTO otaku_aotw_polls (started_by) VALUES (%s)", [user_id]
    )
    poll_rows = ctx.sql.query(
        "SELECT poll_id FROM otaku_aotw_polls "
        "WHERE started_by = %s AND status = 'active' "
        "ORDER BY started_at DESC LIMIT 1",
        [user_id],
    ) or []
    if not poll_rows or poll_rows[0].get("poll_id") is None:
        _reply_error(ctx, S.SQL_FAIL, deferred=True)
        return
    poll_id = int(poll_rows[0]["poll_id"])
    # v10.0.3: single multi-row INSERT (was one INSERT per candidate).
    # v10.0.5: UNNEST(...) syntax replaces the f-string VALUES (...) builder
    # to comply with the SDK's "no f-string SQL" rule. Pure static SQL with
    # two parameters (poll_id + media_id array).
    if candidate_ids:
        ctx.sql.execute(
            "INSERT INTO otaku_aotw_candidates (poll_id, media_id) "
            "SELECT %s, mid FROM UNNEST(%s::INT[]) AS u(mid)",
            [poll_id, list(candidate_ids)],
        )

    media_by_id = _aotw_resolve_media(ctx, candidate_ids)
    embed = _aotw_render_embed(
        ctx, poll_id, candidate_ids, media_by_id, header=S.AOTW_STARTED
    )
    ctx.interaction.followup(
        embeds=[embed],
        components=[_aotw_vote_buttons(poll_id, candidate_ids)],
    )


def _cmd_aotw_status(ctx: Context) -> None:
    ctx.interaction.defer()
    poll_id = _aotw_active_poll_id(ctx)
    if poll_id is None:
        _reply_error(ctx, S.AOTW_NONE_ACTIVE, deferred=True)
        return
    candidates = _aotw_candidates(ctx, poll_id)
    media_by_id = _aotw_resolve_media(ctx, candidates)
    embed = _aotw_render_embed(
        ctx, poll_id, candidates, media_by_id, header=S.AOTW_STATUS_HEADER
    )
    # /aotw status doesn't re-render the vote buttons — the original /aotw
    # start embed already has them and stays interactive.
    ctx.interaction.followup(embeds=[embed])


def _cmd_aotw_end(ctx: Context, user_id: str, channel_id: str) -> None:
    if not _caller_is_admin(ctx, user_id):
        ctx.interaction.respond(
            content=S.AOTW_NOT_ADMIN.format(action="end"), ephemeral=True
        )
        return
    ctx.interaction.defer(ephemeral=True)
    poll_id = _aotw_active_poll_id(ctx)
    if poll_id is None:
        _reply_error(ctx, S.AOTW_NONE_ACTIVE, deferred=True)
        return

    counts = _aotw_vote_counts(ctx, poll_id)
    if not counts:
        ctx.sql.execute(
            "UPDATE otaku_aotw_polls SET status = 'ended', ended_at = NOW() "
            "WHERE poll_id = %s",
            [poll_id],
        )
        ctx.interaction.followup(
            content=S.AOTW_ENDED_NO_VOTES.format(poll_id=poll_id), ephemeral=True
        )
        return

    # Highest count wins; tie-break by lowest media_id (deterministic).
    winner_id = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    votes = counts[winner_id]
    ctx.sql.execute(
        "UPDATE otaku_aotw_polls SET status = 'ended', ended_at = NOW(), "
        "winner_id = %s WHERE poll_id = %s",
        [winner_id, poll_id],
    )

    media_by_id = _aotw_resolve_media(ctx, [winner_id])
    media = media_by_id.get(winner_id) or {}
    title = _format_title(media) if media else f"anime #{winner_id}"

    target_channel = _resolve_announcement_channel(ctx) or channel_id
    announcement = S.AOTW_ENDED_WINNER.format(
        title=title, poll_id=poll_id, votes=votes
    )
    embed = {
        "title": "🏆 Anime of the Week",
        "description": announcement,
        "color": ANILIST_COLOR,
    }
    cover = (media.get("coverImage") or {}).get("large") if media else None
    if cover:
        embed["thumbnail"] = {"url": cover}
    site_url = media.get("siteUrl") if media else None
    if site_url:
        embed["url"] = site_url

    posted = False
    if target_channel:
        try:
            ctx.discord.send_message(channel_id=target_channel, embeds=[embed])
            posted = True
        except Exception:  # noqa: BLE001 — fall through to ephemeral confirm
            posted = False

    if posted:
        ctx.interaction.followup(content=announcement, ephemeral=True)
    else:
        ctx.interaction.followup(
            content=S.AOTW_ANNOUNCE_FAIL.format(poll_id=poll_id), ephemeral=True
        )


@plugin.on_slash_command("aotw")
def cmd_aotw(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = event.get("options") or []
    if not opts or not isinstance(opts, list):
        ctx.interaction.respond(content=S.AOTW_USAGE, ephemeral=True)
        return
    sub = opts[0] if isinstance(opts[0], dict) else {}
    subcommand = sub.get("name") or ""
    if subcommand == "start":
        _cmd_aotw_start(ctx, user_id)
    elif subcommand == "status":
        _cmd_aotw_status(ctx)
    elif subcommand == "end":
        _cmd_aotw_end(ctx, user_id, channel_id=event.get("channel_id") or "")
    else:
        ctx.interaction.respond(content=S.AOTW_USAGE, ephemeral=True)


def _handle_aotw_vote(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    cid = event.get("custom_id") or ""
    parts = cid.split(":", 3)
    if len(parts) < 4:
        ctx.interaction.respond(content=S.AOTW_VOTE_BUTTON_MALFORMED, ephemeral=True)
        return
    try:
        poll_id = int(parts[2])
        media_id = int(parts[3])
    except ValueError:
        ctx.interaction.respond(content=S.AOTW_VOTE_BUTTON_MALFORMED, ephemeral=True)
        return

    # v10.0.12 — defer before the slow work (3-second rule). This handler is
    # the worst offender of the class: an AniList title resolve (with up to
    # 2.0s of retry backoff) plus four SQL round trips preceded its only
    # respond, so the vote could record while the user saw "interaction
    # failed". The parse-failure exits above stay instant respond()s; every
    # reply below this line is a followup.
    ctx.interaction.defer(ephemeral=True)
    try:
        _aotw_vote_deferred(ctx, poll_id, media_id, user_id)
    except Exception as exc:  # noqa: BLE001 — never strand a deferred interaction
        # v10.0.13 — reply first, then log; independent guards (see
        # _handle_poll_vote).
        try:
            ctx.interaction.followup(content=S.VOTE_FAILED, ephemeral=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.log(
                f"aotw vote failed: {f'{type(exc).__name__}: {exc}'[:300]}",
                level="error", tags=["aotw", "vote"],
            )
        except Exception:  # noqa: BLE001
            pass


def _aotw_vote_deferred(ctx: Context, poll_id: int, media_id: int, user_id: str) -> None:
    """Post-defer body of _handle_aotw_vote (v10.0.12) — every reply is a
    followup; the caller guards exceptions so a failure can't strand the
    deferred interaction in an eternal "thinking…" state."""
    poll = _aotw_poll_row(ctx, poll_id)
    if poll is None or poll.get("status") != "active":
        ctx.interaction.followup(content=S.AOTW_VOTE_POLL_CLOSED, ephemeral=True)
        return

    candidates = _aotw_candidates(ctx, poll_id)
    if media_id not in candidates:
        ctx.interaction.followup(content=S.AOTW_VOTE_BUTTON_MALFORMED, ephemeral=True)
        return

    existing = _aotw_existing_vote(ctx, poll_id, user_id)
    media_by_id = _aotw_resolve_media(ctx, [media_id])
    media = media_by_id.get(media_id) or {}
    title = _format_title(media) if media else f"anime #{media_id}"

    if existing == media_id:
        ctx.interaction.followup(
            content=S.AOTW_VOTE_NOOP.format(title=title), ephemeral=True
        )
        return
    if existing is None:
        ctx.sql.execute(
            "INSERT INTO otaku_aotw_votes (poll_id, user_id, media_id) "
            "VALUES (%s, %s, %s)",
            [poll_id, user_id, media_id],
        )
        msg = S.AOTW_VOTE_RECORDED.format(title=title)
    else:
        ctx.sql.execute(
            "UPDATE otaku_aotw_votes SET media_id = %s, voted_at = NOW() "
            "WHERE poll_id = %s AND user_id = %s",
            [media_id, poll_id, user_id],
        )
        msg = S.AOTW_VOTE_CHANGED.format(title=title)
    ctx.interaction.followup(content=msg, ephemeral=True)


# ── v6.2.0 — /genre-trends (genre-trend recommendations) ────────────────────
#
# Bridges discovery and personalization: figure out the user's top tracked
# genres, then pull AniList's currently-trending anime filtered to those
# genres. Anime the user already tracks (any status) are filtered out so
# the surface stays "what's new for me." Uses the same 50-anime sample
# heuristic as /stats's _top_genre_for_user so we never pay for a full
# tracker scan.

GENRE_TRENDS_TOP_N = 3       # how many of the user's genres to filter by
GENRE_TRENDS_FETCH = 15      # AniList page size — fetch extra so post-filter still has ≥5
GENRE_TRENDS_RESULT_LIMIT = 5


def _user_top_genres(ctx: Context, user_id: str, limit: int) -> list[str]:
    """Top N genres the user has tracked, by occurrence over the most-recent
    STATS_TOP_GENRE_SAMPLE anime. Empty list if the tracker is empty or
    AniList can't be reached. Tied counts break alphabetically for stability."""
    rows = ctx.sql.query(
        "SELECT media_id FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' "
        "ORDER BY added_at DESC LIMIT %s",
        [user_id, STATS_TOP_GENRE_SAMPLE],
    ) or []
    ids = [int(r["media_id"]) for r in rows if r.get("media_id") is not None]
    if not ids:
        return []
    data = _anilist_query(ctx, QUERY_MEDIA_BATCH, {"ids": sorted(ids)}, cache=True)
    if data is None:
        return []
    media_list = ((data.get("Page") or {}).get("media")) or []
    counts: dict[str, int] = {}
    for m in media_list:
        for g in (m.get("genres") or []):
            counts[g] = counts.get(g, 0) + 1
    if not counts:
        return []
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [g for g, _ in ordered[:limit]]


@plugin.on_slash_command("genre-trends")
def cmd_genre_trends(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    ctx.interaction.defer(ephemeral=True)

    # Empty-tracker short-circuit before touching AniList.
    tracked_rows = ctx.sql.query(
        "SELECT COUNT(*) AS n FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime'",
        [user_id],
    ) or [{"n": 0}]
    if int((tracked_rows[0] or {}).get("n") or 0) == 0:
        _reply_error(ctx, S.GENRE_TRENDS_EMPTY, deferred=True)
        return

    top_genres = _user_top_genres(ctx, user_id, GENRE_TRENDS_TOP_N)
    if not top_genres:
        _reply_error(ctx, S.GENRE_TRENDS_NO_GENRES, deferred=True)
        return

    data = _anilist_query(
        ctx,
        QUERY_GENRE_TRENDS,
        {"genres": top_genres, "perPage": GENRE_TRENDS_FETCH},
        cache=True,
    )
    if data is None:
        _reply_anilist_failure(ctx, deferred=True)
        return
    media_list = ((data.get("Page") or {}).get("media")) or []

    tracked = _recommend_tracked_ids(ctx, user_id)
    fresh = [m for m in media_list if m.get("id") not in tracked][:GENRE_TRENDS_RESULT_LIMIT]
    if not fresh:
        _reply_error(
            ctx,
            S.GENRE_TRENDS_NO_FRESH.format(genres=", ".join(top_genres)),
            deferred=True,
        )
        return

    header = S.GENRE_TRENDS_HEADER.format(genres=", ".join(top_genres))
    embed = _make_list_embed(fresh, header, page=1, has_next=False)
    embed["footer"] = {"text": S.GENRE_TRENDS_FOOTER.format(n=len(top_genres))}
    components = []
    select_row = _make_select_row(fresh)
    if select_row is not None:
        components.append(select_row)
    ctx.interaction.followup(
        embeds=[embed], components=components or None, ephemeral=True
    )


# ── v6.1.0 — /mood (mood-based suggestions) ─────────────────────────────────
#
# Each mood is a curated AniList genre/tag blend. The runtime allowlist
# rejects sibling .json / .py modules (see v1.4 strings deviation), so the
# mapping lives inline. Tags enrich some moods; if AniList returns no
# results for the with-tags query we transparently fall back to genres-only
# so a fragile tag never strands the user.

MOODS: dict[str, dict] = {
    "uplifting":   {"label": "✨ Uplifting",   "genres": ["Comedy", "Slice of Life"],         "tags": ["Heartwarming"]},
    "tense":       {"label": "😰 Tense",        "genres": ["Thriller", "Psychological"],      "tags": []},
    "cathartic":   {"label": "😭 Cathartic",    "genres": ["Drama"],                          "tags": ["Tragedy"]},
    "chill":       {"label": "🍵 Chill",        "genres": ["Slice of Life"],                  "tags": ["Iyashikei"]},
    "epic":        {"label": "⚔️ Epic",         "genres": ["Action", "Adventure", "Fantasy"], "tags": []},
    "nostalgic":   {"label": "🌅 Nostalgic",    "genres": ["Romance", "Slice of Life"], "tags": ["Coming of Age"]},
    "dark":        {"label": "🌑 Dark",         "genres": ["Horror", "Psychological"],        "tags": ["Gore"]},
    "funny":       {"label": "😂 Funny",        "genres": ["Comedy"],                         "tags": ["Parody"]},
    "romantic":    {"label": "💖 Romantic",     "genres": ["Romance"],                        "tags": []},
    "adventurous": {"label": "🧭 Adventurous",  "genres": ["Adventure"],                      "tags": []},
}


def _search_by_genre_tag_blend(
    ctx: Context, genres: list[str], tags: list[str], page: int
) -> tuple[list[dict] | None, bool]:
    """Shared genre+tag search used by /mood (v6.1) and /find (v9.0).

    Returns (media_list, has_next). media_list=None on AniList failure.

    If tags is non-empty, try the tags+genres query first. If it returns
    empty (the tag might not exist on AniList, or nothing matches this
    season), retry with genres only so we always have *some*
    recommendations to show — fragile or missing tags never strand the
    user. Extracted from _mood_query in v9.0 to back /find as well.
    """
    if tags:
        # v10.0.1: cache every page (see _render_discover comment).
        data = _anilist_query(
            ctx,
            QUERY_MOOD_WITH_TAGS,
            {"genres": genres, "tags": tags, "page": page, "perPage": PER_PAGE},
            cache=True,
        )
        if data is not None:
            page_obj = (data.get("Page") or {})
            media_list = page_obj.get("media") or []
            if media_list:
                has_next = bool((page_obj.get("pageInfo") or {}).get("hasNextPage"))
                return media_list, has_next
        # Empty with tags → fall through to genre-only.

    # v10.0.1: cache every page (see _render_discover comment).
    data = _anilist_query(
        ctx,
        QUERY_MOOD_GENRE_ONLY,
        {"genres": genres, "page": page, "perPage": PER_PAGE},
        cache=True,
    )
    if data is None:
        return None, False
    page_obj = (data.get("Page") or {})
    media_list = page_obj.get("media") or []
    has_next = bool((page_obj.get("pageInfo") or {}).get("hasNextPage"))
    return media_list, has_next


# ── v9.0.0 — /find (natural-language search) ───────────────────────────────
#
# Maps a free-form English description to a blend of AniList genres and tags
# via an inline phrase table. Per the v1.4 deviation (runtime allowlist
# rejects sibling .json files), the phrase table lives inline like MOODS.
# This is a purely lexical mapping; no external LLM call. v9.2 will optionally
# bolt an LLM proxy on top IF the platform exposes one (per ROADMAP).
#
# Each entry maps a list of trigger substrings to a (genres, tags) blend.
# Triggers are matched case-insensitively as whole-word substrings of the
# tokenised input — so "slow romance" matches both "slow" and "romance"
# entries, unioning their genres + tags.
#
# Entries draw from AniList's canonical genre vocabulary and the most-
# stable AniList tags. New entries should prefer genres over tags when
# the distinction is fuzzy (tags can disappear or drift; genres are
# rock-stable across the AniList API history).

FIND_PHRASES: list[dict] = [
    # Pacing / tone
    {"triggers": ["slow", "leisurely", "calm", "chill", "relaxing"],
     "genres": ["Slice of Life"], "tags": ["Iyashikei"]},
    {"triggers": ["dark", "gritty", "bleak"],
     "genres": ["Horror", "Psychological"], "tags": []},
    {"triggers": ["tense", "thriller", "suspense", "suspenseful"],
     "genres": ["Thriller", "Psychological"], "tags": []},
    {"triggers": ["epic", "grand", "huge", "massive", "sweeping"],
     "genres": ["Action", "Adventure", "Fantasy"], "tags": []},
    {"triggers": ["cathartic", "tearjerker", "sad", "tragic", "tragedy"],
     "genres": ["Drama"], "tags": ["Tragedy"]},
    {"triggers": ["uplifting", "heartwarming", "wholesome", "feel-good"],
     "genres": ["Slice of Life", "Comedy"], "tags": ["Heartwarming"]},
    {"triggers": ["funny", "hilarious", "comedy", "comedic", "humor", "humour"],
     "genres": ["Comedy"], "tags": []},
    {"triggers": ["nostalgic", "old", "classic", "retro"],
     "genres": ["Romance", "Slice of Life"], "tags": ["Coming of Age"]},
    # Setting
    {"triggers": ["school", "high school", "highschool", "classroom", "student"],
     "genres": [], "tags": ["School"]},
    {"triggers": ["space", "cosmic", "interstellar", "galaxy"],
     "genres": ["Sci-Fi"], "tags": ["Space"]},
    {"triggers": ["isekai", "transported", "another world", "reborn"],
     "genres": ["Fantasy"], "tags": ["Isekai"]},
    {"triggers": ["mecha", "robot", "robots", "giant robots"],
     "genres": ["Mecha", "Sci-Fi"], "tags": []},
    {"triggers": ["cyberpunk", "dystopian", "dystopia"],
     "genres": ["Sci-Fi"], "tags": ["Cyberpunk"]},
    {"triggers": ["historical", "edo", "feudal", "samurai"],
     "genres": ["Action"], "tags": ["Historical"]},
    {"triggers": ["post-apocalyptic", "apocalypse", "wasteland"],
     "genres": ["Sci-Fi"], "tags": ["Post-Apocalyptic"]},
    {"triggers": ["urban", "city", "modern"],
     "genres": [], "tags": ["Urban Fantasy"]},
    # Theme / content
    {"triggers": ["romance", "romantic", "love", "rom-com"],
     "genres": ["Romance"], "tags": []},
    {"triggers": ["magic", "magical", "wizard", "sorcery", "spells"],
     "genres": ["Fantasy"], "tags": ["Magic"]},
    {"triggers": ["supernatural", "ghost", "yokai", "spirit", "haunted"],
     "genres": ["Supernatural"], "tags": []},
    {"triggers": ["mystery", "detective", "whodunit"],
     "genres": ["Mystery"], "tags": []},
    {"triggers": ["sports", "athletic", "tournament"],
     "genres": ["Sports"], "tags": []},
    {"triggers": ["music", "musical", "band", "concert"],
     "genres": ["Music"], "tags": []},
    {"triggers": ["food", "cooking", "chef", "restaurant"],
     "genres": [], "tags": ["Food"]},
    {"triggers": ["war", "military", "soldier", "battle"],
     "genres": ["Action"], "tags": ["War"]},
    {"triggers": ["psychological", "mind-bending", "mindbending"],
     "genres": ["Psychological"], "tags": []},
    # Audience / archetype
    {"triggers": ["female", "girl", "female protagonist", "heroine"],
     "genres": [], "tags": ["Female Protagonist"]},
    {"triggers": ["male", "male protagonist", "hero"],
     "genres": [], "tags": ["Male Protagonist"]},
    {"triggers": ["coming of age", "coming-of-age", "growing up"],
     "genres": [], "tags": ["Coming of Age"]},
    # Genre catchalls (last so more-specific phrases match first if both present)
    {"triggers": ["action"], "genres": ["Action"], "tags": []},
    {"triggers": ["adventure"], "genres": ["Adventure"], "tags": []},
    {"triggers": ["fantasy"], "genres": ["Fantasy"], "tags": []},
    {"triggers": ["horror", "scary", "creepy"],
     "genres": ["Horror"], "tags": []},
    {"triggers": ["drama", "dramatic"], "genres": ["Drama"], "tags": []},
    {"triggers": ["sci-fi", "scifi", "science fiction"],
     "genres": ["Sci-Fi"], "tags": []},
]


def _match_find_phrases(text: str) -> tuple[set[str], set[str]]:
    """Tokenise + lowercase + match `text` against FIND_PHRASES.

    Returns (genres, tags). Both sets are unions over every matched entry.
    A trigger matches if its full string appears as a substring of the
    lowercased input — so "high school" matches "high school" and "junior
    high school" but not "schooled".
    """
    normalised = " " + text.lower().strip() + " "
    # Light punctuation strip; preserve hyphens (some triggers use them).
    for ch in ".,!?\"'():;":
        normalised = normalised.replace(ch, " ")
    # Collapse runs of whitespace so multi-word triggers match cleanly.
    while "  " in normalised:
        normalised = normalised.replace("  ", " ")

    genres: set[str] = set()
    tags: set[str] = set()
    for entry in FIND_PHRASES:
        for trigger in entry["triggers"]:
            # Use " <trigger> " match against the padded normalised string
            # so triggers don't accidentally match inside a longer word
            # (e.g. "art" → "heart").
            if f" {trigger.lower()} " in normalised:
                genres.update(entry.get("genres") or [])
                tags.update(entry.get("tags") or [])
                break  # one match per entry is enough
    return genres, tags


def _render_find(
    ctx: Context,
    query: str,
    genres: list[str],
    tags: list[str],
    page: int,
    *,
    deferred: bool,
) -> None:
    media_list, has_next = _search_by_genre_tag_blend(ctx, genres, tags, page)
    if media_list is None:
        _reply_anilist_failure(ctx, deferred=deferred)
        return

    # Build the "decoded as" footer up-front so users see what we matched.
    if genres and tags:
        blend = f"genres: {', '.join(genres)} · tags: {', '.join(tags)}"
    elif genres:
        blend = f"genres: {', '.join(genres)}"
    else:
        blend = f"tags: {', '.join(tags)}"

    if not media_list:
        _reply_error(ctx, S.FIND_NO_RESULTS.format(blend=blend), deferred=deferred)
        return

    header = S.FIND_HEADER.format(query=_truncate(query, 80))
    embed = _make_list_embed(media_list, header, page=page, has_next=has_next)
    embed["footer"] = {"text": S.FIND_FOOTER.format(blend=blend)}

    # Pagination uses a compact custom_id: otaku:find:<page>. The genres+tags
    # blend isn't re-encoded — page 2 re-runs _match_find_phrases against the
    # same description, which is stable per FIND_PHRASES. To support multi-
    # turn paging without re-tokenisation, we'd need to encode the blend
    # OR persist it in KV; the simpler choice is to cap /find at one page
    # and link users to /discover for deeper browsing. v9.0 ships single-
    # page; pagination can come in v9.0.x if user-demand surfaces.
    components = []
    select_row = _make_select_row(media_list)
    if select_row is not None:
        components.append(select_row)

    if deferred:
        ctx.interaction.followup(embeds=[embed], components=components or None)
    else:
        ctx.interaction.respond(embeds=[embed], components=components or None)


@plugin.on_slash_command("find")
def cmd_find(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    query = (opts.get("description") or "").strip()
    if not query:
        ctx.interaction.respond(content=S.FIND_USAGE, ephemeral=True)
        return

    genres_set, tags_set = _match_find_phrases(query)
    if not genres_set and not tags_set:
        ctx.interaction.respond(
            content=S.FIND_NO_MATCH.format(query=_truncate(query, 80)),
            ephemeral=True,
        )
        return

    # Sort for deterministic ordering in the footer + cache key.
    genres = sorted(genres_set)
    tags = sorted(tags_set)

    ctx.interaction.defer()
    _render_find(ctx, query, genres, tags, page=1, deferred=True)


# ── v9.3.0 — /preferences ───────────────────────────────────────────────────


@plugin.on_slash_command("preferences")
def cmd_preferences(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    new_lang = (opts.get("language") or "").strip().lower() or None
    new_spoilers = (opts.get("spoilers") or "").strip().lower() or None

    changed = False
    if new_lang and new_lang in PREF_LANGUAGE_CHOICES and user_id:
        ctx.kv.set(f"{PREF_LANGUAGE_KV}:{user_id}", new_lang)
        changed = True
    if new_spoilers and new_spoilers in PREF_SPOILERS_CHOICES and user_id:
        ctx.kv.set(f"{PREF_SPOILERS_KV}:{user_id}", new_spoilers)
        changed = True

    # Read back the (possibly just-updated) current state.
    cur_lang = _get_pref_language(ctx, user_id)
    cur_spoilers = _get_pref_spoilers(ctx, user_id)

    lang_display = cur_lang or S.PREFERENCES_LANG_NOT_SET
    description_lines = [
        S.PREFERENCES_LANG_LINE.format(lang=lang_display),
        S.PREFERENCES_SPOILERS_LINE.format(state=cur_spoilers),
    ]
    if not changed:
        description_lines.insert(0, S.PREFERENCES_NO_CHANGES + "\n")
    else:
        description_lines.insert(0, S.PREFERENCES_UPDATED + "\n")

    # If language is set, show the placeholder note (translation not active yet).
    if cur_lang:
        description_lines.append("\n" + S.PREFERENCES_LANG_NOTE)

    embed = {
        "title": S.PREFERENCES_HEADER,
        "description": "\n".join(description_lines),
        "color": ANILIST_COLOR,
    }
    ctx.interaction.respond(embeds=[embed], ephemeral=True)


# ── v10.0.0 — /achievements ─────────────────────────────────────────────────
#
# Achievement detection runs lazily — only when the user runs /achievements.
# Each predicate is a pure SQL/KV read; if a predicate returns True and the
# user doesn't yet have the row in otaku_achievements, we insert it and
# surface it as "newly earned this check." This keeps the per-handler write
# path zero-cost (no detection overhead on /rate, /watch, /progress, etc.)
# while still giving users a satisfying "you just earned X" surface when
# they check.
#
# Each entry in ACHIEVEMENTS has:
#   key:         immutable stable identifier (stored in DB; never rename)
#   name:        user-visible display name
#   description: one-line flavour text shown under the name
#   check:       (ctx, user_id) -> bool — does the user qualify NOW?
#   progress:    (ctx, user_id) -> str | None — short progress hint for the
#                in-progress section (e.g. "12/50 completed"). None = no hint.
#
# New achievements get APPENDED to ACHIEVEMENTS over time. Order shouldn't
# matter for correctness; it does affect display order.


# v10.0.2 — per-call aggregate cache, thread-local. The SDK runs up to
# MMO_SDK_DISPATCH_THREADS (default 4) dispatcher threads per worker, so the
# v10.0.1 module-global dict could leak user A's stats into user B's
# predicates if their /achievements calls overlapped. `threading.local()`
# scopes the cache to the dispatcher thread that opened it, eliminating the
# cross-user leak while preserving the same reentrant-scope semantics.
_ach_stats_tls = threading.local()


def _ach_stats_current() -> dict | None:
    """Read the current thread's cached achievement stats, or None if no
    scope is active on this thread. Exposed for tests + ad-hoc readers."""
    return getattr(_ach_stats_tls, "current", None)


def _ach_stats_set(stats: dict | None) -> None:
    """Set the current thread's cached achievement stats. None clears."""
    _ach_stats_tls.current = stats

# Maps the `where_extra` argument used by predicates onto the column name in
# the cached aggregate row. New `where_extra` values fall through to the SQL
# path automatically — the dispatch table is the only thing to update if a
# future predicate adds a new aggregate.
_ACH_STATS_KEYS: dict[str, str] = {
    "": "total",
    "AND is_favorite = TRUE": "favorites",
    "AND status = 'completed'": "completed",
    "AND rating IS NOT NULL": "rated",
}

# v10.0.5 — static SQL per `where_extra` value, dispatched by key. Replaces
# the prior `f"... WHERE ... 'anime'{extra}"` f-string composition (which
# the SDK upload reviewer rejects, even though `extra` was de-facto a
# constant from a closed dispatch table). A future predicate that needs a
# new aggregate must add a row here AND a column in `_ach_load_stats`.
_ACH_COUNT_SQL: dict[str, str] = {
    "": (
        "SELECT COUNT(*) AS n FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime'"
    ),
    "AND is_favorite = TRUE": (
        "SELECT COUNT(*) AS n FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' AND is_favorite = TRUE"
    ),
    "AND status = 'completed'": (
        "SELECT COUNT(*) AS n FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' AND status = 'completed'"
    ),
    "AND rating IS NOT NULL": (
        "SELECT COUNT(*) AS n FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' AND rating IS NOT NULL"
    ),
}


def _ach_load_stats(ctx: Context, user_id: str) -> dict:
    """Single-trip aggregate fetch for every count the predicates need.

    v10.0.1: collapses what used to be up to 16 sequential COUNT queries
    per `/achievements` call (10 checks + 6 progress) into 2 SQL trips.
    FILTER aggregates over `otaku_user_media` get all four media counts in
    one scan; a second query takes reviews + subscriptions in one row.
    """
    if not user_id:
        return {"total": 0, "favorites": 0, "completed": 0, "rated": 0,
                "reviews": 0, "subs": 0}
    media_rows = ctx.sql.query(
        "SELECT "
        "  COUNT(*) AS total, "
        "  COUNT(*) FILTER (WHERE is_favorite = TRUE) AS favorites, "
        "  COUNT(*) FILTER (WHERE status = 'completed') AS completed, "
        "  COUNT(*) FILTER (WHERE rating IS NOT NULL) AS rated "
        "FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime'",
        [user_id],
    ) or [{}]
    # v10.0.9: %s placeholders are positional, not numbered — repeat
    # user_id once per occurrence (was $1/$1 reuse pre-conversion).
    misc_rows = ctx.sql.query(
        "SELECT "
        "  (SELECT COUNT(*) FROM otaku_reviews WHERE user_id = %s) AS reviews, "
        "  (SELECT COUNT(*) FROM otaku_notifications WHERE user_id = %s) AS subs",
        [user_id, user_id],
    ) or [{}]
    m = media_rows[0] or {}
    n = misc_rows[0] or {}
    return {
        "total": int(m.get("total") or 0),
        "favorites": int(m.get("favorites") or 0),
        "completed": int(m.get("completed") or 0),
        "rated": int(m.get("rated") or 0),
        "reviews": int(n.get("reviews") or 0),
        "subs": int(n.get("subs") or 0),
    }


class _ach_stats_scope:
    """Per-call memoization for achievement predicates. Reentrant per-thread
    — an inner scope on the same dispatcher thread is a no-op so callers
    (e.g. `cmd_achievements` wrapping `_check_and_award_achievements`) load
    stats exactly once across the nested call.

    v10.0.2: the underlying cache is `threading.local()`, so concurrent
    /achievements calls on different dispatcher threads never see each
    other's stats. Each thread owns its own scope lifetime independently.
    """
    def __init__(self, ctx: Context, user_id: str):
        self._ctx = ctx
        self._user_id = user_id
        self._owns = False

    def __enter__(self):
        if _ach_stats_current() is None:
            _ach_stats_set(_ach_load_stats(self._ctx, self._user_id))
            self._owns = True
        return _ach_stats_current()

    def __exit__(self, *exc):
        if self._owns:
            _ach_stats_set(None)
        return False


def _ach_count_rows(ctx: Context, user_id: str, where_extra: str = "") -> int:
    """Helper — COUNT(*) from otaku_user_media with optional extra WHERE.

    v10.0.1: inside an `_ach_stats_scope`, reads from the cached aggregate
    row; outside one, falls back to the per-call SQL it always ran.
    v10.0.2: the scope cache is now thread-local (see _ach_stats_tls).
    v10.0.5: SQL fall-back goes through the static `_ACH_COUNT_SQL` dispatch
    table instead of an f-string. Unknown `where_extra` returns 0 (the
    predicate doesn't qualify) rather than building dynamic SQL.
    """
    if not user_id:
        return 0
    stats = _ach_stats_current()
    if isinstance(stats, dict):
        key = _ACH_STATS_KEYS.get(where_extra)
        if key is not None:
            return int(stats.get(key) or 0)
    sql = _ACH_COUNT_SQL.get(where_extra)
    if sql is None:
        return 0
    rows = ctx.sql.query(sql, [user_id]) or [{"n": 0}]
    return int((rows[0] or {}).get("n") or 0)


def _ach_count_reviews(ctx: Context, user_id: str) -> int:
    """v10.0.1: scope-aware review count for the community / first_review
    predicates and the reviews progress line. v10.0.2: thread-local."""
    if not user_id:
        return 0
    stats = _ach_stats_current()
    if isinstance(stats, dict):
        return int(stats.get("reviews") or 0)
    rows = ctx.sql.query(
        "SELECT COUNT(*) AS n FROM otaku_reviews WHERE user_id = %s",
        [user_id],
    ) or [{"n": 0}]
    return int((rows[0] or {}).get("n") or 0)


def _ach_count_subs(ctx: Context, user_id: str) -> int:
    """v10.0.1: scope-aware subscription count for the subscriber predicate
    and the subscriptions progress line. v10.0.2: thread-local."""
    if not user_id:
        return 0
    stats = _ach_stats_current()
    if isinstance(stats, dict):
        return int(stats.get("subs") or 0)
    rows = ctx.sql.query(
        "SELECT COUNT(*) AS n FROM otaku_notifications WHERE user_id = %s",
        [user_id],
    ) or [{"n": 0}]
    return int((rows[0] or {}).get("n") or 0)


def _ach_check_first_anime(ctx, uid):
    return _ach_count_rows(ctx, uid) >= 1
def _ach_check_first_favorite(ctx, uid):
    return _ach_count_rows(ctx, uid, "AND is_favorite = TRUE") >= 1
def _ach_check_completed_10(ctx, uid):
    return _ach_count_rows(ctx, uid, "AND status = 'completed'") >= 10
def _ach_check_completed_50(ctx, uid):
    return _ach_count_rows(ctx, uid, "AND status = 'completed'") >= 50
def _ach_check_rated_25(ctx, uid):
    return _ach_count_rows(ctx, uid, "AND rating IS NOT NULL") >= 25
def _ach_check_rated_100(ctx, uid):
    return _ach_count_rows(ctx, uid, "AND rating IS NOT NULL") >= 100
def _ach_check_first_review(ctx, uid):
    return _ach_count_reviews(ctx, uid) >= 1
def _ach_check_community(ctx, uid):
    return _ach_count_reviews(ctx, uid) >= 5
def _ach_check_polyglot(ctx, uid):
    return _get_pref_language(ctx, uid) is not None
def _ach_check_subscriber(ctx, uid):
    return _ach_count_subs(ctx, uid) >= 3


def _ach_progress_completed(ctx, uid):
    n = _ach_count_rows(ctx, uid, "AND status = 'completed'")
    return f"{n} completed"
def _ach_progress_rated(ctx, uid):
    n = _ach_count_rows(ctx, uid, "AND rating IS NOT NULL")
    return f"{n} rated"
def _ach_progress_reviews(ctx, uid):
    return f"{_ach_count_reviews(ctx, uid)} reviews"
def _ach_progress_subs(ctx, uid):
    return f"{_ach_count_subs(ctx, uid)} subscriptions"


ACHIEVEMENTS: list[dict] = [
    {
        "key": "first_anime", "name": "📺 First Look",
        "description": "Look up your first anime with /anime.",
        "check": _ach_check_first_anime, "progress": None,
    },
    {
        "key": "first_favorite", "name": "⭐ Favorite Picker",
        "description": "Add your first favorite via /favorite.",
        "check": _ach_check_first_favorite, "progress": None,
    },
    {
        "key": "first_review", "name": "📝 Critic's Debut",
        "description": "Submit your first /review.",
        "check": _ach_check_first_review, "progress": None,
    },
    {
        "key": "completed_10", "name": "🎬 Binge Watcher",
        "description": "Mark 10 anime as completed via /watch or /progress.",
        "check": _ach_check_completed_10, "progress": _ach_progress_completed,
    },
    {
        "key": "completed_50", "name": "🏆 Veteran Viewer",
        "description": "Mark 50 anime as completed.",
        "check": _ach_check_completed_50, "progress": _ach_progress_completed,
    },
    {
        "key": "rated_25", "name": "🎯 Opinionated",
        "description": "Rate 25 anime via /rate.",
        "check": _ach_check_rated_25, "progress": _ach_progress_rated,
    },
    {
        "key": "rated_100", "name": "📊 Curator",
        "description": "Rate 100 anime — a full review-graded catalogue.",
        "check": _ach_check_rated_100, "progress": _ach_progress_rated,
    },
    {
        "key": "community", "name": "✍️ Community Voice",
        "description": "Write 5 /review entries — give back to the server.",
        "check": _ach_check_community, "progress": _ach_progress_reviews,
    },
    {
        "key": "seasonal_subscriber", "name": "🔔 Tuned In",
        "description": "Subscribe to airing pings for 3+ anime via /notify.",
        "check": _ach_check_subscriber, "progress": _ach_progress_subs,
    },
    {
        "key": "polyglot", "name": "🌐 Polyglot",
        "description": "Set a non-default language via /preferences.",
        "check": _ach_check_polyglot, "progress": None,
    },
]


def _get_user_achievements(ctx: Context, user_id: str) -> set[str]:
    """Return the set of achievement keys this user has already been awarded."""
    if not user_id:
        return set()
    rows = ctx.sql.query(
        "SELECT achievement_key FROM otaku_achievements WHERE user_id = %s",
        [user_id],
    ) or []
    return {r["achievement_key"] for r in rows if r.get("achievement_key")}


def _check_and_award_achievements(ctx: Context, user_id: str) -> list[str]:
    """Run every predicate, award newly-met achievements, return the list
    of keys awarded THIS call (for the 'newly earned' surface).

    v10.0.1: opens an `_ach_stats_scope` so all SQL-backed predicates share
    one aggregate fetch. Reentrant — when called from inside
    `cmd_achievements`'s outer scope, this is a no-op.

    Defensive: a SQL transport failure when fetching the already-earned
    set returns [] (no awards this round) rather than raising into the
    handler. Next call retries.
    """
    if not user_id:
        return []
    try:
        already = _get_user_achievements(ctx, user_id)
    except Exception:  # noqa: BLE001 — never strand the handler
        return []
    newly_awarded: list[str] = []
    with _ach_stats_scope(ctx, user_id):
        for ach in ACHIEVEMENTS:
            key = ach["key"]
            if key in already:
                continue
            try:
                qualifies = ach["check"](ctx, user_id)
            except Exception:  # noqa: BLE001 — predicate must never strand the handler
                continue
            if not qualifies:
                continue
            try:
                ctx.sql.execute(
                    "INSERT INTO otaku_achievements (user_id, achievement_key) "
                    "VALUES (%s, %s) "
                    "ON CONFLICT (user_id, achievement_key) DO NOTHING",
                    [user_id, key],
                )
                newly_awarded.append(key)
            except Exception:  # noqa: BLE001
                continue
    return newly_awarded


@plugin.on_slash_command("achievements")
def cmd_achievements(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    target_id, display, is_self = _extract_target_user(event, opts)
    ctx.interaction.defer(ephemeral=True)

    # v10.0.1: open the stats scope once. The nested call into
    # `_check_and_award_achievements` and the progress loop below both
    # consult the same cached aggregate row — one SQL fetch instead of 16.
    with _ach_stats_scope(ctx, target_id):
        # Re-check awards for the target user. Even when viewing someone
        # else's achievements, we re-check their predicates (cheap inside
        # the scope) so the count surface always reflects current state.
        newly = _check_and_award_achievements(ctx, target_id)
        earned = _get_user_achievements(ctx, target_id)

        if not earned and not newly:
            empty = S.ACHIEVEMENTS_NONE_OWN if is_self else S.ACHIEVEMENTS_NONE_OTHER.format(who=display)
            _reply_error(ctx, empty, deferred=True)
            return

        # Build the embed: newly-earned (if any), earned list, then in-progress.
        description_parts: list[str] = []
        if newly:
            names = ", ".join(
                next((a["name"] for a in ACHIEVEMENTS if a["key"] == k), k)
                for k in newly
            )
            description_parts.append(S.ACHIEVEMENTS_NEW_AWARDED.format(names=names) + "\n")

        earned_lines: list[str] = []
        unearned_lines: list[str] = []
        for ach in ACHIEVEMENTS:
            line = f"**{ach['name']}** — {ach['description']}"
            if ach["key"] in earned:
                earned_lines.append(line)
            else:
                prog = ach.get("progress")
                prog_text = ""
                if callable(prog):
                    try:
                        prog_text = prog(ctx, target_id) or ""
                    except Exception:  # noqa: BLE001
                        prog_text = ""
                if prog_text:
                    unearned_lines.append(f"{line}\n*Progress: {prog_text}*")
                else:
                    unearned_lines.append(line)

    if earned_lines:
        description_parts.append(f"### {S.ACHIEVEMENTS_EARNED_HEADER}\n" + "\n".join(earned_lines))
    if unearned_lines:
        description_parts.append(f"### {S.ACHIEVEMENTS_UNEARNED_HEADER}\n" + "\n".join(unearned_lines))

    header = (
        S.ACHIEVEMENTS_HEADER_OWN if is_self
        else S.ACHIEVEMENTS_HEADER_OTHER.format(who=display)
    )
    embed = {
        "title": header,
        "description": "\n\n".join(description_parts),
        "color": ANILIST_COLOR,
        "footer": {"text": S.ACHIEVEMENTS_FOOTER.format(earned=len(earned), total=len(ACHIEVEMENTS))},
    }
    ctx.interaction.followup(embeds=[embed], ephemeral=True)


def _render_mood(
    ctx: Context, feeling: str, page: int, *, deferred: bool, ephemeral_reply: bool = False
) -> None:
    mood = MOODS[feeling]
    media_list, has_next = _search_by_genre_tag_blend(
        ctx, mood["genres"], mood.get("tags") or [], page
    )
    if media_list is None:
        _reply_anilist_failure(ctx, deferred=deferred)
        return
    if not media_list:
        _reply_error(
            ctx, S.MOOD_NO_RESULTS.format(label=mood["label"]), deferred=deferred
        )
        return

    header = S.MOOD_HEADER.format(label=mood["label"])
    embed = _make_list_embed(media_list, header, page=page, has_next=has_next)
    # Footer mentions the active filters so users understand the blend.
    if mood.get("tags"):
        footer = S.MOOD_FOOTER_GENRES_TAGS.format(
            genres=", ".join(mood["genres"]),
            tags=", ".join(mood["tags"]),
        )
    else:
        footer = S.MOOD_FOOTER_GENRES.format(genres=", ".join(mood["genres"]))
    embed["footer"] = {"text": footer}

    prev_id = f"otaku:mood:{feeling}:{page - 1}" if page > 1 else None
    next_id = f"otaku:mood:{feeling}:{page + 1}" if has_next else None
    components = [_page_buttons(prev_id, next_id)]
    select_row = _make_select_row(media_list)
    if select_row is not None:
        components.append(select_row)

    if deferred:
        ctx.interaction.followup(
            embeds=[embed], components=components, ephemeral=ephemeral_reply
        )
    else:
        ctx.interaction.respond(
            embeds=[embed], components=components, ephemeral=ephemeral_reply
        )


@plugin.on_slash_command("mood")
def cmd_mood(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    opts = _option_map(event)
    feeling = (opts.get("feeling") or "").strip().lower()
    if feeling not in MOODS:
        ctx.interaction.respond(
            content=S.MOOD_UNKNOWN.format(feeling=feeling or "(empty)"),
            ephemeral=True,
        )
        return
    ctx.interaction.defer()
    _render_mood(ctx, feeling, page=1, deferred=True)


# ── v6.0.0 — /recommend (collaborative filtering) ───────────────────────────
#
# Algorithm: cosine similarity over per-user rating vectors scoped to this
# server (ctx.sql is already per-(plugin, server) so no explicit server_id
# filter is needed). For the target user we score each candidate as
#   score(media) = Σ over qualifying peers of (cosine_sim × peer_rating)
# Peers must share ≥ RECOMMEND_MIN_SHARED_TITLES rated anime to qualify
# (filters out trivial "we both rated one Pokémon movie" overlaps). Up to
# RECOMMEND_PEER_CAP peers are scanned, sampled randomly if more. Anime the
# target already tracks (any status) are excluded from candidates. Fallback
# to AniList /similar (seeded by the target's highest-rated tracked anime)
# triggers when the target has < RECOMMEND_MIN_SELF_RATINGS ratings OR no
# peer qualifies.

RECOMMEND_PEER_CAP = 50
RECOMMEND_MIN_SELF_RATINGS = 3
RECOMMEND_MIN_SHARED_TITLES = 3
RECOMMEND_RESULT_LIMIT = 5
# v10.0.5 — the SDK caps `ctx.sql.query` at 1000 rows (transport-level,
# `min(int(limit), 1000)`). Both caps below align with that hard ceiling.
# RECOMMEND_VECTOR_LIMIT (1000): the target's own rating vector is capped
#   at the 1000 highest-rated rows. Typical users rate 100–500 anime;
#   power users will see their tail truncated.
# RECOMMEND_PEER_RATING_CAP (20): each peer's top-N rated anime considered
#   for shared-title cosine. 50 peers × 20 = 1000, the SDK ceiling for the
#   batched peer query. Reduces noise from peers' lightly-rated tail.
RECOMMEND_VECTOR_LIMIT = 1000
RECOMMEND_PEER_RATING_CAP = 20


def _recommend_user_vector(ctx: Context, user_id: str) -> dict[int, float]:
    """media_id → user's rating on a 0.5..10.0 scale. Anime-only; unrated rows omitted.

    v10.0.4: hard-capped via LIMIT + ORDER BY. v10.0.5: cap aligned to the
    SDK's 1000-row ceiling, and the LIMIT clause is a literal in the SQL.

    v10.0.8 — wrapped in a try/except that logs the failing SQL when the
    host returns the generic `SQL execution failed` error. The cause of
    that error on the live host hasn't been isolated (same error fires
    against the simpler `_aggregate_user_stats` SELECT after v10.0.7's
    schema bootstrap completed); the log captures it for v10.0.9
    triage. Returns an empty vector on failure so /recommend falls
    through to its AniList-similar seed path.
    """
    try:
        rows = ctx.sql.query(
            "SELECT media_id, rating FROM otaku_user_media "
            "WHERE user_id = %s AND media_type = 'anime' AND rating IS NOT NULL "
            "ORDER BY rating DESC, media_id LIMIT 1000",
            [user_id],
        ) or []
    except Exception as exc:  # noqa: BLE001 — v10.0.8 diagnostic capture
        ctx.log(
            f"_recommend_user_vector SQL failed: {exc}",
            level="error",
            tags=["recommend", "sql"],
        )
        return {}
    vec: dict[int, float] = {}
    for r in rows:
        mid = r.get("media_id")
        rating = r.get("rating")
        if mid is None or rating is None:
            continue
        vec[int(mid)] = float(rating) / 2.0
    return vec


def _recommend_tracked_ids(ctx: Context, user_id: str) -> set[int]:
    """All anime media_ids the user has tracked in any status — excluded from candidates."""
    rows = ctx.sql.query(
        "SELECT media_id FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime'",
        [user_id],
    ) or []
    return {int(r["media_id"]) for r in rows if r.get("media_id") is not None}


def _recommend_peer_ids(ctx: Context, target_id: str) -> list[str]:
    """Other users on this server with at least one anime rating."""
    rows = ctx.sql.query(
        "SELECT DISTINCT user_id FROM otaku_user_media "
        "WHERE media_type = 'anime' AND rating IS NOT NULL AND user_id != %s",
        [target_id],
    ) or []
    return [str(r["user_id"]) for r in rows if r.get("user_id") is not None]


def _recommend_peer_vectors_batch(
    ctx: Context, peer_ids: list[str]
) -> dict[str, dict[int, float]]:
    """v10.0.1 — single-query batch fetch of every peer's rating vector.
    v10.0.5 — per-peer ROW_NUMBER cap so the batch stays under the SDK's
    1000-row cap on `ctx.sql.query`.

    Without the per-peer cap, 50 peers × ~50 typical ratings = 2500 rows;
    the SDK silently truncated at 1000, dropping ~60% of peer data
    arbitrarily and degrading recommendations on active servers.

    The window function picks each peer's top
    `RECOMMEND_PEER_RATING_CAP` rated anime (ordered by rating, then
    media_id for determinism). With cap=20 and RECOMMEND_PEER_CAP=50,
    the total row count is bounded at 50 × 20 = 1000 — exactly the SDK
    ceiling. Cosine sim still picks up the highest-confidence shared
    titles for each peer; lightly-rated peer titles drop out, which is
    desirable (low-signal noise) rather than a correctness regression.

    Empty peer_ids returns `{}` without hitting SQL.
    """
    if not peer_ids:
        return {}
    rows = ctx.sql.query(
        "SELECT user_id, media_id, rating FROM ("
        "  SELECT user_id, media_id, rating, "
        "    ROW_NUMBER() OVER ("
        "      PARTITION BY user_id ORDER BY rating DESC, media_id"
        "    ) AS rn "
        "  FROM otaku_user_media "
        "  WHERE user_id = ANY(%s::TEXT[]) "
        "  AND media_type = 'anime' AND rating IS NOT NULL"
        ") sub "
        "WHERE rn <= %s",
        [list(peer_ids), RECOMMEND_PEER_RATING_CAP],
    ) or []
    out: dict[str, dict[int, float]] = {}
    for r in rows:
        uid = r.get("user_id")
        mid = r.get("media_id")
        rating = r.get("rating")
        if uid is None or mid is None or rating is None:
            continue
        out.setdefault(str(uid), {})[int(mid)] = float(rating) / 2.0
    return out


def _cosine_similarity(
    target: dict[int, float],
    peer: dict[int, float],
    *,
    target_norm: float | None = None,
) -> tuple[float, int]:
    """Cosine over shared media_ids. Returns (similarity, shared_count).

    v10.0.3: `target_norm` lets the caller hoist the target's L2 norm out
    of a per-peer loop — it's invariant across peers and was previously
    recomputed N times in `_recommend_candidates`. Passing `None`
    (default) preserves the original signature for ad-hoc callers.
    """
    shared = target.keys() & peer.keys()
    if not shared:
        return 0.0, 0
    dot = sum(target[m] * peer[m] for m in shared)
    norm_t = (
        target_norm
        if target_norm is not None
        else math.sqrt(sum(v * v for v in target.values()))
    )
    norm_p = math.sqrt(sum(v * v for v in peer.values()))
    if norm_t == 0.0 or norm_p == 0.0:
        return 0.0, len(shared)
    return dot / (norm_t * norm_p), len(shared)


def _recommend_candidates(
    ctx: Context, target_id: str, target_vector: dict[int, float]
) -> tuple[list[dict], int]:
    """Score CF candidates. Returns (sorted_candidates, qualifying_peer_count).

    Each candidate dict: {media_id, score, peer_count}.
    """
    excluded = _recommend_tracked_ids(ctx, target_id) | set(target_vector.keys())
    peer_ids = _recommend_peer_ids(ctx, target_id)
    if len(peer_ids) > RECOMMEND_PEER_CAP:
        peer_ids = random.sample(peer_ids, RECOMMEND_PEER_CAP)

    peer_vectors = _recommend_peer_vectors_batch(ctx, peer_ids)

    # v10.0.3: hoist target_norm out of the per-peer loop — invariant.
    target_norm = math.sqrt(sum(v * v for v in target_vector.values()))

    candidates: dict[int, dict] = {}
    peers_kept = 0
    for pid in peer_ids:
        peer_vec = peer_vectors.get(pid, {})
        sim, shared = _cosine_similarity(target_vector, peer_vec, target_norm=target_norm)
        if shared < RECOMMEND_MIN_SHARED_TITLES or sim <= 0.0:
            continue
        peers_kept += 1
        for mid, peer_rating in peer_vec.items():
            if mid in excluded:
                continue
            entry = candidates.setdefault(
                mid, {"media_id": mid, "score": 0.0, "peer_count": 0}
            )
            entry["score"] += sim * peer_rating
            entry["peer_count"] += 1

    ordered = sorted(
        candidates.values(),
        key=lambda c: (-c["score"], -c["peer_count"], c["media_id"]),
    )
    return ordered, peers_kept


def _recommend_fallback_seed_id(ctx: Context, user_id: str) -> int | None:
    """Pick what to seed AniList /similar with: top-rated → newest favorite → newest tracked.

    Anime-only; v8.0 added the media_type filter so a favorite manga doesn't
    leak into the anime-recommendation seed path.
    """
    for sql in (
        "SELECT media_id FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' AND rating IS NOT NULL "
        "ORDER BY rating DESC, added_at DESC LIMIT 1",
        "SELECT media_id FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' AND is_favorite = TRUE "
        "ORDER BY added_at DESC LIMIT 1",
        "SELECT media_id FROM otaku_user_media "
        "WHERE user_id = %s AND media_type = 'anime' "
        "ORDER BY added_at DESC LIMIT 1",
    ):
        rows = ctx.sql.query(sql, [user_id]) or []
        if rows and rows[0].get("media_id") is not None:
            return int(rows[0]["media_id"])
    return None


def _recommend_via_anilist_fallback(
    ctx: Context, user_id: str, *, reason: str
) -> None:
    seed_id = _recommend_fallback_seed_id(ctx, user_id)
    if seed_id is None:
        _reply_error(ctx, S.RECOMMEND_NO_DATA, deferred=True)
        return
    data = _anilist_query(ctx, QUERY_SIMILAR_BY_ID, {"id": seed_id})
    if data is None or not data.get("Media"):
        _reply_anilist_failure(ctx, deferred=True)
        return
    media = data["Media"]
    nodes = ((media.get("recommendations") or {}).get("nodes")) or []
    excluded = _recommend_tracked_ids(ctx, user_id)
    recs = [
        n["mediaRecommendation"]
        for n in nodes
        if n and n.get("mediaRecommendation")
        and n["mediaRecommendation"].get("id") not in excluded
    ][:RECOMMEND_RESULT_LIMIT]
    if not recs:
        _reply_error(ctx, S.RECOMMEND_FALLBACK_EMPTY, deferred=True)
        return

    parent_title = _format_title(media)
    lines = []
    for r in recs:
        title = _format_title(r)
        url = r.get("siteUrl") or ""
        anchor = f"[{title}]({url})" if url else title
        lines.append(f"• {anchor}")
    embed = {
        "title": S.RECOMMEND_FALLBACK_HEADER.format(title=parent_title),
        "description": f"{reason}\n\n" + "\n".join(lines),
        "color": ANILIST_COLOR,
        "footer": {"text": S.FOOTER_ANILIST},
    }
    ctx.interaction.followup(embeds=[embed], ephemeral=True)


@plugin.on_slash_command("recommend")
def cmd_recommend(ctx: Context, event: dict) -> None:
    user_id = event.get("user_id") or ""
    if _on_cooldown(ctx, user_id):
        return
    ctx.interaction.defer(ephemeral=True)

    target_vector = _recommend_user_vector(ctx, user_id)
    if len(target_vector) < RECOMMEND_MIN_SELF_RATINGS:
        _recommend_via_anilist_fallback(
            ctx, user_id, reason=S.RECOMMEND_FALLBACK_FEW_RATINGS
        )
        return

    candidates, peers_kept = _recommend_candidates(ctx, user_id, target_vector)
    if peers_kept == 0 or not candidates:
        _recommend_via_anilist_fallback(
            ctx, user_id, reason=S.RECOMMEND_FALLBACK_NO_PEERS
        )
        return

    top = candidates[:RECOMMEND_RESULT_LIMIT]
    media_ids = [c["media_id"] for c in top]
    data = _anilist_query(ctx, QUERY_MEDIA_BATCH, {"ids": sorted(media_ids)}, cache=True)
    if data is None:
        _reply_anilist_failure(ctx, deferred=True)
        return
    media_by_id: dict[int, dict] = {}
    for m in ((data.get("Page") or {}).get("media") or []):
        mid = m.get("id")
        if isinstance(mid, int):
            media_by_id[mid] = m

    lines = []
    for c in top:
        mid = c["media_id"]
        m = media_by_id.get(mid)
        title = _format_title(m) if m else f"#{mid}"
        url = (m or {}).get("siteUrl") or ""
        anchor = f"[{title}]({url})" if url else title
        peers = c["peer_count"]
        viewers = "viewer" if peers == 1 else "viewers"
        lines.append(f"• {anchor} — recommended by {peers} {viewers}")

    embed = {
        "title": S.RECOMMEND_HEADER,
        "description": "\n".join(lines),
        "color": ANILIST_COLOR,
        "footer": {"text": S.RECOMMEND_FOOTER_CF.format(peers=peers_kept)},
    }
    ctx.interaction.followup(embeds=[embed], ephemeral=True)


# ── Entry point ─────────────────────────────────────────────────────────────
# Non-negotiable per the yourbot-plugin-dev skill: plugin.run() is the last line.
# Tests import __main__ via a renamed module spec; they set OTAKU_SKIP_RUN=1 in
# tests/conftest.py so the import doesn't block on the RPC loop.
if not os.environ.get("OTAKU_SKIP_RUN"):
    plugin.run()

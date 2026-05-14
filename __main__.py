"""
Otaku — anime discovery for Discord via AniList.

Slash commands:
  /anime <query>             one-shot search; caches the result for 7 days
  /discover <genre> [sort]   genre browse with prev/next + select-to-expand
  /trending                  current-season top trending
  /similar [anime]           recommendations for an anime (or the cached one)

The plugin is interaction-only — no message events, no schedules, no SQL.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, timezone

from mmo_maid_sdk import (
    ActionRow,
    Button,
    Context,
    Plugin,
    RateLimitError,
    RpcTimeoutError,
    SelectMenu,
    SelectOption,
    ValidationError,
)

plugin = Plugin()


# ── SQL schema ───────────────────────────────────────────────────────────────
# Per-user anime tracking. Rows auto-scoped to ctx.server_id by the runner —
# no server_id column needed. DDL is idempotent (IF NOT EXISTS) and runs from
# both on_install and on_ready so pool-mode workers and v1.x→v2.0 upgrades
# both seed cleanly. See ROADMAP.md "Phase 2" for the rationale.
_SCHEMA_DDL = (
    "CREATE TABLE IF NOT EXISTS otaku_user_anime ("
    "  user_id TEXT NOT NULL,"
    "  media_id INTEGER NOT NULL,"
    "  status TEXT NOT NULL,"
    "  is_favorite BOOLEAN NOT NULL DEFAULT FALSE,"
    "  added_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    "  PRIMARY KEY (user_id, media_id))"
)
_SCHEMA_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS otaku_user_anime_user_status_added_idx "
    "ON otaku_user_anime (user_id, status, added_at DESC)"
)

# v2.1.0 — additive column for ratings (1.0–10.0 stored as int*2, range 2..20).
# IF NOT EXISTS makes this idempotent and safe to run on existing tables.
_SCHEMA_RATING_DDL = (
    "ALTER TABLE otaku_user_anime ADD COLUMN IF NOT EXISTS rating SMALLINT"
)
# v2.2.0 — additive column for episode progress.
_SCHEMA_EPISODES_DDL = (
    "ALTER TABLE otaku_user_anime ADD COLUMN IF NOT EXISTS episodes_watched SMALLINT DEFAULT 0"
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

    # /ratings.
    RATINGS_HEADER_OWN = "⭐ Your ratings"
    RATINGS_HEADER_OTHER = "⭐ {who}'s ratings"
    RATINGS_EMPTY_OWN = (
        "You haven't rated any anime yet. Try `/rate score: 8` after a `/anime` lookup."
    )
    RATINGS_EMPTY_OTHER = "{who} hasn't rated any anime yet."


S = _Strings


ANILIST_URL = "https://graphql.anilist.co"
ANILIST_COLOR = 0x02A9FF
PER_PAGE = 5
DESC_MAX = 350
COOLDOWN_SECONDS = 2
LAST_ANIME_TTL = 7 * 24 * 60 * 60  # 7 days
ANILIST_CACHE_TTL = 5 * 60          # 5 minutes — short enough that fresh trends still update
ANILIST_CACHE_MAX_ENTRIES = 128     # bounded; LRU-ish via insertion-order pop

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

# /list and /favorites — batch fetch titles for up to PER_PAGE media_ids in one round trip.
QUERY_MEDIA_BATCH = (
    "query ($ids: [Int]) {"
    "  Page(perPage: 50) {"
    "    media(id_in: $ids, type: ANIME) {" + _MEDIA_FIELDS + "}"
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


def _option_map(event: dict) -> dict:
    """Slash command options arrive as a list of {name, value}; flatten."""
    opts = event.get("options") or []
    if isinstance(opts, dict):
        return opts
    return {o["name"]: o["value"] for o in opts if isinstance(o, dict) and "name" in o}


def _options_list(opts: dict | list | None) -> list:
    """Same as _option_map but yields the raw list form for tests that prefer it."""
    if not opts:
        return []
    if isinstance(opts, list):
        return opts
    return [{"name": k, "value": v} for k, v in opts.items()]


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


def _make_anime_embed(media: dict, *, user_progress: int | None = None) -> dict:
    """Full anime card — used by /anime and the expand-from-list select.

    If `user_progress` is a positive int, a "Your progress" field is appended
    so users can see where they left off without running /list.
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
        embed["fields"].append({"name": S.PROGRESS_FIELD_NAME, "value": value, "inline": False})
    cover = (media.get("coverImage") or {}).get("large")
    if cover:
        embed["thumbnail"] = {"url": cover}
    banner = media.get("bannerImage")
    if banner:
        embed["image"] = {"url": banner}
    return embed


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


def _cache_key(query: str, variables: dict) -> str:
    return repr((query, sorted(variables.items())))


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
        status = resp.get("status") if resp else 0
        if 500 <= int(status or 0) < 600:
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

    if resp is None or resp.get("status") != 200:
        ctx.log(
            "anilist non-200",
            level="warning",
            tags=["anilist", "http"],
            status=str(resp.get("status") if resp else None),
        )
        return None

    try:
        payload = json.loads(resp.get("body_bytes") or "")
    except (TypeError, ValueError):
        ctx.log("anilist returned unparseable JSON", level="error", tags=["anilist"])
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
    data = _anilist_query(
        ctx,
        QUERY_DISCOVER,
        {"genre": genre, "sort": [sort_const], "page": page, "perPage": PER_PAGE},
        cache=(page == 1),
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
    data = _anilist_query(
        ctx,
        QUERY_SEASON,
        {"season": season, "year": year, "sort": ["TRENDING_DESC"],
         "page": page, "perPage": PER_PAGE},
        cache=(page == 1),
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

def _bootstrap_schema(ctx: Context) -> None:
    """Create the otaku_user_anime table, its index, and additive columns. Idempotent.

    DDLs land in version order. New columns added in later versions go here
    with ADD COLUMN IF NOT EXISTS so the bootstrap stays a single source of
    truth across every upgrade path.
    """
    ctx.sql.execute(_SCHEMA_DDL)
    ctx.sql.execute(_SCHEMA_INDEX_DDL)
    # v2.1.0
    ctx.sql.execute(_SCHEMA_RATING_DDL)
    # v2.2.0
    ctx.sql.execute(_SCHEMA_EPISODES_DDL)


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


def _upsert_user_anime(
    ctx: Context,
    user_id: str,
    media_id: int,
    *,
    status: str | None = None,
    is_favorite: bool | None = None,
) -> None:
    """Insert or update a row in otaku_user_anime, mutating only the fields passed."""
    # Defaults for new rows; for existing rows the ON CONFLICT DO UPDATE
    # branch only touches the columns the caller actually set.
    insert_status = status if status is not None else "watching"
    insert_favorite = bool(is_favorite) if is_favorite is not None else False

    update_clauses = []
    if status is not None:
        update_clauses.append("status = EXCLUDED.status")
    if is_favorite is not None:
        update_clauses.append("is_favorite = EXCLUDED.is_favorite")
    update_sql = ", ".join(update_clauses) if update_clauses else "user_id = otaku_user_anime.user_id"

    sql = (
        "INSERT INTO otaku_user_anime (user_id, media_id, status, is_favorite) "
        "VALUES ($1, $2, $3, $4) "
        f"ON CONFLICT (user_id, media_id) DO UPDATE SET {update_sql}"
    )
    ctx.sql.execute(sql, [user_id, media_id, insert_status, insert_favorite])


def _is_favorite(ctx: Context, user_id: str, media_id: int) -> bool:
    row = ctx.sql.query_one(
        "SELECT is_favorite FROM otaku_user_anime WHERE user_id = $1 AND media_id = $2",
        [user_id, media_id],
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
        ctx.interaction.respond(content=S.ANIME_USAGE, ephemeral=True)
        return

    ctx.interaction.defer()
    # Normalize the search key so equivalent queries hit the same cache entry.
    data = _anilist_query(ctx, QUERY_SEARCH_ONE, {"q": query.lower()}, cache=True)
    if data is None:
        _reply_anilist_failure(ctx, deferred=True)
        return
    media = data.get("Media")
    if not media:
        _reply_error(ctx, S.ANIME_NOT_FOUND.format(query=_truncate(query, 80)), deferred=True)
        return

    media_id = media.get("id")
    if media_id is not None and user_id:
        ctx.kv.set(f"last_anime:user:{user_id}", media_id, ttl_seconds=LAST_ANIME_TTL)

    progress = _get_user_progress(ctx, user_id, int(media_id or 0))
    embed = _make_anime_embed(media, user_progress=progress)
    buttons = [Button("Similar", custom_id=f"otaku:similar:{media_id}", style="primary", emoji="🔁")]
    site_url = media.get("siteUrl")
    if site_url:
        buttons.append(Button("Open on AniList", url=site_url, style="link", emoji="🌐"))
    ctx.interaction.followup(embeds=[embed], components=[ActionRow(*buttons)])


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

    progress = _get_user_progress(ctx, user_id, int(media_id or 0))
    embed = _make_anime_embed(media, user_progress=progress)
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


# Static usage examples — one per command. The /help embed merges these with the
# descriptions in manifest.json so the help text never drifts behind a new
# slash command being declared.
_HELP_EXAMPLES = {
    "anime":     "`/anime query: Your Name`",
    "discover":  "`/discover genre: Action sort: trending`",
    "trending":  "`/trending`",
    "similar":   "`/similar` (uses your last lookup) or `/similar anime: Your Name`",
    "random":    "`/random` or `/random genre: Romance`",
    "character": "`/character query: Edward Elric`",
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
    body = "\n\n".join(lines) if lines else S.HELP_EMPTY

    embed = {
        "title": S.HELP_TITLE,
        "description": body,
        "color": ANILIST_COLOR,
        "footer": {"text": S.HELP_FOOTER},
    }
    ctx.interaction.respond(embeds=[embed], ephemeral=True)


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
            content=f"Status must be one of: {', '.join(VALID_STATUSES)}",
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
    """Read PER_PAGE+1 rows for the given user/scope/page. Returns (rows, has_next)."""
    offset = max(0, (page - 1) * PER_PAGE)
    limit = PER_PAGE + 1
    if scope == "favorites":
        sql = (
            "SELECT media_id, status, is_favorite FROM otaku_user_anime "
            "WHERE user_id = $1 AND is_favorite = TRUE "
            "ORDER BY added_at DESC LIMIT $2 OFFSET $3"
        )
        params = [target_user_id, limit, offset]
    elif scope == "all":
        sql = (
            "SELECT media_id, status, is_favorite FROM otaku_user_anime "
            "WHERE user_id = $1 "
            "ORDER BY added_at DESC LIMIT $2 OFFSET $3"
        )
        params = [target_user_id, limit, offset]
    else:
        sql = (
            "SELECT media_id, status, is_favorite FROM otaku_user_anime "
            "WHERE user_id = $1 AND status = $2 "
            "ORDER BY added_at DESC LIMIT $3 OFFSET $4"
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


# ── v2.2.0 — episode progress ───────────────────────────────────────────────

def _get_user_progress(ctx: Context, user_id: str, media_id: int) -> int:
    """Look up the user's recorded episodes_watched for a media id. 0 if no row."""
    if not user_id or not media_id:
        return 0
    row = ctx.sql.query_one(
        "SELECT episodes_watched FROM otaku_user_anime WHERE user_id = $1 AND media_id = $2",
        [user_id, media_id],
    )
    if not row:
        return 0
    val = row.get("episodes_watched") or 0
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 0


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

    ctx.sql.execute(
        "INSERT INTO otaku_user_anime (user_id, media_id, status, episodes_watched) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (user_id, media_id) DO UPDATE SET "
        "  episodes_watched = EXCLUDED.episodes_watched, "
        "  status = CASE WHEN EXCLUDED.status = 'completed' THEN 'completed' "
        "               ELSE otaku_user_anime.status END",
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
    ctx.sql.execute(
        "INSERT INTO otaku_user_anime (user_id, media_id, status, rating) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (user_id, media_id) DO UPDATE SET rating = EXCLUDED.rating",
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
        "SELECT media_id, rating, status, is_favorite FROM otaku_user_anime "
        "WHERE user_id = $1 AND rating IS NOT NULL "
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


# Register the dispatcher under every dynamic prefix we use. The SDK matches on
# exact custom_id, so we hook the raw interaction_create event and filter ourselves.
@plugin.on_event("interaction_create")
def _route_components(ctx: Context, event: dict) -> None:
    if event.get("interaction_type") != 3:  # MESSAGE_COMPONENT
        return
    cid = event.get("custom_id") or ""
    if cid == "otaku:expand":
        return  # handled by @plugin.on_component above
    if (
        cid.startswith("otaku:page:")
        or cid.startswith("otaku:trend:")
        or cid.startswith("otaku:similar:")
        or cid.startswith("otaku:list:")
    ):
        _component_dispatch(ctx, event)


# Convenience wrapper exposed for tests — let tests call the similar handler
# directly with a media ID without constructing a full event.
def handle_similar_button(ctx: Context, event: dict) -> None:
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

    progress = _get_user_progress(ctx, user_id, int(media.get("id") or 0))
    embed = _make_anime_embed(media, user_progress=progress)
    buttons = [Button("Similar", custom_id=f"otaku:similar:{media.get('id')}", style="primary", emoji="🔁")]
    site_url = media.get("siteUrl")
    if site_url:
        buttons.append(Button("Open on AniList", url=site_url, style="link", emoji="🌐"))
    ctx.interaction.followup(embeds=[embed], components=[ActionRow(*buttons)], ephemeral=True)


# ── Entry point ─────────────────────────────────────────────────────────────
# Non-negotiable per the mmo-maid-plugins skill: plugin.run() is the last line.
# Tests import __main__ via a renamed module spec; they set OTAKU_SKIP_RUN=1 in
# tests/conftest.py so the import doesn't block on the RPC loop.
if not os.environ.get("OTAKU_SKIP_RUN"):
    plugin.run()

# Otaku

> Discover, search, and learn about anime from inside Discord — powered by AniList.

A plugin for [YourBot](https://yourbot.gg) (the platform formerly known as MMO Maid) — runs sandboxed in the platform and reacts to slash-command interactions on installed servers.

## What it does

Otaku gives your server a small set of slash commands for finding anime: look one up by name, browse a genre, see what's trending this season, or pull recommendations similar to a show you've already looked at. Every command answers with a rich Discord embed (cover art, score, episode count, genre tags, a short description, and a link to the AniList page). The plugin caches each user's most recent `/anime` lookup for 7 days so `/similar` works with no arguments — handy when you've already found the show you care about and just want "more like this." Primary data comes from AniList's public GraphQL API, with MAL (Jikan) and Kitsu as fallback search sources — the plugin reaches exactly the three declared proxy domains. Stored data: each user's tracking rows (watch status, rating, episodes, favorites, reviews, subscriptions — see the SQL schema below) plus the small KV keys in the table below.

## Capabilities

This plugin requests the following capabilities. Each is listed in `manifest.json` with a one-line rationale so server admins know *why* it's needed before they install:

| Capability | Tier | Why |
|---|---|---|
| `interaction:respond` | Safe | Reply to slash commands and component (button/select) clicks. Auto-added because the manifest declares `slash_commands`. |
| `proxy:http` | Safe | Call AniList's GraphQL endpoint (`graphql.anilist.co`) for primary search/lookup. v9.1 added MAL (Jikan v4 at `api.jikan.moe`) and Kitsu (`kitsu.io`) as secondary fallback sources for `/anime` and `/manga` when AniList misses. Per-source rate buckets enforce AniList 90/min, Jikan 3/sec, Kitsu 10/sec. |
| `storage:kv` | Safe | Cache each user's last-viewed anime ID for 7 days so `/similar` can default to it. Also caches AniList's genre list for 24h. |
| `storage:sql` | Risky | Per-user anime/manga tracking (`/watch`, `/rate`, `/list`, `/favorites`), reviews, watch parties, polls, AOTW, subscriptions, achievements — 13 tables (see the SQL schema section), auto-scoped per server. |
| `discord:read` | Safe | Read the caller's guild membership and roles to gate `/otaku-admin` to server admins (anyone with `Administrator` or `Manage Server`). Used purely for permission checks. |
| `discord:send_message` | Safe | Post airing notifications to the per-server announcement channel (or the channel each user subscribed in) when AniList reports a new episode is airing. |

The plugin uses one Discord-side write capability — `discord:send_message` — exclusively for posting airing notifications to a server-designated channel. All other Discord interactions are slash command replies.

## Dashboard

A two-page manifest-mode dashboard ships with the plugin. Server admins can view it from the YourBot dev portal:

- **Overview** — four stat cards (tracked rows, active users 30d, episodes watched, airing subscriptions), a watch-status bar chart, and a top-5-tracked-anime table.
- **Settings** — pick the airing-announcement channel without leaving the portal (mirrors `/otaku-admin set-channel`).

All widgets read from the existing SQL tables — no new capability required.

If/when new capabilities are added, update this table *and* `CHANGELOG.md`.

## Slash commands

| Command | Description |
|---|---|
| `/anime <query>` | Multi-source search. Tries AniList first, falls through to MyAnimeList (Jikan) then Kitsu when AniList misses. Replies with a full anime card; footer shows which source served the result. `[🔁 Similar]` button appears only for AniList results (the recommendation graph is AniList-specific). Cached as "last anime" (7 days) — AniList results only. |
| `/discover <genre> [sort]` | Browse a genre. `sort` is one of `popular` (default), `trending`, or `score`. Replies with a paginated list of 5 results plus `[⬅️ Prev]` / `[Next ➡️]` buttons and a select menu to expand any result into the full anime card. |
| `/trending` | Top 5 trending anime for the current season (Winter/Spring/Summer/Fall) — same paginated style as `/discover`. |
| `/similar [anime]` | Top 5 AniList-recommended anime for a given title. If `anime` is omitted, uses your cached last `/anime` lookup (if any); otherwise tells you ephemerally to run `/anime` first. |
| `/random [genre]` | Roll a single random anime. If `genre` is given, the roll is constrained to that genre. Caches the result as your "last anime." |
| `/character <query>` | Look up an AniList character by name. Returns a card with image, native + romaji name, description, and the top 5 media they appear in. First match only. |
| `/voice-actor <query>` | Look up a voice actor. Shows bio, primary language, and the top 5 most-favourited character roles, each with parent media link. First match only. |
| `/staff <query>` | Look up an anime/manga staff person (director, writer, composer, etc.). Shows bio, primary occupations, and the top 5 production credits prefixed with the staff role at each project. First match only. |
| `/studio <query>` | Look up an animation studio. Shows recent works (last 2 years) and older catalog works (each up to 5). Header emoji distinguishes animation studios from other production orgs (licensors, distributors). |
| `/character-popular` | Global character popularity leaderboard, sorted by AniList favourites. Paginated 5 per page; rank numbers continue across pages (#1–5 page 1, #6–10 page 2, etc.). Each row links to the character + their most-popular parent media. |
| `/find <description>` | Natural-language search. Describe what you want in plain English ("slow romance set in school with supernatural twist"); the plugin decodes it into a genre/tag blend via an inline mapping table and shows 5 picks. Footer surfaces what was decoded so you can see why. Single-page; no external LLM. |
| `/preferences [language] [spoilers]` | View or update your per-user preferences. `language:` (en/ja/ko/zh/es/de/fr) — v10 activates Japanese + Spanish translations for ~20 user-visible strings; ko/zh/de/fr currently fall back to English. `spoilers: hide\|show` controls whether `/reviews` wraps detected `SPOILER: …` lines in Discord's spoiler syntax (default: hide). Stored per-user in KV. |
| `/achievements [user]` | View earned + in-progress achievements (10 starter entries: First Look, Favorite Picker, Critic's Debut, Binge Watcher, Veteran Viewer, Opinionated, Curator, Community Voice, Tuned In, Polyglot). Detection runs lazily on access — re-run any time to re-check. Defaults to your own; pass `user:` to view another member's. |
| `/help` | Lists every otaku command with a one-line description and example. Built from `manifest.json` so it always reflects what's registered. |
| `/genres` | Shows AniList's canonical genre list. Cached in KV at `genres:global` (24h TTL); falls back to a live AniList call on cache miss. |
| `/favorite [anime] [remove]` | Mark (or unmark) the user's last `/anime` lookup as a favorite, or pass an `anime:` title explicitly. Stored in SQL. |
| `/favorites [user]` | Paginated list of favorites for the caller or a mentioned user. ⭐ next to each row. |
| `/watch <status>` | Set watch status for the user's last `/anime` lookup. Status is one of `watching`, `completed`, `on_hold`, `dropped`, `plan`. |
| `/list [status] [user]` | Paginated tracker. Status filter optional; defaults to "all." Status emojis (📺 ✅ ⏸ ❌ 📌) prefix every row. |
| `/rate <score>` | Rate the user's last `/anime` lookup on a 1.0–10.0 scale (half-points OK). Stored as `SMALLINT` (score × 2). |
| `/ratings [user]` | Show a user's rated anime, top 25 sorted by score. |
| `/progress <episodes>` | Record episodes watched for the user's last `/anime` lookup. Caps at the anime's total; auto-promotes status to `completed` at total. |
| `/stats [user]` | Aggregate per-user view: counts by status, episodes, est. hours (24min/ep heuristic), mean score, top genre. |
| `/import anilist <username>` | Bulk-import an AniList user's list into your tracker. Streams 50 entries per page. Idempotent — re-imports update, don't duplicate. |
| `/otaku-reset` | Self-service deletion of every tracked row for the caller on this server. Asks to confirm. |
| `/otaku-admin reset-user <user>` | Server-admin-only moderation. Deletes every tracked row for a specific user. Gated to anyone with `Administrator` or `Manage Server`. |
| `/server-watchlist view` | Public, paginated browse of the server's curated anime watchlist. |
| `/server-watchlist add <anime> [note]` | Admin-only. Adds an anime to the server watchlist. Optional note shown alongside the entry. |
| `/server-watchlist remove <anime>` | Admin-only. Removes an anime from the server watchlist. Accepts title or numeric AniList ID. |
| `/compare <user>` | Side-by-side comparison vs another user: totals, shared favorites, divergent ratings, completion recs. |
| `/wp create <anime>` | Start a watch party. Returns a public embed with a `[🎬 Join party]` button. |
| `/wp join <id>` | Manually join a watch party by id. |
| `/wp status <id>` | Show the party's members and their progress, plus status (active/completed/abandoned). |
| `/wp progress <id> <episode>` | Update your episode count. If everyone in the party is at the same episode, a public sync announcement fires. |
| `/leaderboard [metric]` | Server-wide top-10 board. Metric: `completed` (default), `score` (≥3 rated), or `hours`. |
| `/notify <anime>` | Subscribe to airing notifications for an anime. |
| `/unnotify <anime>` | Stop airing notifications for an anime. |
| `/notify-list` | Show your active subscriptions with the next-episode ETA. |
| `/otaku-admin set-channel <#channel>` | Admin-only. Sets where airing pings post. Omit the channel to clear. |
| `/season-premieres [season] [year]` | Paginated browse of upcoming anime premieres. Defaults to next season. |
| `/my-stats` | Richer personal view than `/stats` — top rated, top favorites, recently completed, with completion percentage. |
| `/recommend` | Personalized recommendations via collaborative filtering over this server's rated rows. Cosine similarity across up to 50 peers; peers must share ≥3 rated titles. Falls back to AniList `/similar` (seeded by your top-rated tracked anime) if you have <3 ratings or no peer overlaps enough. |
| `/mood <feeling>` | Mood-based picks. Ten curated moods (`uplifting`, `tense`, `cathartic`, `chill`, `epic`, `nostalgic`, `dark`, `funny`, `romantic`, `adventurous`) each map to an AniList genre/tag blend. Paginated like `/discover`. |
| `/genre-trends` | Trending anime right now in your top 3 most-tracked genres. Bridges discovery and personalization — anime you already track are filtered out. Ephemeral. |
| `/review` | Open a modal to write or edit a review of your last `/anime` lookup. Existing review (if any) is pre-filled. One review per user per anime. |
| `/reviews [anime]` | Browse this server's reviews for an anime. Accepts a title, a numeric AniList ID, or defaults to your last `/anime` lookup. Paginated, sorted by most-recently-edited. Spoiler-marked lines (`SPOILER:`, `[SPOILER]`, `(spoiler)`) are wrapped in Discord's spoiler syntax by default; opt out via `/preferences spoilers: show`. |
| `/aotw start \| status \| end` | Anime-of-the-week voting. Admin starts (top 5 from server watchlist); members vote via numbered buttons; admin ends and winner gets posted in the announcement channel. One active poll per server. |
| `/poll create \| status \| end` | Free-form server polls. Admin creates with a question and 2–4 options; members vote via A/B/C/D buttons; admin ends. Multiple concurrent polls allowed; each has its own `poll_id`. |
| `/manga <query>` | Multi-source manga search (AniList → MAL → Kitsu fallback). Renders chapters, volumes, start year. Footer shows the source. Cached as "last manga" (7 days) — AniList results only. |
| `/manga-discover <genre> [sort]` | Browse manga by genre. Paginated 5 results per page; sorts `popular`/`trending`/`score`. |
| `/manga-favorites [manga] [remove]` | Favorite or unfavorite a manga (defaults to your last `/manga` lookup), or list your manga favorites when called with no args. Manga rows are stored in the same `otaku_user_media` table as anime, separated by `media_type='manga'`. |

### Politeness throttle

Every command that reaches AniList (or SQL) checks an ephemeral per-user cooldown (`otaku:user:<id>`, 2 s) first — the lone exception is `/help`, which only reads the manifest. The cooldown is sandbox-side only — AniList itself permits ~90 req/min globally, and the platform proxy enforces 30/min per (server, plugin). The 2 s per-user throttle stops a single chatty user from monopolising either budget.

### Reliability & observability

The HTTP transport is best-effort by design — any upstream failure logs and returns "no results" rather than crashing a handler:

- **Multi-source fallback** — `/anime` and `/manga` fall through AniList → MAL (Jikan) → Kitsu, so one source being down doesn't kill search.
- **Compression-proof bodies** — requests send `Accept-Encoding: identity` and recover gzip bytes client-side (v10.0.14), after a live incident where the platform proxy passed compressed bodies through lossily.
- **Thread-safe throttling** — the per-source rate buckets and the response cache are lock-guarded (v10.0.16), since the platform runs several dispatcher threads per worker; the limiter can't overshoot an upstream budget or raise into a handler.
- **Metrics & tracing** — every real transport call emits one `http.request` metric tagged `{source, outcome}` (v10.0.15; a spike in `outcome=non_2xx / timeout / bad_body` is the tell for an upstream/proxy incident), and every transport error/anomaly log carries the interaction's `request_id` (v10.0.16) so a burst correlates to its cause. `ctx.metrics` and `ctx.log` need no capability.

## KV key convention

Durable KV keys (`ctx.kv`):

```
last_anime:user:<discord_user_id>   →   <anilist_media_id>   (TTL: 7 days, per-user)
last_manga:user:<discord_user_id>   →   <anilist_media_id>   (TTL: 7 days, per-user)
pref:lang:user:<discord_user_id>    →   "en" | "ja" | ...    (no TTL, per-user)
pref:spoilers:user:<discord_user_id>→   "hide" | "show"      (no TTL, per-user)
genres:global                       →   ["Action", ...]      (TTL: 24h, server-wide)
notify_channel:guild                →   "<channel_id>"       (no TTL, server-wide)
premieres_digest_last:guild         →   "<SEASON>_<YEAR>"    (no TTL, server-wide)
otaku:schema_version                →   "<version>"          (bootstrap-complete marker)
otaku:schema_v8_migrated            →   "1"                  (v7→v8 migration marker)
otaku:schema_ddl_idx                →   "<n>"                (v10.0.11 bootstrap resume cursor)
otaku:schema_pk_verified            →   "1"                  (v10.0.12 one-time wide-PK check marker)
```

Ephemeral keys (`ctx.ephemeral`, Redis-backed, evictable): `otaku:user:<id>` (2 s command cooldown) and `otaku:airing:<media_id>:<episode>` (24 h announce-once dedup).

KV is per-server and per-plugin, so the same user is tracked independently on each server. The 7-day TTL means an inactive user's cache expires on its own — no explicit cleanup needed. KV is wiped automatically on uninstall.

## SQL schema

Rows are auto-scoped to `ctx.server_id` by the runner, so no table has an explicit `server_id` column. The core tracking table (created `otaku_user_anime` in v2.0.0, renamed and widened in v8.0.0 for manga support):

```sql
CREATE TABLE IF NOT EXISTS otaku_user_media (
  user_id      TEXT NOT NULL,
  media_id     INTEGER NOT NULL,
  media_type   TEXT NOT NULL DEFAULT 'anime',  -- v8.0.0: anime | manga
  status       TEXT NOT NULL,                  -- watching | completed | on_hold | dropped | plan
  is_favorite  BOOLEAN NOT NULL DEFAULT FALSE,
  rating       SMALLINT,                       -- v2.1.0: score × 2 (2..20)
  episodes_watched SMALLINT DEFAULT 0,         -- v2.2.0 (carries chapters for manga rows)
  added_at     TIMESTAMP NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, media_id, media_type)
);
CREATE INDEX IF NOT EXISTS otaku_user_media_user_status_added_idx
  ON otaku_user_media (user_id, status, added_at DESC);

-- v3.0.0:
CREATE TABLE IF NOT EXISTS otaku_server_watchlist (
  media_id   INTEGER NOT NULL,
  added_by   TEXT NOT NULL,
  added_at   TIMESTAMP NOT NULL DEFAULT NOW(),
  note       TEXT,
  PRIMARY KEY (media_id)
);

-- v3.2.0:
CREATE TABLE IF NOT EXISTS otaku_watch_parties (
  party_id   SERIAL PRIMARY KEY,
  media_id   INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  status     TEXT NOT NULL DEFAULT 'active'  -- active | completed | abandoned
);
CREATE TABLE IF NOT EXISTS otaku_watch_party_members (
  party_id          INTEGER NOT NULL,
  user_id           TEXT NOT NULL,
  episodes_watched  SMALLINT NOT NULL DEFAULT 0,
  joined_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  PRIMARY KEY (party_id, user_id)
);

-- v4.0.0:
CREATE TABLE IF NOT EXISTS otaku_notifications (
  user_id    TEXT NOT NULL,
  media_id   INTEGER NOT NULL,
  channel_id TEXT,
  added_at   TIMESTAMP NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, media_id)
);
```

Later versions add: `otaku_reviews` (v7.0), `otaku_aotw_polls` / `otaku_aotw_candidates` / `otaku_aotw_votes` (v7.1), `otaku_polls` / `otaku_poll_options` / `otaku_poll_votes` (v7.2), and `otaku_achievements` (v10.0) — 13 tables across a 16-statement bootstrap. The canonical, ordered list lives in `_SCHEMA_DDL_SEQUENCE` in `__main__.py`.

The DDL is idempotent (`IF NOT EXISTS`) and runs from both `@plugin.on_install` and `@plugin.on_ready`. The `on_ready` path covers pool-mode workers picking up a tenant that upgraded from v1.x (where `on_install` does not re-fire). The host rate-limits DDL (~5 statements/hour per tenant), so bootstrap resumes across worker boots via the `otaku:schema_ddl_idx` KV cursor (v10.0.11) instead of replaying from the top.

## Custom_id namespace

All component custom_ids are prefixed with `otaku:`:

| custom_id | Source | Notes |
|---|---|---|
| `otaku:similar:<media_id>` | `[🔁 Similar]` button on the `/anime` card | Stateless — the media ID is in the ID itself. |
| `otaku:page:<genre>:<sort>:<page>` | `/discover` prev/next buttons | Re-queries AniList on each click. |
| `otaku:trend:<page>` | `/trending` prev/next buttons | Re-queries the current season. |
| `otaku:expand` | List-view select menu | The selected option's `value` is the media ID. |
| `otaku:list:<user>:<scope>:<page>` | `/list` and `/favorites` prev/next buttons | scope is `all`, `favorites`, or one of the watch statuses. |
| `otaku:swl:<page>` | `/server-watchlist view` prev/next | Server watchlist is shared, no user_id in the id. |
| `otaku:wp-join:<party_id>` | `[🎬 Join party]` button on `/wp create` and `/wp status` embeds | One-click join for a watch party. |
| `otaku:premieres:<season>:<year>:<page>` | `/season-premieres` prev/next buttons | Season + year baked in so the same buttons work across multiple seasons in flight. |
| `otaku:mood:<mood>:<page>` | `/mood` prev/next buttons | Curated mood key baked in. |
| `otaku:mpage:<genre>:<sort>:<page>` | `/manga-discover` prev/next buttons | Manga twin of `otaku:page`. |
| `otaku:popchar:<page>` | `/character-popular` prev/next buttons | Rank numbers continue across pages. |
| `otaku:reviews:<media_id>:<page>` | `/reviews` prev/next buttons | Spoiler redaction re-applied per viewer. |
| `otaku:reset-confirm:<user_id>` / `otaku:reset-cancel:<user_id>` | `/otaku-reset` confirmation buttons | User-scoped so only the invoker can confirm. |
| `otaku:aotw-vote:<poll_id>:<media_id>` | `/aotw start` candidate buttons | One active poll per server. |
| `otaku:poll-vote:<poll_id>:<key>` | `/poll create` option buttons (A–D) | Vote upserts on PK (poll_id, user_id). |
| `otaku:review-modal:<media_id>` | `/review` modal (interaction_type 5) | Pre-fills the user's existing review. |
| `otaku:noop:prev` / `otaku:noop:next` | Disabled pagination filler buttons | Never clickable; no route needed. |

## Quick start (development)

```bash
# 1. Clone & install
git clone https://github.com/CubCadetXT1/mmo-maid-plugin-otaku.git
cd mmo-maid-plugin-otaku
# The venv must live OUTSIDE the repo — the validator rejects a top-level .venv/
python -m venv ../mmo-maid-plugin-otaku-venv && source ../mmo-maid-plugin-otaku-venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 2. Local dev loop (hot-reload + mock host)
yourbot dev --watch

# 3. Tests
python -m pytest -q

# 4. Lint (ruff — also runs in CI)
make lint

# 5. Pre-flight validation (also runs in CI). Use make — it cleans first:
#    test runs leave __pycache__/.pytest_cache at the repo top level, which
#    the validator's layout check rejects.
make validate
```

`yourbot dev` fires events from `events.yaml` against a `MockContext`, prints every action the plugin takes, and reloads on file change. See [SDK docs](https://yourbot.gg/dev/docs) for the full developer workflow.

## Release process

Releases are tagged on `main` with semver tags (`v1.2.3`), which triggers `.github/workflows/release.yml` to validate, test, build the upload zip, and attach it to the GitHub release.

```bash
# 1. Bump manifest.json "version" and update CHANGELOG.md
# 2. Verify locally
make release          # validates, tests, builds dist/otaku-<version>.zip

# 3. Commit, tag, push
git commit -am "Release v1.2.3"
git tag v1.2.3
git push && git push --tags
```

The tag's version (`v1.2.3` → `1.2.3`) must match `manifest.json`'s `version` field; CI rejects the release otherwise.

## Submitting for review

The YourBot dev portal links to this repo and pulls a specific tag for review. Review turnaround is typically 1–3 business days. The reviewer checks the manifest, scans for disallowed imports and unparameterised SQL, and re-prompts users on any tier shift.

## License

MIT — see [`LICENSE`](LICENSE).

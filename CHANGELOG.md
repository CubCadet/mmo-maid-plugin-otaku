# Changelog

All notable changes to this plugin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version-bump policy, tied to `manifest.json`:

- **MAJOR** (`1.x.y → 2.0.0`) — added a Dangerous capability, removed a slash
  command, breaking KV/SQL schema change.
- **MINOR** (`1.0.x → 1.1.0`) — new slash command, new event handler, additive
  KV/SQL columns.
- **PATCH** (`1.0.0 → 1.0.1`) — bug fix, internal refactor, docs/CI changes.

The tag in GitHub (`v1.2.3`) must match the `version` field in `manifest.json`.
CI enforces this during release builds.

---

## [Unreleased]

## [7.2.0] - 2026-05-14

### Added — Phase 7 closes
- `/poll` — generic server polls, three subcommands:
  - `/poll create question:<…> a:<…> b:<…> [c:<…>] [d:<…>]`
    (admin only) — 2 to 4 options. Posts an embed with one button per
    option (labels A/B/C/D) and the poll_id in the footer.
  - `/poll status id:<…>` (public) — live standings for any poll
    (active or ended), with a vote-bar indicator.
  - `/poll end id:<…>` (admin only) — closes the poll. No winner
    crowned — /poll is a discussion tool; use /aotw for "winner".
- Three new SQL tables wired into `_bootstrap_schema`:
  `otaku_polls`, `otaku_poll_options`, `otaku_poll_votes`. PK on votes
  is `(poll_id, user_id)`; re-voting UPDATEs the row.
- Vote button routing — `otaku:poll-vote:<poll_id>:<option_key>` runs
  through the existing `_route_components` dispatcher with ephemeral
  vote confirmations.
- Concurrent polls — multiple polls can be active per server at once
  (unlike /aotw which enforces single-active). Each is addressed by
  its `poll_id`.
- Regression file `tests/regression/test_v7_2_0.py` (17 tests) freezes
  the schema bootstrap, the option-key set (`a`/`b`/`c`/`d`),
  POLL_MIN_OPTIONS=2 / POLL_MAX_OPTIONS=4, admin gating on create/end
  (status stays public), option-insert ordering, vote insert/update/
  noop branches, vote-button custom_id shape, and end-of-already-ended
  poll graceful no-op.

### Deviation from the roadmap
- The roadmap mentioned **reactions** for /poll voting. v7.2 ships
  **buttons** instead — every other interactive surface in the plugin
  already uses buttons (cleaner state model, no reaction-event
  capability needed). Notes in CHANGELOG so the reactive variant is on
  record if someone asks.

### Capability surface
- **No new capabilities.** All of Phase 7 added zero capabilities.

## [7.1.0] - 2026-05-14

### Added
- `/aotw` — anime-of-the-week voting, three subcommands:
  - `/aotw start` (admin only) — pulls the top 5 entries from
    `otaku_server_watchlist` by recency, creates a poll, posts an
    embed with 5 numbered vote buttons.
  - `/aotw status` (public) — shows the active poll's candidates and
    live vote counts.
  - `/aotw end` (admin only) — declares the winner, marks the poll
    `status='ended'`, and posts the winner card to the configured
    announcement channel (falls back to the channel where the command
    was run if no announcement channel is set). Ephemeral confirm to
    the admin either way.
- Three new SQL tables wired into `_bootstrap_schema`:
  `otaku_aotw_polls`, `otaku_aotw_candidates`, `otaku_aotw_votes`.
  `otaku_aotw_votes`'s PK `(poll_id, user_id)` enforces one vote per
  user — clicking another candidate UPDATEs the existing row.
- Vote button routing — `otaku:aotw-vote:<poll_id>:<media_id>` runs
  through the existing `_route_components` dispatcher. Ephemeral
  confirmation; live counts surface via `/aotw status`.
- Winner = max votes; tie-break by **lowest media_id** (deterministic,
  no implicit favoritism).
- One active poll per server enforced in `start`; a second
  `/aotw start` while one is open is rejected with a pointer.
- Regression file `tests/regression/test_v7_1_0.py` (17 tests) freezes
  the schema bootstrap, the candidate limit, admin gating on start
  and end (status stays public), the watchlist-min-2 requirement, the
  insert-vs-update vote routing, the tie-break rule, and the
  announce-channel posting target.

### Design notes
- No cron auto-end in v7.1.0. The roadmap mentioned "weekly" but the
  cron-end mechanic adds time-tracking complexity that isn't worth it
  if admins are running these manually anyway. A v7.1.x patch could
  layer it onto the existing v4.0 hourly cron if demand arises.
- The vote-confirmation message is ephemeral and doesn't edit the
  original `/aotw start` embed with new counts — message-edit would
  need to track the message_id of the start embed, which isn't worth
  the complexity given `/aotw status` exists.

### Capability surface
- **No new capabilities.** `discord:send_message` was added in v4.0
  for airing notifications and now also carries the winner
  announcement.

## [7.0.0] - 2026-05-14

### Added — Phase 7 opens
- `/review` slash command — opens a modal pre-filled with the caller's
  existing review (if any) for their cached `last_anime`. Modal has a
  Title (short, max 100) and Body (paragraph, max 2000). Submitting
  upserts a row in the new `otaku_reviews` table.
- `/reviews [anime]` slash command — paginated 3-per-page view of this
  server's reviews for an anime. `anime` accepts a title, numeric
  AniList ID, or is omitted to use the caller's cached `last_anime`.
  Sorted `updated_at DESC` so freshly-edited reviews surface first.
- Modal submit routing — extended `_route_components` to also handle
  `interaction_type == 5` (MODAL_SUBMIT) with prefix `otaku:review-modal:`.
  The custom_id encodes the media_id since Discord modals can't
  receive context other than their custom_id and submitted values.
- Regression file `tests/regression/test_v7_0_0.py` (18 tests) freezes
  the schema bootstrap, the modal custom_id shape, the existing-review
  pre-fill path, the upsert (insert vs. update) routing, and the
  paginated /reviews query contract.

### Changed — schema
- New table `otaku_reviews (user_id, media_id, title, body, created_at,
  updated_at, PK (user_id, media_id))`. Idempotent CREATE wired into
  `_bootstrap_schema`. **MAJOR version bump per the roadmap doctrine**
  — new schema is a schema change even though it's additive.

### Deviation from the roadmap
- The roadmap showed `/review <anime>` with anime as a positional arg.
  Discord's 3-second pre-modal wall clock makes a synchronous AniList
  title lookup unreliable before `send_modal()`, so `/review` is
  cached-`last_anime`-only (same pattern as `/watch`, `/rate`,
  `/progress`). Users who want to review a specific anime do
  `/anime query: <title>` first; the cached AniList lookup is
  in-process and instant for the modal-prefill path. `/reviews` keeps
  the optional anime arg with title-or-ID resolution because it can
  `defer()` before querying.
- An `updated_at` column was added on top of the roadmap's schema so
  re-reviewing reorders the list freshness-first instead of being
  invisible.

### Capability surface
- **No new capabilities.** `storage:sql`, `proxy:http`, and
  `interaction:respond` already covered everything Phase 7 v7.0.0
  needed. (Modals piggy-back on `interaction:respond`.)

## [6.2.0] - 2026-05-14

### Added
- `/genre-trends` — bridges discovery and personalization. Picks the
  caller's top 3 most-tracked genres from a 50-row sample of their
  recent tracker (same heuristic `/stats` uses), then queries AniList
  for currently-trending anime in those genres. Anime the caller
  already tracks (any status) are filtered out so the surface stays
  "new for me." Ephemeral.
- `QUERY_GENRE_TRENDS` GraphQL query — `Page(media: genre_in: $genres,
  sort: [TRENDING_DESC])`, fetched at `perPage=15` so post-filter we
  still have ≥5 fresh picks for the typical user.
- Empty-tracker short-circuits before any AniList call to keep
  newcomers from racking up rate-limited proxy hits.
- Regression file `tests/regression/test_v6_2_0.py` (10 tests) freezes
  the constants (TOP_N=3, FETCH=15, RESULT_LIMIT=5), the genre-ranking
  alpha-tie-break order, the tracked-id exclusion, and the
  short-circuit ordering.

### Capability surface
- **No new capabilities.** Reuses `storage:sql` (tracker reads),
  `proxy:http` (AniList), `interaction:respond` (slash + future
  expand-select).

## [6.1.0] - 2026-05-14

### Added
- `/mood feeling:<one of 10 choices>` — pick a vibe, get a paginated
  list of anime matching it. Ten curated moods ship: `uplifting`,
  `tense`, `cathartic`, `chill`, `epic`, `nostalgic`, `dark`, `funny`,
  `romantic`, `adventurous`. Each is a small AniList genre/tag blend;
  half also carry a tag enrichment (e.g. `chill` → Slice of Life +
  Iyashikei). Footer surfaces the active filters so users understand
  the recommendation.
- Same paginated-list shape as `/discover` — prev/next via
  `otaku:mood:<feeling>:<page>` plus an expand-select to dive into any
  result.
- Two GraphQL shapes (`QUERY_MOOD_WITH_TAGS`, `QUERY_MOOD_GENRE_ONLY`)
  because AniList rejects empty in-filter lists. When a mood has tags
  the tagged query runs first; if it returns zero matches we transparently
  fall back to genres-only so a fragile or missing tag never strands the
  user.
- Regression file `tests/regression/test_v6_1_0.py` (11 tests) freezes
  the mood set, the table shape, the genres-only routing for tag-less
  moods, the empty-tags fallback, and the pagination custom_id contract.

### Deviation from the roadmap
- The roadmap called for `moods.json` as the mapping config. The runtime
  upload allowlist (same one that blocked v1.4's sibling `strings.py`)
  rejects extra top-level `.json` files, so the MOODS table lives inline
  in `__main__.py` like the v1.4 `S` namespace. v9 localization can swap
  this out per-locale the same way it'll swap `S`.
- "Weighted blend" landed as **union semantics** rather than literal
  weighting — AniList's `genre_in`/`tag_in` filters are themselves
  OR-within-array, AND-across-arrays. The "blend" is now: a curated set
  of genres any of which matches, optionally enriched by a tag filter
  layered on top. True weighted scoring (e.g. boost results that match
  multiple genres) would need post-query reranking and was out of scope
  for this slice — noted for a possible v6.1.x or Phase 9 enhancement.

### Capability surface
- **No new capabilities.** Reuses `proxy:http` for AniList and
  `interaction:respond` for the slash command and pagination buttons.

## [6.0.0] - 2026-05-14

### Added
- `/recommend` — personalized anime recommendations driven by
  collaborative filtering over this server's rated rows. Vector is each
  user's `rating / 2.0` on the 0.5–10.0 scale; candidate score is
  `Σ cosine_sim(target, peer) × peer_rating` across peers that share
  ≥3 rated titles with the target. Peers capped at 50 (random sample
  when more). Top 5 results shown ephemerally, annotated with the peer
  count supporting each pick.
- Fallback to AniList `/similar` (seeded by the target's highest-rated
  tracked anime, falling through to newest favorite, then newest
  tracked) when the target has <3 ratings OR no peer overlaps ≥3
  titles. The reason is surfaced in the embed body so users know why
  they got the AniList path.
- Regression file `tests/regression/test_v6_0_0.py` freezes the
  algorithm constants, the cosine helper, and the candidate-exclusion
  contract.

### Deviation from the roadmap
- The roadmap's v5.2 iframe upgrade and v5.3.x charts stay deferred —
  the same SDK manifest-vs-iframe exclusivity that blocked them at the
  end of Phase 5 still applies. This MAJOR bump is purely the Phase 6
  kick-off (collaborative filtering), not the dashboard-mode swap. The
  swap waits until charts/time-series become user-visible priorities.
- v6.0 ships with **no new capabilities** — `storage:sql` already
  covers everything the algorithm needs. That keeps the marketplace
  review surface area unchanged.

### Note — Phase 5 closure (carried forward from v5.1.0)
- Phase 5 closes at v5.1.0. v5.2 (iframe upgrade) and v5.3.x (charts &
  trends) are deferred to a future MAJOR bump: the SDK requires
  picking ONE dashboard mode, and switching from manifest to iframe
  breaks the v5.0 regression contract. v6.0.0 does **not** make that
  switch — see "Deviation from the roadmap" above.

## [5.1.0] - 2026-05-15

### Added
- `/my-stats` — richer self-view than `/stats`. Adds three list sections
  on top of the aggregate fields:
  - **Top rated** (your 5 highest-scored anime, with rating shown)
  - **Top favorites** (your 5 most-recent favorites)
  - **Recently completed** (your 5 most-recent completions)
- Completion percentage now displayed alongside the completed count.
- One AniList batch HTTP call resolves titles for everything that lands
  in the embed (cached via the existing 5-min cache).
- Regression file `tests/regression/test_v5_1_0.py`.

## [5.0.0] - 2026-05-15

### Added
- **Plugin dashboard (manifest mode).** New `dashboard_manifest.json`
  with two pages:
  - **Overview** — four stat cards (tracked rows, active users in 30
    days, episodes watched, airing subscriptions), one bar chart (watch
    status distribution), one table (top 5 tracked anime).
  - **Settings** — form with a `channel` field that mirrors
    `/otaku-admin set-channel`, so admins can point airing pings without
    leaving the dashboard.
- Eight new `@plugin.on_dashboard` handlers:
  `get_total_tracked`, `get_active_users_30d`, `get_total_episodes`,
  `get_total_subscriptions`, `get_status_distribution`, `get_top_tracked`,
  `get_settings`, `save_settings`.
- Each handler is single-query SQL with `<10s` budget (per dashboard
  contract). The `get_top_tracked` table makes one AniList batch call to
  resolve titles, cached via the existing in-process AniList cache.
- Regression file `tests/regression/test_v5_0_0.py`.

### Changed
- No new capability. The dashboard reads through `storage:sql` and
  `storage:kv`, both already declared.
- v5.0.0 bumps the MAJOR version even though no behavior changes for
  slash users — adding a dashboard is a new surface area.

### Note on the roadmap "mean score by genre" widget
- Roadmap listed "Mean score by genre" as a v5.0 chart. Implementing it
  needs genre data per media (we only have `media_id` in SQL), which
  would mean ~50 AniList lookups on every dashboard load. Deferred to
  v5.3.x (charts & trends) where caching genre data is in scope.

## [4.2.0] - 2026-05-15

### Added
- `/season-premieres [season] [year]` — paginated browse of upcoming
  premieres. Defaults to next season when neither arg is passed.
- Automatic weekly seasonal digest: the v4.0 hourly cron now also calls
  `_dispatch_premieres_digest()`. During the first 7 days of each new
  season, it posts a top-5 premieres embed to the per-server
  announcement channel (if one is configured). KV-deduped per season per
  server at `premieres_digest_last:guild`.
- New helpers `_next_season()`, `_current_season_at(now)`,
  `_season_is_fresh(now)` for testable season-boundary logic.
- Pagination button `otaku:premieres:<season>:<year>:<page>`.
- Regression file `tests/regression/test_v4_2_0.py`.

### Note
- v4.1.0 stays vacant (per v4.0.0's note — its admin-channel scope was
  rolled forward into v4.0.0).

## [4.0.0] - 2026-05-15

### Added
- **New capability:** `discord:send_message` (Risky tier — re-triggers
  marketplace human review).
- `/notify <anime>` — subscribe to airing notifications for an anime.
  Stores the channel where the subscription was made, so the cron can
  fall back to that channel if no server-wide announce channel is set.
- `/unnotify <anime>` — remove a subscription.
- `/notify-list` — ephemeral list of your subscriptions, with the
  next-episode ETA pulled live from AniList.
- `/otaku-admin set-channel [channel]` — admin-only. Sets the
  per-server announcement channel for airing pings. Omit the channel
  arg to clear.
- New SQL table `otaku_notifications (user_id, media_id, channel_id,
  added_at; PK (user_id, media_id))`.
- New KV key `notify_channel:guild` for the per-server announce channel.
- `@plugin.cron("5 * * * *")` — hourly airing check that polls AniList's
  `Page.airingSchedules(airingAt_greater, airingAt_lesser)` for a
  75-minute window (slightly wider than the cron interval to absorb
  delay), then dispatches `discord.send_message` to each subscriber's
  target channel.
- Ephemeral dedup at `otaku:airing:<media_id>:<episode>` (24h TTL)
  ensures a single airing only pings once.
- Regression file `tests/regression/test_v4_0_0.py`.

### Changed
- Manifest description + tags reflect the notifications surface.

### Migration
- Existing v3.x installs will be re-prompted to grant `discord:send_message`
  on next interaction. Until granted, `/notify` still records subscriptions
  but the cron can't post pings.

### Roadmap deviation
- The ROADMAP originally split this work: `v4.0 = per-user DM pings`,
  `v4.1 = server announcement channel`. Two SDK gaps motivated rolling
  v4.1's work forward into v4.0:
  1. The SDK has no DM helper — `ctx.discord.send_message` only takes a
     `channel_id`. Channel pings with @mentions deliver the same UX in
     a way the SDK supports cleanly.
  2. The server-side cron manifest declaration is documented as
     "consult the dev portal docs," which aren't bundled with this skill.
     We use `@plugin.cron` (which works in single-tenant deployments) and
     don't fire in pool mode. The roadmap was clear about this risk.
- v4.1 is now vacant; v4.2 (seasonal premieres digest) is unchanged.

## [3.3.0] - 2026-05-15

### Added
- `/leaderboard [metric]` — server-wide top-10 board across three metrics:
  - `completed` (default) — most rows with `status='completed'`
  - `score` — highest mean rating (gated to users with ≥ 3 rated rows)
  - `hours` — most episodes (rendered as hours via the 24min/ep heuristic)
- Medals 🥇🥈🥉 on the top three rows.
- Regression file `tests/regression/test_v3_3_0.py`.

## [3.2.0] - 2026-05-15

### Added
- `/wp create anime:<title>` — start a watch party. Returns a public embed
  with a `[🎬 Join party]` button so anyone in the channel can join with
  one click.
- `/wp join id:<n>` — manually join by party id.
- `/wp status id:<n>` — public embed listing members and their progress.
- `/wp progress id:<n> episode:<n>` — update your episode count. If every
  member is at the same episode, a public "everyone reached episode N"
  announcement fires. If everyone is at the total, the party is
  auto-promoted to `status='completed'`.
- Two new SQL tables: `otaku_watch_parties (party_id SERIAL PK, media_id,
  created_by, created_at, status)` and `otaku_watch_party_members (party_id,
  user_id, episodes_watched, joined_at; composite PK)`.
- `otaku:wp-join:<party_id>` button dispatch.
- Regression file `tests/regression/test_v3_2_0.py`.

## [3.1.0] - 2026-05-15

### Added
- `/compare user:<user>` — side-by-side comparison of two users' anime
  tracking on this server. Four sections:
  - **Tracked totals** (yours / theirs / both)
  - **Shared favorites** — anime you both marked as favorite
  - **You disagree on** — overlap where ratings differ by ≥ 2 points
  - **Anime they've completed (and you haven't)** — soft recs
- Each section capped at 5 entries.
- One AniList batch HTTP call per `/compare` invocation to resolve titles.
- Regression file `tests/regression/test_v3_1_0.py`.

### Changed
- Comparing against yourself short-circuits ephemerally before any SQL runs.

## [3.0.0] - 2026-05-15

### Added
- `/server-watchlist view` — public, paginated browse of the server's
  curated anime watchlist.
- `/server-watchlist add anime:<title> [note:<text>]` — admin-only.
  Accepts a title (AniList search) or a numeric AniList media ID.
- `/server-watchlist remove anime:<title|id>` — admin-only.
- New SQL table `otaku_server_watchlist (media_id PK, added_by, added_at,
  note)`. Idempotent CREATE in `_bootstrap_schema`.
- Pagination buttons use `otaku:swl:<page>` custom_ids.
- Regression file `tests/regression/test_v3_0_0.py`.

### Changed
- Admin gating reuses the `_caller_is_admin` helper that landed in
  v2.6.0; no new capability needed.

### Migration
- None. `otaku_server_watchlist` is created on the next event after
  upgrade (via the existing `on_ready` bootstrap).

## [2.6.0] - 2026-05-15

### Added
- **New capability:** `discord:read` (Safe tier — no plugin-tier change,
  but existing installs will see a one-time permission prompt before
  `/otaku-admin` works).
- `_caller_is_admin(ctx, user_id)` helper — checks the guild owner via
  `ctx.discord.get_guild()`, then the caller's roles via
  `ctx.discord.get_member()` against `ctx.discord.list_roles()` for the
  ADMINISTRATOR (0x8) or MANAGE_GUILD (0x20) permission bits.
- 5-minute per-server in-process cache for `list_roles()` so admin
  checks don't hammer the Discord API.
- `/otaku-admin reset-user <user>` — server-admin-only moderation
  command. Deletes every tracked row for the target user.
- Regression file `tests/regression/test_v2_6_0.py`.

### Changed
- v2.5.0's note that admin moderation was deferred no longer applies —
  this version closes it. The helper here is the same one Phase 3's
  `/server-watchlist add/remove` will reuse.

### Migration
- Existing v2.x installations will be re-prompted to grant `discord:read`
  on next interaction (the runner blocks the new API calls until
  granted). `/otaku-admin` won't function until that grant lands; the
  rest of the plugin keeps working.

## [2.5.0] - 2026-05-15

### Added
- `/otaku-reset` — self-service data deletion. Surfaces a confirm prompt
  with a destructive-style "🗑 Yes, delete it all" button and a cancel
  option. Custom_id encodes the original caller's id so a different user
  clicking the button can't trigger the delete.
- `/anime`, `/random`, and the expand-from-list select now show the
  user's rating ("🎯 8.5/10") alongside the progress field when the user
  has rated the anime.
- New helper `_get_user_tracking(ctx, user_id, media_id)` returns
  `(episodes, rating)` in a single SELECT; old `_get_user_progress` is
  kept as a thin shim.

### Note on roadmap deviation
- v2.5.x in ROADMAP.md called for `/otaku-admin reset-user @user`
  (server-admin-only moderation). The SDK's `interaction_create` event
  doesn't carry caller permissions; gating to admins would require
  `discord:read` plus a Discord API call, a meaningful capability bump
  mid-phase. The self-service `/otaku-reset` ships the GDPR-friendly
  half of that work; admin moderation is deferred to Phase 4 or later
  when other Discord caps land.

## [2.4.0] - 2026-05-15

### Added
- `/import anilist <username>` — bulk-imports an AniList user's anime list
  into the caller's tracker. Streams 50 entries per page; aborts cleanly
  if a page comes back malformed mid-stream and tells the user how many
  rows landed. Hard cap at 100 pages (5000 anime).
- AniList status mapping (`CURRENT/REPEATING → watching`, `COMPLETED →
  completed`, etc.) and `score(format: POINT_10)` → our SMALLINT rating
  via score × 2.
- Re-imports update `status`, `episodes_watched`, and `rating` only; the
  `is_favorite` flag is preserved (AniList doesn't model it the same way).
- Regression file `tests/regression/test_v2_4_0.py`.

## [2.3.0] - 2026-05-15

### Added
- `/stats [user]` — aggregate per-user stats: total tracked, counts by
  status (watching/completed/dropped/on_hold/plan), total episodes,
  estimated hours (24 min/episode heuristic), mean rating, and top
  genre (sampled from the 50 most-recently-added anime).
- New helpers `_aggregate_user_stats` and `_top_genre_for_user`. The
  aggregate query is a single `GROUP BY status` over the existing
  composite index — no schema change needed.
- Regression file `tests/regression/test_v2_3_0.py`.

## [2.2.0] - 2026-05-15

### Added
- `/progress <episodes>` — record episodes watched for the user's last
  `/anime` lookup. Validates against the anime's total episode count
  (caps over-cap input and warns the user). Auto-promotes status to
  `completed` when `episodes_watched == total`.
- Schema: `ALTER TABLE otaku_user_anime ADD COLUMN IF NOT EXISTS
  episodes_watched SMALLINT DEFAULT 0`.
- `/anime`, `/random`, and the expand-from-list select now show a
  "Your progress" field on the card when the user has progress > 0.
- Regression file `tests/regression/test_v2_2_0.py`.

## [2.1.0] - 2026-05-15

### Added
- `/rate <score>` — rate the user's last `/anime` lookup on a 1.0–10.0 scale,
  half-points allowed. Stored as `SMALLINT` (score × 2) in a new
  `rating` column.
- `/ratings [user]` — list a user's rated anime, top 25 by score.
- Schema: `ALTER TABLE otaku_user_anime ADD COLUMN IF NOT EXISTS rating SMALLINT`.
  Wired into `_bootstrap_schema` so it runs from both `on_install` and
  `on_ready`. Additive — no MAJOR bump needed.
- Regression file `tests/regression/test_v2_1_0.py`.

### Fixed
- **regression-fix:** `tests/regression/test_v2_0_0.py::test_schema_ddl_idempotent`
  used to assert `len(executed) == 4`, which over-specified the contract by
  locking the exact DDL count. The real contract is "calling twice does not
  raise." Now asserts the recorder doubles, which holds across additive
  schema changes. See the inline comment.

## [2.0.0] - 2026-05-15

### Added
- **New capability:** `storage:sql` (Risky tier — staff-reviewed). Triggers
  marketplace re-review.
- New SQL table `otaku_user_anime` (`user_id`, `media_id`, `status`,
  `is_favorite`, `added_at`) plus composite index on
  `(user_id, status, added_at DESC)`. Auto-scoped to `server_id` by the runner.
- `@plugin.on_install` and `@plugin.on_ready` both run a shared
  `_bootstrap_schema(ctx)` so v1.x → v2.0.0 upgrades on pool-mode workers
  bootstrap before the first event handler.
- `/favorite [anime] [remove]` — toggle a favorite for the user's last
  `/anime` lookup or for an explicit title.
- `/favorites [user]` — paginated list of favorites, optionally for another
  in-server user.
- `/watch <status>` — set watch status (`watching | completed | on_hold |
  dropped | plan`) for the user's last lookup.
- `/list [status] [user]` — paginated tracker view. Status filter optional;
  defaults to "all". Reuses the existing `_page_buttons` + select-row
  affordances.
- New AniList batch query `Page.media(id_in: [...])` so one HTTP call
  fetches every title on a `/list` page.
- Status emojis on list rows: 📺 watching, ✅ completed, ⏸ on_hold,
  ❌ dropped, 📌 plan, ⭐ favorite.
- Regression file `tests/regression/test_v2_0_0.py` freezes the new schema,
  manifest entries, and pagination custom_id format.

### Changed
- Manifest description updated to mention tracking alongside discovery.
- `tags` now includes `"tracking"`.

### Migration notes
- **No data migration required.** v1.x had no SQL. On first event after the
  upgrade, `@plugin.on_ready` creates the table.
- The KV key `last_anime:user:<id>` is preserved verbatim — old caches keep
  driving `/similar`, `/favorite`, and `/watch` when called without an
  `anime:` arg.
- Privacy: `/list user:@them` is public-within-server by design. Explicit
  opt-out (`/otaku-privacy hide`) lands in v2.5.x per ROADMAP.md.

## [1.5.0] - 2026-05-14

### Added
- Ruff (`ruff>=0.4`) wired into dev deps, the Makefile (`make lint`), and the
  `ci.yml` workflow. Runs on every PR and push.
- `pyproject.toml` with a minimal `[tool.ruff]` config (line-length 120,
  target py310).

### Changed
- Type-hint completeness verified across every top-level function in
  `__main__.py`.
- README quickstart now mentions `make lint`.
- Makefile `release` target depends on `lint validate test` (was
  `validate test`).

### Note on `ruff format`
- The roadmap also called for `ruff format`. Running it produced a 597-line
  diff that would inline our multi-line GraphQL query strings and collapse
  several intentionally vertical blocks. Per the roadmap's failure-mode
  warning ("review manually if formatting touches >50 lines"), we kept
  `ruff check` in CI and skipped `ruff format` on existing code. New code
  is welcome to follow ruff format.

## [1.4.0] - 2026-05-14

### Changed
- Every user-facing string is now defined as a constant on a single
  `_Strings` class (aliased `S`) at the top of `__main__.py`. This is the
  i18n-ready structural separation called for in the roadmap. A future
  localization layer (planned for Phase 9) can swap `S` per-locale.

### Note
- The roadmap originally planned a sibling `strings.py` module. The plugin
  upload zip's runtime allowlist (`manifest.json`, `__main__.py`,
  `requirements.txt`, `dashboard_manifest.json`, `dashboard/`) doesn't
  accept additional `.py` files, so the structural separation lives inside
  `__main__.py` instead. Behavior is unchanged.

## [1.3.0] - 2026-05-14

### Added
- `/help` — lists every command in `manifest.json` with a one-line
  description and an example. Generated from the manifest at boot so it
  cannot drift behind newly-registered commands.
- `/genres` — shows AniList's canonical genre list. Cached in KV at
  `genres:global` for 24h, with a live HTTP fallback on cache miss.
- Regression test file `tests/regression/test_v1_3_0.py`.

### Changed
- KV key conventions doc: `genres:global` is now a documented key.

## [1.2.0] - 2026-05-14

### Added
- Automatic retry (up to 2 retries with 0.5s + 1.5s exponential backoff) for
  AniList calls that raise `RpcTimeoutError` or return a 5xx status. Total
  retry budget ~2 seconds, well under the 15-minute followup window.
- User-fixable AniList GraphQL errors (e.g. "Query must contain at least 3
  characters") are now surfaced verbatim to the user instead of the generic
  fallback line.

### Changed
- All generic AniList failure messages now include actionable suggestions:
  "try again in a moment, or try a different keyword."
- `RateLimitError` is still **not** retried — the client backs off via the
  next user request.

## [1.1.0] - 2026-05-14

### Added
- `/random [genre]` — rolls a single random anime, optionally constrained to
  a genre. Falls back to page 1 if the random roll lands on an empty page in
  a niche genre.
- `/character <query>` — looks up an AniList character by name. Shows native
  + romaji name, image, description, and the top 5 media they appear in.
  First match only (noted in the footer).
- Regression test file `tests/regression/test_v1_1_0.py` locking in both
  new commands.

## [1.0.2] - 2026-05-14

### Added
- In-process AniList response cache (5-minute TTL, bounded to 128 entries).
  Cuts repeat HTTP traffic for popular queries.
- `/anime` lookups (normalized to lowercase) cache by query string.
- `/discover` page 1 results cache by `(genre, sort)`.
- `/trending` page 1 results cache by `(season, year)`.

### Changed
- `/similar` is deliberately left uncached.
- Cache writes are wrapped so a failure in the cache path never raises into
  the request handler.

## [1.0.1] - 2026-05-14

### Changed
- `plugin.run()` is now the unconditional last line of `__main__.py`, matching
  the mmo-maid-plugins skill's non-negotiable rule #1. Tests set
  `OTAKU_SKIP_RUN=1` before importing to avoid blocking on the RPC loop.

### Fixed
- LICENSE file: filled in the MIT template placeholders (`<YEAR>`,
  `<COPYRIGHT HOLDER>`) so the notice is valid.

### Added
- Regression test suite (`tests/regression/test_v1_0_0.py`) — the immutable
  v1.0.0 behavior contract.
- Hardening tests for `/anime`: ephemeral error on `RpcTimeoutError`,
  graceful handling of AniList `{errors:[...]}` payloads, and an explicit
  cap of 5 genres on the anime card.

## [1.0.0] - 2026-05-14

### Added
- Initial release.
- `/anime <query>` — search AniList for an anime by title; rich embed with cover, score, format, episodes, status, season, genres, description, and a link to AniList. Buttons: `[🔁 Similar]` and `[🌐 Open on AniList]`. Caches the result as the user's "last anime" for 7 days.
- `/discover <genre> [sort]` — browse a genre with `popular` (default), `trending`, or `score` sort. Paginated list of 5 results with prev/next buttons and a select menu to expand any pick.
- `/trending` — top 5 trending anime for the current season (Winter/Spring/Summer/Fall), same paginated style as `/discover`.
- `/similar [anime]` — top 5 AniList recommendations for a title, or for the user's last cached `/anime` lookup if no argument is given.
- Per-user 2-second cooldown via `ctx.ephemeral.cooldown_set` to be polite to AniList's ~90 req/min global soft limit.
- Capabilities: `interaction:respond`, `proxy:http`, `storage:kv`. Proxy domain: `graphql.anilist.co`.
- KV convention: `last_anime:user:<discord_user_id>` → AniList media ID, 7-day TTL.

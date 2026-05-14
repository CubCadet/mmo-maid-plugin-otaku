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

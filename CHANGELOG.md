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

## [10.0.4] - 2026-05-14

### Final audit cleanup — defensive bounds + test hardening

Closes the last 🟡 findings from the v10.0.3 audit. Three small fixes;
no new user-visible behavior; no schema changes; no new capabilities or
proxy domains.

### Added
- **`RECOMMEND_VECTOR_LIMIT = 5000`** module-level constant. Bounds the
  number of rows `_recommend_user_vector` loads for the target user, so
  a pathological account with 10k+ ratings can't blow worker memory or
  cause an O(N) hit on every cosine intersection. Typical users (100–500
  ratings) never hit the cap.

### Fixed
- **`_recommend_user_vector` defensive LIMIT.** Added
  `ORDER BY rating DESC, media_id LIMIT RECOMMEND_VECTOR_LIMIT` to the
  SELECT. When the cap kicks in, the highest-confidence rows survive
  truncation so cosine norms and shared-title intersections still
  weight the user's strongest signals.

### Changed
- **Hardened multi-row INSERT test assertions** in
  `tests/regression/test_v10_0_3.py`. v10.0.3's tests asserted literal
  `"($1, $2, $3)"` substrings; v10.0.4 swaps them for a
  whitespace-agnostic `re.findall(r"\$\d+", sql)` placeholder count.
  The contract is "N placeholders in $1..$N order," not "the SQL
  contains these exact substrings" — a future formatter change can no
  longer silently break the assertion without breaking functionality.

### Tests
- 5 new immutable contracts in `tests/regression/test_v10_0_4.py`:
  - `_recommend_user_vector`'s SQL includes `LIMIT RECOMMEND_VECTOR_LIMIT`.
  - `ORDER BY rating DESC, media_id` is preserved (deterministic
    truncation under the cap).
  - `RECOMMEND_VECTOR_LIMIT` is a module-level int in the sane range
    `[1000, 50000]`.
  - Vector building succeeds end-to-end with realistic mock data.
  - `test_v10_0_3.py`'s placeholder assertions now use `re.findall`
    rather than literal substrings (meta-test guarding the hardening).
- Suite total: 602 tests (was 597), all green.

## [10.0.3] - 2026-05-14

### Audit cleanup — efficiency + stale-code

Closes the remaining 🟡 findings from the v10.0.2 audit. Four small
fixes; no new user-visible behavior; no schema changes; no new
capabilities or proxy domains.

### Performance
- **Hoisted target_norm out of `_recommend_candidates` peer loop.**
  `_cosine_similarity` gained an optional `target_norm` kwarg so the
  caller can supply a pre-computed L2 norm; `_recommend_candidates` now
  computes it once outside the per-peer loop. Latent O(N×M) was
  bounded at ~15k ops for typical users; this patch makes the cost
  truly O(N+M) and protects against pathological 10k+-rating users.
- **Multi-row INSERT for poll options.** `_cmd_poll_create` collapses
  the per-option loop into one `INSERT ... VALUES ($1, $2, $3), ($4, $5, $6), ...`
  statement (POLL_MAX_OPTIONS = 4 bounds the placeholder count).
- **Multi-row INSERT for AOTW candidates.** `_cmd_aotw_start` collapses
  the per-candidate loop into one multi-row VALUES INSERT
  (AOTW_CANDIDATE_LIMIT = 5 bounds the placeholder count).

### Fixed
- **Stale `# Page 2 should bypass the cache` comment** in
  `tests/test_plugin.py:2651`. v10.0.1 made every page cacheable;
  the comment was misleading future maintainers. The test's `+ 1`
  assertion still holds (first click on page 2 is a cache miss — a
  new cache key), only the framing changed.

### Removed
- **`_Strings.FIND_PAGE_MALFORMED`** — orphan scaffolding string never
  referenced anywhere. Declared as a stub for /find pagination that
  v9.0 never shipped. Removing it eliminates dead code; if pagination
  lands later, the string can be reintroduced alongside its call site.

### Doctrine carve-outs
- `tests/regression/test_v7_1_0.py::test_start_inserts_candidates_and_posts_buttons`
  — asserted N separate INSERT statements; adapted to one multi-row
  INSERT with all candidates' params, in order. `# regression-fix (v10.0.3):`
  comment documents.
- `tests/regression/test_v7_2_0.py::test_create_inserts_options_in_order`
  — same shape adaptation for poll options. `# regression-fix (v10.0.3):`
  comment documents.

### Tests
- 6 new immutable contracts in `tests/regression/test_v10_0_3.py`:
  - `_cosine_similarity(target_norm=...)` accepts pre-computed norm and
    returns the same numerical result as the default path.
  - Default signature preserved (no kwarg → same v6 behavior).
  - `_recommend_candidates` produces correct candidate set + peers_kept
    with the hoisted norm.
  - `/poll create` issues exactly one multi-row INSERT containing all
    options in declaration order.
  - `/aotw start` issues exactly one multi-row INSERT containing all
    candidates in order.
  - `_Strings.FIND_PAGE_MALFORMED` no longer exists (orphan removal
    guard).
- Suite total: 597 tests (was 591), all green.

## [10.0.2] - 2026-05-14

### Concurrency fix — cross-user achievement leak

Post-v10.0.1 audit caught a thread-safety bug in the new achievement
stats cache. The SDK runs `MMO_SDK_DISPATCH_THREADS` dispatcher threads
per worker (default 4), and v10.0.1's module-global `_ACH_STATS = {"current": ...}`
dict was shared across them. Two overlapping `/achievements` calls on
different users could collide:

1. Thread A enters `_ach_stats_scope` on user A → writes A's stats to
   `_ACH_STATS["current"]`, sets `self._owns = True`.
2. Thread B enters `_ach_stats_scope` on user B → reads "current is
   not None" (A's stats), sets `self._owns = False`, reuses A's data.
3. All of B's predicates run against A's totals → B is awarded A's
   achievements.

### Fixed
- **Per-thread achievement stats cache.** `_ACH_STATS` dict replaced by
  `_ach_stats_tls = threading.local()`. Each dispatcher thread now owns
  its own `current` attribute; no cross-thread visibility. Reentrant
  semantics on a single thread are preserved.
- **New accessors `_ach_stats_current()` / `_ach_stats_set()`** replace
  direct dict reads. Used internally by `_ach_count_rows`,
  `_ach_count_reviews`, `_ach_count_subs`, and `_ach_stats_scope`;
  tests + future tooling should also go through them.

### Doctrine carve-outs
- `tests/regression/test_v10_0_1.py` — 3 tests that asserted
  `p._ACH_STATS.get("current") is None` / `p._ACH_STATS["current"]`
  now go through `p._ach_stats_current()`. Each carries an explicit
  `# regression-fix (v10.0.2):` comment. Same semantic checks
  (None outside scope, dict inside scope) — only the access path
  changed, since `threading.local()` is an object, not a dict.

### Tests
- 5 new immutable contracts in `tests/regression/test_v10_0_2.py`
  pinning the thread-safety fix:
  - Two overlapping threads on different users do NOT see each
    other's stats (the v10.0.1 bug, now blocked).
  - A scope on the main thread is invisible to a freshly-spawned
    child thread (the `threading.local()` invariant).
  - Accessor API contract: `_ach_stats_current()` returns None
    initially; `_ach_stats_set()` round-trips through `_ach_stats_current()`.
  - Single-thread reentrancy preserved verbatim (sanity check).
- Suite total: 591 tests (was 586), all green.

## [10.0.1] - 2026-05-14

### Post-audit safety + performance patch

Closes the four 🟡 findings from the v10.0.0 audit. Zero new user-visible
surfaces, zero capability changes, zero schema migrations. One safety fix
(TOCTOU on `/review` modal submits) plus three perf fixes that collapse
N+1 query patterns and broaden the AniList in-process cache. Same outputs
on every observable behavior — only the underlying SQL/cache shape
changes.

### Fixed
- **TOCTOU on review upsert (`_upsert_review`).** The v7 doctrine read the
  existing row first and then INSERT-or-UPDATEd; two concurrent modal
  submits for the same `(user, media)` could both observe "no existing"
  and race the INSERT, surfacing IntegrityError out of
  `_handle_review_submit`. Collapsed into a single
  `INSERT ... ON CONFLICT (user_id, media_id) DO UPDATE ... RETURNING
  (xmax = 0)` query. The new-vs-updated distinction (used to pick
  `REVIEW_SAVED_NEW` vs `REVIEW_SAVED_EDIT`) is preserved via the xmax
  predicate.

### Performance
- **Batched peer-vector fetch in `_recommend_candidates`.** Replaced 50
  sequential SELECTs (one per peer) with a single
  `WHERE user_id = ANY($1::TEXT[])` query, hydrating every peer's rating
  vector in one round-trip. New helper `_recommend_peer_vectors_batch`.
- **Batched achievement aggregates.** `/achievements` used to fan out 16
  COUNT queries per call (10 predicates + 6 progress lines). Added
  `_ach_load_stats` — one FILTER aggregate over `otaku_user_media` for
  total/favorites/completed/rated, plus one paired subquery for
  reviews + subscriptions — and `_ach_stats_scope` (reentrant context
  manager) that pre-loads stats once per `cmd_achievements` call so
  predicates and progress lines read from the cached row. Outside the
  scope, helpers fall back to per-call SQL so ad-hoc callers (tests,
  future integrations) keep their existing behavior.
- **Pagination cache coverage.** Every paginated `_render_*` now passes
  `cache=True` to `_anilist_query` unconditionally (was `cache=(page == 1)`
  in 7 call sites). The cache key already varies by `page`, so each page
  gets its own 5-minute-TTL entry; the `ANILIST_CACHE_MAX_ENTRIES = 256`
  LRU cap bounds memory. Covers `_render_discover`, `_render_trending`,
  `_render_character_popular`, `_render_manga_discover`,
  `_render_premieres`, and `_search_by_genre_tag_blend` (used by `/mood`
  + `/find`).

### Doctrine carve-outs
v10.0.1 is a PATCH but adapts 7 regression assertions because they pinned
implementation details (SQL emission shape, cache flag) that the perf
fixes legitimately change. Business behavior is preserved verbatim;
each adapted test carries an explicit `# regression-fix (v10.0.1):`
comment explaining what changed and why.

- `tests/regression/test_v7_0_0.py`:
  `test_modal_submit_inserts_new_row`,
  `test_modal_submit_updates_existing_row` — switched from asserting
  separate INSERT/UPDATE execute calls to asserting one combined
  `ON CONFLICT` upsert query.
- `tests/regression/test_v6_0_0.py`:
  `test_candidates_excludes_target_tracked_ids`,
  `test_candidates_drop_peers_below_min_shared` — mocks updated to
  return one batched response (rows tagged with `user_id`) instead of
  a per-peer iterator.
- `tests/regression/test_v10_0_0.py`:
  `test_check_and_award_inserts_only_newly_met_achievements`,
  `test_check_and_award_uses_idempotent_insert` — mocks updated to
  return the new aggregate-row shape (`{"total", "favorites",
  "completed", "rated"}` plus `{"reviews", "subs"}`).
- `tests/regression/test_v8_3_0.py`:
  renamed `test_character_popular_uses_first_page_cache` →
  `test_character_popular_caches_every_page`; assertion flipped from
  "page 2+ should NOT cache" to "every page caches."

### Tests
- 17 new immutable contracts in `tests/regression/test_v10_0_1.py`
  pinning the four fixes (TOCTOU collapse, batch peer vectors,
  scope-cached achievement aggregates, all-page pagination cache).
- Suite total: 586 tests (was 569), all green.

## [10.0.0] - 2026-05-14

### Phase 10 — Maturity & marketplace-featured release

The deliberate **growing → running** pivot per ROADMAP. v10.0 ships three
maturity slices in one tag: gamification (achievements), localization
(real i18n built atop the v1.4 strings table + v9.3 language preference),
and accessibility (embed-description audit). Monetization-ready and
marketplace submission are documented deferrals — both blocked on
infrastructure the SDK doesn't yet expose.

### Added — Achievements (gamification)
- New SQL table `otaku_achievements (user_id, achievement_key, awarded_at,
  PRIMARY KEY (user_id, achievement_key))`. Bootstrapped idempotently
  via `_SCHEMA_ACHIEVEMENTS_DDL` in `_bootstrap_schema`.
- New slash command `/achievements [user]` — displays earned +
  in-progress achievements for self or another server member. Runs every
  predicate on access, awards newly-met ones via `INSERT ON CONFLICT DO
  NOTHING`, surfaces "✨ newly earned" line when applicable.
- `ACHIEVEMENTS` registry — 10 starter entries:
  - `first_anime` — Look up your first anime via `/anime`.
  - `first_favorite` — Add your first favorite.
  - `first_review` — Submit your first review.
  - `completed_10` / `completed_50` — Mark N anime completed.
  - `rated_25` / `rated_100` — Rate N anime.
  - `community` — Write 5 reviews.
  - `seasonal_subscriber` — Subscribe to airing pings for 3+ anime.
  - `polyglot` — Set a language preference.
- Detection runs **lazily** on `/achievements` access (zero per-handler
  overhead on /rate, /watch, /progress, etc.). Predicates are pure SQL/
  KV reads guarded by try/except so any one raising can never strand the
  handler. Outer `_get_user_achievements` lookup also guarded so transport
  failure returns 0 newly-awarded rather than propagating.
- New achievements get **appended** to `ACHIEVEMENTS`; keys are
  immutable identifiers stored forever once earned.

### Added — Localization (i18n)
- `TRANSLATIONS` dict: maps language code → {`_Strings` attribute name →
  translated value}. v10.0 ships partial coverage for **Japanese (ja)**
  and **Spanish (es)** — 20+ user-visible strings each (`/anime` and
  `/manga` usage + not-found messages, `/discover`, `/trending`,
  `/similar` empty/cache surfaces, achievements headers, footer
  attribution, cooldown wait).
- `T(key, *, lang=None, **fmt)` — returns the translated string when
  `TRANSLATIONS[lang][key]` exists; otherwise falls back transparently
  to the English value from `_Strings`. Missing English keys raise
  `AttributeError` (caller bug, not a translation gap).
- `T_for(ctx, user_id, key, **fmt)` — per-user shorthand. Reads
  `pref:lang:user:<id>` (the v9.3 KV pref) and routes through `T()`.
- v9.3's `/preferences language:` choices are activated — the embed
  note that "this doesn't translate anything yet" is now stale for ja
  and es; still accurate for ko/zh/de/fr until v10.x fills them in.
- `/anime` migrated to `T_for(...)` for usage + not-found surfaces as
  proof-of-life. Other commands stay on `S.KEY` until v10.x; they
  fall back to English cleanly even for users with non-English
  preferences set. **Partial-coverage doctrine:** every English string
  is reachable for every user; translated strings are an opt-in
  enhancement that grows incrementally.

### Added — Accessibility
- Audited every embed builder for the `description` field (the only
  embed field Discord exposes consistently to screen readers).
- Fixed `_make_studio_embed` which previously had no description.
  Studio embeds now carry a one-line summary describing the studio
  type and credit count.
- All other v1–v9 embed builders (`_make_anime_embed`,
  `_make_manga_embed`, `_make_character_embed`,
  `_make_voice_actor_embed`, `_make_staff_embed`,
  `_make_list_embed`) already had descriptions. Audit clean.

### Tests
- New regression file `tests/regression/test_v10_0_0.py` (23 tests).
  Coverage:
  - Manifest `/achievements` shape + USER-type optional `user` option.
  - Schema bootstrap includes `otaku_achievements` with wide PK.
  - `ACHIEVEMENTS` registry contract: 10 expected keys present, every
    entry has name/description/check.
  - `_check_and_award_achievements` returns [] for empty users,
    awards only newly-met achievements, skips already-earned,
    uses idempotent `ON CONFLICT DO NOTHING` inserts, swallows
    predicate AND outer-query exceptions.
  - `/achievements` empty-state surface, earned-list rendering.
  - Localization: TRANSLATIONS has ja + es with ANIME_NOT_FOUND
    coverage; T() returns English by default; T() returns translated
    when lang has entry; T() falls back to English for unknown lang
    AND for missing-key-in-covered-lang; T_for() reads KV pref;
    `/anime` integration with Spanish-user preference.
  - Accessibility: `_make_studio_embed` and `_make_anime_embed` both
    produce non-empty `description` fields.

### Deferred per ROADMAP §10
- **Auto-translation activation.** v10's localization infrastructure is
  built for static translations; auto-translating AniList descriptions
  to the user's preferred language still requires a translation proxy
  (the v9.2/v9.3 blocker). When the SDK exposes one, the v9.3 lang
  pref + v10 T() pipeline activates without further changes.
- **Monetization-ready.** ROADMAP §10 mentions "free vs. paid features
  if the platform supports paid plugin tiers by then." SDK v0.5.2
  exposes no paid-tier capability — confirmed via the `pip show
  mmo-maid-sdk` audit performed during the v9.3 → v10 transition.
  Skipped; will reopen if a future SDK adds the capability.
- **Documentation site + video demo.** Out of scope for a code change.
  The README, CHANGELOG, and ROADMAP serve as the canonical reference.
- **Featured-marketplace application.** External workflow; the
  technical bar (Risky-tier stability, regression discipline, multi-
  language UX, accessibility audit) is now met from the code side.

### Phase 10 summary
One tag shipped this phase: v10.0.0. Two new slash commands across
the phase (`/find` in v9.0 stayed the only new search; `/preferences`
in v9.3 + `/achievements` in v10 round out the personalisation
surface). 569 tests total (546 → 569). 47 slash commands total (was
46). Zero new capabilities. Zero new `proxy_domains_requested`.

### The full v10.0 plugin shape
- **47 slash commands** spanning discovery (/anime, /manga, /discover,
  /manga-discover, /trending, /similar, /random, /find, /mood,
  /genre-trends, /season-premieres, /character, /voice-actor, /staff,
  /studio, /character-popular, /genres), tracking (/favorite,
  /favorites, /watch, /list, /rate, /ratings, /progress, /import,
  /stats, /my-stats, /otaku-reset), recommendations (/recommend),
  community (/compare, /server-watchlist, /wp, /aotw, /poll,
  /leaderboard, /review, /reviews), notifications (/notify, /unnotify,
  /notify-list), preferences (/preferences, /achievements),
  admin (/otaku-admin), and meta (/help).
- **6 capabilities** declared: `discord:read`, `discord:send_message`,
  `interaction:respond`, `proxy:http`, `storage:kv`, `storage:sql`.
  No tier shifts since v2.0 (the original Risky declaration).
- **3 outbound sources** (`graphql.anilist.co`, `api.jikan.moe`,
  `kitsu.io`) all proxied through `ctx.http`. Per-source rate buckets.
- **13 SQL tables**: otaku_user_media, otaku_server_watchlist,
  otaku_watch_parties, otaku_watch_party_members, otaku_notifications,
  otaku_reviews, otaku_aotw_polls, otaku_aotw_candidates,
  otaku_aotw_votes, otaku_polls, otaku_poll_options, otaku_poll_votes,
  otaku_achievements.
- **569 tests** across 38 test files (35 immutable regression contracts
  v1.0 → v10.0, plus tests/test_plugin.py for dev iteration).

## [9.3.0] - 2026-05-14

### Phase 9 closes with what shipped today

**v9.2 was skipped** per the roadmap fallback. The MMO Maid SDK v0.5.2
exposes `ctx.discord`, `ctx.http`, `ctx.kv`, `ctx.sql`, `ctx.ephemeral`,
`ctx.metrics`, `ctx.interaction`, `ctx.log` — **no `ctx.llm` or any
AI/LLM proxy capability**. Per `ROADMAP.md` Phase 9: *"If the LLM proxy
is unavailable, this version slips and we go straight to v9.3."* That's
what we did. AI-powered summaries land if/when the SDK adds a proxy.

### Added — spoiler control
- `_redact_spoilers(text, *, show_unhidden=False)` — explicit-marker
  heuristic that wraps lines starting with `SPOILER:` / `[SPOILER]` /
  `(spoiler)` / `# spoiler` (case-insensitive) in Discord's `||…||`
  syntax. Idempotent: pre-wrapped content (`||x||`) is never re-wrapped.
  Empty input + bare markers (no body after the prefix) pass through.
- `_render_reviews` now accepts `viewer_id=...` and reads the viewer's
  `pref:spoilers:user:<id>` KV setting. Default `"hide"` applies the
  wrap; `"show"` returns plain text. Authors' submitted text is never
  modified in storage — only the render layer changes per viewer.
- We deliberately do NOT do content-based heuristic detection ("dies",
  "twist", "secretly", etc.). False-positive cost is too high; users
  who want to hide arbitrary content can use Discord's `||...||`
  themselves and the helper preserves their explicit choice.

### Added — /preferences
- `/preferences [language: <choice>] [spoilers: <choice>]` — single
  command with two optional options. With no options passed, displays
  current preferences. Either option updates that preference; both
  can be updated in one call. Per-user, persisted via KV
  (`pref:lang:user:<id>`, `pref:spoilers:user:<id>`).
- Language choices: `en`, `ja`, `ko`, `zh`, `es`, `de`, `fr` (7
  entries). The pref is stored but **doesn't translate anything yet** —
  v9.3 set up the scaffolding so v9.x or v10 can activate it when an
  SDK translation proxy lands. The embed surfaces this expectation
  via `S.PREFERENCES_LANG_NOTE`.
- Spoilers choices: `hide` (default, wraps in `||...||`) and `show`
  (renders raw text).
- Helpers `_get_pref_spoilers(ctx, user_id)` and
  `_get_pref_language(ctx, user_id)` — both defend against
  tampered/corrupted KV values by checking against the choice
  constants and falling back to default/None.

### Changed — wiring
- `_render_reviews` signature: added `viewer_id: str = ""` keyword-only
  parameter. Both callers updated (`cmd_reviews` slash handler at
  ~line 6307 and the `otaku:reviews:<media_id>:<page>` pagination
  dispatcher at ~line 5970) to pass the event's user_id through. An
  empty `viewer_id` (legacy call sites without a viewer context)
  defaults to no redaction opt-out lookup — `_get_pref_spoilers`
  with empty user returns the default `"hide"`, which keeps the
  safe-by-default behavior.

### Tests
- New regression file `tests/regression/test_v9_3_0.py` (31 tests).
  Coverage:
  - Manifest /preferences shape + choice-list parity with code constants
  - KV prefix constants frozen (`pref:lang:user`, `pref:spoilers:user`)
  - `_redact_spoilers` four marker forms + case-insensitivity + opt-out
    + idempotency on pre-wrapped + empty input + bare marker + multi-
    line mix
  - `_get_pref_*` defaults + stored values + garbage-value defense
  - `/preferences` view defaults, set spoilers, set language, set both,
    rejects bogus values, translation-pending note surface
  - `/reviews` integration: default-viewer wraps spoilers; show-pref
    viewer sees plain; redaction applies to title AND body

### What we *didn't* ship in v9.3 (deferred)
- Auto-translate AniList descriptions to the user's preferred
  language. Needs the same translation-proxy infrastructure as v9.2.
- Content-based spoiler detection (e.g. last-3-paragraph wrap,
  ML-classification). False-positive cost too high without per-user
  feedback loops.

### Capability surface
- **No new capabilities.** `storage:kv` covers preference storage;
  `interaction:respond` covers /preferences responses. No new
  `proxy_domains_requested` (no outbound calls for redaction or
  preferences).

### Phase 9 summary
Three tags shipped this phase: v9.0.0 (natural-language /find), v9.1.0
(multi-source AniList → MAL → Kitsu aggregation), v9.3.0 (spoiler
control + /preferences scaffold). v9.2 skipped per the documented
LLM-proxy fallback. Phase 9 closes at v9.3.0.

Two new slash commands (`/find`, `/preferences`). 546 tests total,
up from 489 at the start of the phase. Manifest grew to 46 commands
total. No new capability tiers; one `proxy_domains_requested`
expansion (Jikan + Kitsu in v9.1) that triggers marketplace re-review
on next upload.

## [9.1.0] - 2026-05-14

### Multi-source aggregation — Phase 9 architectural shift

`/anime` and `/manga` search now fall through **AniList → MyAnimeList
(Jikan v4) → Kitsu** when AniList misses or fails. The plugin's primary
source stays AniList; MAL and Kitsu are safety nets for the long tail
of titles AniList doesn't have indexed.

### Added — transport
- `_jikan_query(ctx, path, params, *, cache=False)` — REST GET against
  `https://api.jikan.moe/v4`. Honors the per-source rate bucket. Logs
  with `tags=["jikan", "http"]`.
- `_kitsu_query(ctx, path, params, *, cache=False)` — JSON:API GET
  against `https://kitsu.io/api/edge`. Same rate-bucket + logging
  pattern. Returns a `data` array (single-resource responses normalized
  to a list of one).
- `_RATE_BUCKETS` — in-process per-source token buckets. Constants
  `SOURCE_RATE_LIMITS` freeze the per-source limits: AniList 90/min,
  Jikan 3/sec, Kitsu 10/sec.
- `_rate_acquire(source)` — admits immediately or sleeps via
  `_sleep_for_retry` until the source's per-window budget allows the
  next request. Returns seconds slept (0.0 on admit). Unknown sources
  admit unconditionally — useful for tests.

### Added — canonical media dict
- `_canonicalize_anilist_media(raw)` — passes the AniList shape through
  and stamps `source="anilist"`, `source_id=raw["id"]`.
- `_canonicalize_jikan_media(raw)` — maps Jikan v4 fields onto the
  AniList shape (`title_english`/`title_japanese` → `title.english`/
  `.native`; `images.jpg.large_image_url` → `coverImage.large`;
  score×10 → `averageScore`; `season` uppercased; genres flattened).
  Stamps `source="mal"`, `source_id=raw["mal_id"]`.
- `_canonicalize_kitsu_media(raw)` — maps Kitsu JSON:API attributes onto
  the AniList shape (`canonicalTitle`/`titles` → `title`;
  `posterImage.large` → `coverImage.large`; `averageRating` string parsed
  to int; `startDate[:4]` → `seasonYear`). Stamps `source="kitsu"`,
  `source_id=raw["id"]`.

### Added — aggregator
- `_search_media(ctx, query, *, media_type="anime")` — sequential
  fallback chain. Returns the canonical dict from whichever source
  served the result, or None if all three failed. Caches each source's
  response independently (cache key includes the source).
- `/anime` and `/manga` repointed to use `_search_media`. All other
  commands (`/similar`, `/random`, `/discover`, `/mood`, `/find`,
  `/genres`, `/character`, `/voice-actor`, `/staff`, `/studio`,
  `/character-popular`, `/notify*`) stay AniList-only by design — they
  depend on AniList-specific schemas (recommendation graph, GenreCollection,
  tag taxonomy, airingSchedules) that MAL/Kitsu don't expose 1:1.

### Added — UX
- Embed footer now reads `Data from <Source>` per the source that
  served the result.
- "Open on AniList" button becomes "Open on MyAnimeList" / "Open on
  Kitsu" when the canonical media's `source` field changes.
- `/similar` button is suppressed when source != "anilist" (the
  recommendation graph is AniList-specific; a MAL ID would route to
  nothing).
- `last_anime:user:<id>` and `last_manga:user:<id>` KV cache is ONLY
  populated when source == "anilist" — downstream `/similar`, `/watch`,
  `/rate`, `/progress`, `/notify`, etc. all assume AniList IDs.
  A MAL/Kitsu fallback result is displayed but doesn't propagate to
  the cache. **v9.1 known limitation.**

### Changed — cache key
- `_cache_key` signature changed from `(query, variables)` to `(*parts)`.
  AniList callers pass `(query, variables)` unchanged; Jikan callers
  pass `("jikan", path, params)`; Kitsu callers pass `("kitsu", path,
  params)`. The varargs form is hash-stable: dict args are sorted before
  inclusion in the repr. Backwards-compatible — no caller required
  changes beyond the two new sources.
- `ANILIST_CACHE_MAX_ENTRIES` bumped 128 → 256 to accommodate the
  ~3x working set across three sources. (Constant name kept as
  `ANILIST_CACHE_MAX_ENTRIES` for back-compat; v9.1.x may rename to
  `MEDIA_CACHE_MAX_ENTRIES`.)

### Changed — manifest
- `proxy_domains_requested` now includes `api.jikan.moe` and `kitsu.io`
  (bare hosts — no scheme, no path; the validator at
  `scripts/validate_plugin.py` enforces this). NOTE: this is a
  **marketplace re-review trigger** per ROADMAP working principle #6.
  No capability tier shift (`proxy:http` was already declared).

### Tests
- New regression file `tests/regression/test_v9_1_0.py` (26 tests):
  manifest proxy_domains, rate-limit constants frozen, source-label
  coverage, rate-bucket admit/sleep contract, unknown-source no-op,
  all three canonicalisers (AniList passthrough, Jikan score rescale +
  season uppercasing + genre flattening, Kitsu string rating parsing +
  date year extraction + slug-based URL), `_search_media` fallback chain
  (AniList hit short-circuits MAL+Kitsu; MAL fallback works; Kitsu
  third fallback works; all-three-miss returns None), `/anime` and
  `/manga` routing through `_search_media`, no-`last_anime`-cache on
  MAL fallback, no-`/similar`-button on non-AniList source, footer
  attribution per source, button label per source, cache-key
  namespacing across sources.
- Updated three legacy `tests/test_plugin.py` tests
  (`test_anime_handles_rpc_timeout_with_ephemeral_followup`,
  `test_retry_exhaustion_returns_friendly_error`,
  `test_user_fixable_anilist_error_is_surfaced`) to accept either the
  v9.1 cross-source "not found" message OR the legacy AniList-specific
  message. The new contract: when AniList fails, MAL and Kitsu are
  tried before the user sees an error.

### Capability surface
- **No new capabilities.** `proxy:http` covers all three sources.
  `proxy_domains_requested` is a domain-list expansion within an
  already-granted capability — no tier shift (Risky/Dangerous),
  but DOES re-trigger marketplace human review per ROADMAP §6.

### Deferred to v9.1.x or v9.2
- `/discover`, `/season-premieres`, `/import` source-aware variants.
  `/discover` and `/season-premieres` use AniList's genre+season
  filters; Jikan/Kitsu have different filter syntaxes. `/import`
  already has the `source:` arg shape ready to add per the v8.0.1
  cheat sheet but lands cleanest after the canonicaliser settles.
- AniList-specific user-fixable error fragments
  (`_USER_FIXABLE_ANILIST_FRAGMENTS`) stay AniList-only. v9.1 doesn't
  add MAL/Kitsu-specific error message classification — if AniList
  rejects with "must contain at least 3 characters" but MAL accepts
  the query and returns a result, the user just sees the MAL result.
- Constant rename `ANILIST_CACHE_MAX_ENTRIES` → `MEDIA_CACHE_MAX_ENTRIES`.
  The current name is misleading post-v9.1; deferred to a v9.1.x
  cosmetic patch.

## [9.0.0] - 2026-05-14

### Phase 9 opens — AI & multi-source integrations

This version is a **MAJOR** because it opens Phase 9 (the architectural
phase that culminates in v9.1 multi-source aggregation). v9.0 itself
ships a single new command + a small refactor — but the refactor is
load-bearing for v9.1.

### Added
- `/find description:<text>` — natural-language search. The user types
  a free-form English description ("slow romance set in school with
  supernatural twist") and the plugin decodes it into a genre/tag
  blend via the inline `FIND_PHRASES` table (34 entries covering
  pacing, setting, theme, audience, and genre catch-alls). Single-page
  result (5 picks).
- Footer surfaces what was decoded — e.g. `Decoded as: genres: Romance,
  Slice of Life · tags: Iyashikei, School` — so users understand why
  the picks look the way they do.
- `_match_find_phrases(text)` — word-boundary substring matcher.
  Lowercases + strips punctuation + collapses whitespace, then matches
  each FIND_PHRASES trigger as a full-word substring of the padded
  normalised input. Protects against false positives like "art"
  matching "heart" by requiring leading + trailing whitespace.

### Refactored — load-bearing for v9.1
- Extracted `_search_by_genre_tag_blend` from v6.1's `_mood_query`.
  Both `/mood` and `/find` now share the same with-tags-then-genres-
  only fallback path. v6.1 regression contract preserved (tests still
  pass; the function-rename was the only behavior-equivalent change).
- This is the v8.0.1 audit's "do it once, not piecewise" recommendation
  from the [don't-double-defer memory](.claude/...). v9.1 will also
  call `_search_by_genre_tag_blend` when MAL/Kitsu fallback paths
  need to route AniList-style genre/tag queries through the shared
  fallback chain.

### Tests
- New regression file `tests/regression/test_v9_0_0.py` (21 tests):
  manifest entry, shared-helper extraction contract (both /mood and
  /find route through it), FIND_PHRASES table contract (≥30 entries,
  every entry has a trigger AND a genre or tag), `_match_find_phrases`
  multi-word union, case-insensitivity, word-boundary protection
  (the classic "art" / "heart" false-positive case), punctuation
  strip, empty + unrecognised input, isekai trigger routing,
  empty-query short-circuit (no AniList call), no-trigger-match
  surfacing the pointer-to-trigger-words error (no AniList call),
  decoded-blend footer rendering, query-in-header verbatim, AniList
  empty surfacing the /mood-pointer fallback, AniList failure
  surfacing the default error, sorted-stable genres in the routed
  blend, shared-helper routing locked.

### Capability surface
- **No new capabilities.** /find decodes locally; only AniList HTTP
  is invoked (existing `proxy:http`).

### Deferred — for v9.0.x or v9.1 patches
- /find pagination. v9.0 ships single-page because re-tokenising the
  same description on page 2 is fine, but encoding the decoded blend
  in the pagination custom_id is fiddly. Single-page is plenty for
  the natural-language search UX; if users want depth, /discover or
  /mood are the structured paths.
- AniList tag drift. FIND_PHRASES uses what's stable today (Iyashikei,
  School, Cyberpunk, Magic, Isekai, etc.). If AniList retires a tag,
  the `_search_by_genre_tag_blend` shared helper falls through to
  genres-only — the tag drift never strands a `/find` user.

### What's next — Phase 9 roadmap
- v9.1.0 — multi-source aggregation (MAL/Jikan + Kitsu fallbacks).
  Per the v8.0.1 audit cheat sheet: canonical-dict abstraction for
  embed rendering; parallel `_jikan_query` / `_kitsu_query`; per-source
  rate-limit buckets; new `proxy_domains_requested` entries. v9.1 is
  the real architectural shift — v9.0 was the warmup that proved the
  `_search_by_genre_tag_blend` extraction works.
- v9.2.0 — AI summaries (optional, slips if platform doesn't expose
  an LLM proxy).
- v9.3.x — translation + spoiler control.

## [8.3.0] - 2026-05-14

### Phase 8 closes — character popularity leaderboard

- `/character-popular` (no options) — global leaderboard of AniList
  characters sorted by `favourites` count (FAVOURITES_DESC). Paginated
  5 per page; rank numbers continue across pages (page 2 starts at #6).
  Each row shows `#NNN **Name** · ❤ N,NNN — [Parent Media](url)`.
- AniList query `QUERY_CHARACTER_POPULAR` — `Page(characters: sort:
  FAVOURITES_DESC)` with each character's most-popular parent media
  fetched in the same round-trip.
- Pagination prefix `otaku:popchar:<page>` routed through the existing
  `_route_components` dispatcher.
- Constant `CHARACTER_POPULAR_PER_PAGE = 5` frozen.
- Page 1 uses the in-process AniList cache (it changes slowly); pages
  2+ skip the cache to keep the working set small.

### Tests
- New regression file `tests/regression/test_v8_3_0.py` (13 tests):
  manifest entry, per-page constant, QUERY_CHARACTER_POPULAR field
  shape, rank rendering, rank continuation across pages, pagination
  prefix routing (must NOT collide with `otaku:trend:` or
  `otaku:page:`), empty-page error path, AniList-failure error path,
  null-parent-media graceful rendering, and the page-1 cache contract.

### Phase 8 summary
- Six commands shipped this phase across four MINOR releases (v8.0
  itself was the schema-migration MAJOR):
  - v8.0.0 — `/manga`, `/manga-discover`, `/manga-favorites`
  - v8.0.1 — migration hardening patch (no new commands)
  - v8.1.0 — `/voice-actor`, `/staff`
  - v8.2.0 — `/studio`
  - v8.3.0 — `/character-popular`
- Total slash commands now: **44** (35 v7 + 3 manga + 2 staff + 1
  studio + 1 character-popular + 2 unchanged sub-routings).
- Zero new capabilities across the entire phase. Phase 8's coupling
  surface for v9 (multi-source) is documented in the v8.0.1 audit:
  single HTTP chokepoint at `__main__.py:1320`, 45 `_anilist_query`
  call sites, per-query-family multi-source strategy.

### Capability surface
- **No new capabilities.** Read-only AniList lookup; reuses
  `proxy:http` + `interaction:respond`.

## [8.2.0] - 2026-05-14

### Added
- `/studio query:<name>` — look up an animation studio. Renders the
  studio's popular works split into two sections:
  - **Recent (≤ 2y)** — works with `seasonYear` within the last 2 years.
  - **Popular works** — older popular works from the studio's catalog.
  Each line shows the title + linked AniList URL + `(year)` suffix when
  available. Up to 5 entries per section.
- New AniList query `QUERY_STUDIO` — `Studio(search: $q)` with
  `media(perPage: 10, sort: POPULARITY_DESC, isMain: true)`. The
  `isMain: true` filter keeps non-production credits (distribution,
  licensing) out of the embed.
- Embed builder `_make_studio_embed` — toggles the header prefix between
  🎬 (animation studio) and 🏢 (other production org) based on AniList's
  `isAnimationStudio` flag. Empty-works case shows a `*(no main-work
  credits on AniList)*` state.
- Open-on-AniList link button when `siteUrl` is present.
- Constant `STUDIO_RECENT_WITHIN_YEARS = 2` — frozen so future timeline
  changes are explicit.

### Tests
- New regression file `tests/regression/test_v8_2_0.py` (15 tests):
  manifest entry, recency-cutoff constant, QUERY_STUDIO field shape
  (must request `isMain: true`, `isAnimationStudio`, perPage:10,
  popularity sort), empty-query short-circuit (no AniList call),
  not-found error path, header-emoji toggle for animation vs other,
  recent/catalog split with current year, no-works graceful empty
  state, year-suffix rendering, Open-on-AniList button.

### Capability surface
- **No new capabilities.** Read-only AniList lookup; reuses
  `proxy:http` + `interaction:respond`.

## [8.1.0] - 2026-05-14

### Added
- `/voice-actor query:<name>` — look up a voice actor's notable character
  roles. Renders bio, primary language, and up to 5 character roles (each
  with its most-popular parent media as a linked anchor).
- `/staff query:<name>` — look up an anime/manga production staff person.
  Renders bio, primary occupations (Director, Writer, Composer, etc.),
  and up to 5 production credits — each prefixed with the `staffRole`
  AniList records for that production (`*Director* — [Spirited Away]…`).
- New AniList query `QUERY_STAFF` — single GraphQL constant powering both
  commands. AniList's `Staff` type covers voice actors AND production
  staff; the embed builders pull different field framings (`characters`
  for /voice-actor, `staffMedia` for /staff) from the same record.
- `_make_voice_actor_embed`, `_make_staff_embed`, and a shared
  `_staff_display_name` helper that formats `Full (Native)` when both
  are present and different.
- Open-on-AniList link button when `siteUrl` is present, same pattern
  as `/character`.

### Tests
- New regression file `tests/regression/test_v8_1_0.py` (18 tests):
  manifest contract, QUERY_STAFF field shape (must request
  `characters(perPage:5, sort:FAVOURITES_DESC)` + `staffMedia` with
  `staffRole`), `_staff_display_name` three-way contract,
  /voice-actor + /staff empty-query short-circuit (no AniList call),
  not-found error path, happy-path embed rendering for both shapes,
  no-roles graceful-empty, and the assertion that BOTH commands route
  to the single `QUERY_STAFF` constant.

### Capability surface
- **No new capabilities.** Read-only AniList lookups; reuses
  `proxy:http` + `interaction:respond`.

### Notes
- AniList search returns multiple hits but `/voice-actor` and `/staff`
  surface only the top match — same first-match-only contract as
  `/character`. The embed footer (`S.FOOTER_CHARACTER` reused) flags
  this for the user.
- AniList's `description` for staff can run very long; truncated to
  `DESC_MAX` (350 chars) per the project-wide standard, with a
  `*(no bio on AniList)*` empty-state when the field is null.

## [8.0.1] - 2026-05-14

### Migration hardening — production-safety patch

A six-lens audit of v8.0.0 found two 🔴 high-severity correctness bugs in
`_migrate_v7_to_v8`, both reachable on the very first v7→v8 upgrade. This
PATCH fixes both, adds the regression coverage v8.0.0 was missing, and
tightens an under-asserting test that v8.0.0 introduced.

### Fixed
- **Partial-failure non-recovery** in `_migrate_v7_to_v8`. v8.0.0's helper
  did a single `WHERE table_name = 'otaku_user_anime'` probe and treated
  its absence as "migration already done." But if a previous run succeeded
  on the RENAME but errored mid-sequence (lock timeout, OOM, deploy
  interrupt), the table was renamed yet half-migrated — and the next call's
  probe found the v7 name absent and triggered the early-return. The
  half-state never healed. v8.0.1 makes every step independently
  idempotent: probe is now `WHERE table_name IN ('otaku_user_anime',
  'otaku_user_media')` and the helper re-decides per landmark (RENAME only
  if v7 present + v8 absent; ADD COLUMN always with `IF NOT EXISTS`; PK
  widening only if the wide PK isn't already there, via a new
  `information_schema.key_column_usage` probe).
- **Pool-mode RENAME race.** v8.0.0's CHANGELOG claimed "the second
  worker's probe sees the renamed table absent" — but with snapshot
  isolation, two workers can both pass the probe simultaneously and Worker
  B's `ALTER TABLE … RENAME` then fires against a relation that no longer
  exists at the v7 name. v8.0.1 acquires
  `pg_advisory_xact_lock(hashtext('otaku_v7_to_v8_migration'))` at the top
  of the helper so concurrent workers serialize on the migration. The lock
  is transactional and auto-releases at COMMIT — only the migration block
  serializes, not the rest of bootstrap.

### Added — tests
- New regression file `tests/regression/test_v8_0_1.py` (10 tests).
  Coverage:
  - `test_migration_executes_full_dance_when_v7_table_exists` — happy-path
    pinning that all migration DDLs fire when the v7 table is present. v8.0.0
    only tested the no-op branch; a regression deleting the RENAME, ADD
    COLUMN, or PK widening would have passed silently. Now caught.
  - `test_migration_skips_pk_widen_when_wide_pk_already_present` — verifies
    the new `key_column_usage` probe gates the drop/re-add dance, so the
    second run after a successful migration doesn't re-execute it.
  - `test_migration_self_heals_after_partial_failure` — drives the
    half-migrated scenario (v8 table renamed, column add never finished)
    and asserts the helper completes the column + PK widening steps.
  - `test_migration_noop_on_fresh_install` — confirms the helper skips
    every table DDL when neither v7 nor v8 table exists.
  - `test_migration_acquires_advisory_lock_first` — pins the lock ordering:
    `pg_advisory_xact_lock` must come before any `ALTER`.
  - `test_leaderboard_completed_query_filters_media_type_anime` — tightens
    the v3.3 leaderboard contract. v8.0.0 added `media_type = 'anime'`
    filters to all anime aggregates but `test_v3_3_0.py:63` only checked
    `"status = 'completed'"`. A regression dropping the media_type filter
    would have passed silently (leaking manga rows into the leaderboard).
    Now pinned.
  - Four `..._sql_anchors_<column>` tests — anchor the
    `is_favorite`/`status`/`rating`/`episodes_watched` DO-UPDATE clauses
    so a regression that stops touching the target column can't pass on a
    coincidental `True in params` / `"watching" in params` /
    `18 in params` / `"completed" in params` match. Pairs with the loose
    membership checks v8.0.0's `# regression-fix` edits introduced.

### Documented — PG version note
- `ALTER TABLE … ADD COLUMN … DEFAULT 'anime'` (line ~1601) is metadata-
  only on **PG ≥ 11** (the column lands in the catalog with a stored
  "missing value"; existing rows are NOT rewritten — O(1) regardless of
  table size). On **PG ≤ 10** the same DDL rewrites the entire table,
  holding `ACCESS EXCLUSIVE` for the duration — minutes of downtime for
  large tracker tables.
- The plugin can't enforce a minimum PG version (the runtime decides),
  but plugin operators should confirm their host runs PG ≥ 11 before
  upgrading installs with large `otaku_user_media` tables. The MMO Maid
  platform runtime is on PG 14+ as of 2026-05, so the practical risk
  for marketplace plugins is low — but worth surfacing for self-hosted
  plugin operators.

### Capability surface
- **No new capabilities.** `storage:sql` covers the new
  `information_schema` probes; `pg_advisory_xact_lock` is a SQL function
  call requiring no special grant.

### Audit summary recorded for the v8.1+ session
- All other audit lenses came back clean. **Filter integrity**: 0 leaks
  across 30 SELECTs. **Manga path correctness**: 0 bugs, 7/7 code paths
  verified. **Capability gating**: clean. **Test contracts**: 8/9 v8.0.0
  regression-fix edits honest (the under-asserting `test_v3_3_0.py:63` is
  fixed here).
- Net hygiene change vs v7.2.1 baseline: slightly worse (+3 duplication
  clusters from manga/anime parallel commands and embed builders, -1 from
  the now-atomic `_upsert_user_media`). Acknowledged in v8.0.0 CHANGELOG
  "Deferred to v8.x"; the Phase 9.1 canonical-dict refactor will partially
  absorb these.
- Phase 9 readiness inventory captured: single HTTP chokepoint at
  `__main__.py:1320` (good news — one egress point to teach about
  per-source headers), 45 `_anilist_query` call sites, 12 query constants
  classified per multi-source strategy (search/discover/season/batch:
  canonical-dict abstraction; import: parallel constants + `source:` arg;
  similar/random/genres/mood/airing/character: keep AniList-only).

## [8.0.0] - 2026-05-14

### Phase 8 opens — media universe expansion (manga support)

This is the first non-additive schema migration in the repo. `otaku_user_anime`
is renamed to `otaku_user_media` and gains a `media_type` column (default
`'anime'`) so manga, light novels, and future media types can share the
table without a parallel-schema proliferation.

### Added
- `/manga query:<title>` — AniList search filtered to `type: MANGA`. Caches
  the resolved media_id in `last_manga:user:<id>` (7-day TTL, mirrors the
  v1.0 anime cache shape).
- `/manga-discover genre:<name> [sort:<…>]` — paginated genre browse with
  the same `popular`/`trending`/`score` sort choices as `/discover`. Prev/
  next buttons use the new `otaku:mpage:<genre>:<sort>:<page>` custom_id
  prefix; pagination routes through `_render_manga_discover`.
- `/manga-favorites [manga:<…>] [remove:<…>]` — toggle a manga as a favorite,
  OR (with no args + no cached lookup) list the caller's manga favorites.
  Resolves `manga:` against either a numeric AniList ID or a title query;
  defaults to cached `last_manga:user:<id>`.
- `QUERY_MANGA_SEARCH_ONE`, `QUERY_MANGA_DISCOVER`, `QUERY_MANGA_BY_ID`,
  `QUERY_MANGA_BATCH` — parallel to the anime query constants. Each forces
  `type: MANGA` and uses `_MEDIA_FIELDS_MANGA` (chapters/volumes/startDate.year
  instead of episodes/season/seasonYear).
- `_make_manga_embed` — mirrors `_make_anime_embed` but renders chapters,
  volumes, and start year. The `MANGA_PROGRESS_*` strings say "Chapter X / Y"
  instead of "Episode X / Y".
- Regression file `tests/regression/test_v8_0_0.py` (20 tests) freezes the
  manifest contract, schema migration ordering, upsert media_type behavior,
  anime/manga query routing, embed field shape, and the `otaku:mpage:`
  pagination prefix.

### Changed — schema (MAJOR)
- `otaku_user_anime` table renamed to `otaku_user_media`.
- Added `media_type TEXT NOT NULL DEFAULT 'anime'` column. Migration
  backfills existing v7 rows to `'anime'` atomically via the DEFAULT.
- Primary key extended from `(user_id, media_id)` to
  `(user_id, media_id, media_type)`. The AniList ID space is shared across
  anime and manga, so the same numeric ID can legitimately exist as both;
  the wider PK keeps them as separate rows.
- Index renamed to `otaku_user_media_user_status_added_idx`.
- The `episodes_watched` column stays named — it serves as chapter count
  for manga rows. The column comment documents the dual interpretation.
  Future v8.x work can choose between rename, alias, or per-type accessor.
- New helper `_migrate_v7_to_v8(ctx)` runs from `_bootstrap_schema` before
  the v8 CREATE TABLE. It probes `information_schema.tables` for the v7
  table name before issuing `ALTER TABLE … RENAME TO`, so the migration
  is fully idempotent — re-runs on already-migrated installs are no-ops.
  Includes the constraint dance to widen the PK (Postgres can't widen a
  PK in place; the helper drops the old constraint and re-adds the wider
  one).

### Changed — helpers
- `_upsert_user_anime` → `_upsert_user_media`; the v7 name remains as a
  back-compat alias.
- `_upsert_user_media` accepts a `media_type='anime'` kwarg. INSERT carries
  it explicitly; ON CONFLICT uses the wider PK clause.
- `_is_favorite`, `_get_user_tracking` extended with `media_type='anime'`
  kwargs (same back-compat default).
- Every v7 anime-path SELECT gained `AND media_type = 'anime'` so manga
  rows from `/manga-favorites` don't leak into anime aggregates
  (`/stats`, `/my-stats`, `/leaderboard`, `/compare`, dashboards,
  `/recommend`, `/genre-trends`, `/ratings`, `/favorites`, `/list`).
- Three anime-only INSERT call sites (`/import anilist`, `/progress`,
  `/rate`) now write `'anime'` literally into the `media_type` column.

### Changed — regression test contracts (documented per ROADMAP doctrine)
- `tests/regression/test_v2_0_0.py`, `test_v2_1_0.py`, `test_v2_2_0.py`,
  `test_v2_4_0.py`, `test_v2_5_0.py`, `test_v2_6_0.py`, `test_v3_3_0.py`,
  `test_v6_0_0.py`, `test_v6_2_0.py` got `# regression-fix (v8.0.0):`
  comments where their literal-SQL substring assertions referenced
  `otaku_user_anime`. The intent each test asserted is preserved; only
  the table-name literal moved. This is the exact carve-out ROADMAP
  §"When a regression test would have to change" allows for MAJOR bumps.
- `tests/regression/test_v2_1_0.py:106`, `test_v2_2_0.py:88/100/112`,
  `test_v2_0_0.py:131/146` had positional `params[N]` assertions that
  broke because v8 added `media_type` to the INSERT column list. Switched
  to `in params` membership checks that survive shifts in column order.
- `tests/test_plugin.py` (dev tests, not in the immutable suite) also
  rewritten to assert by intent rather than by positional index.

### Migration notes
- **In-place v7→v8 upgrade** runs automatically the next time
  `_bootstrap_schema` fires (on_install OR on_ready, whichever the runner
  invokes first). The probe-then-rename pattern means concurrent pool-mode
  workers racing the migration is safe: the second worker's probe sees
  the renamed table absent from its v7 name and returns early.
- **Fresh v8 installs** skip the migration entirely — the v7 table never
  existed, so the probe returns empty and `_migrate_v7_to_v8` no-ops.
- **`episodes_watched` is dual-purpose** for v8.0 — anime rows store
  episode count, manga rows store chapter count. Embeds disambiguate via
  the type-aware `_make_anime_embed` / `_make_manga_embed` split.
- **`is_favorite` and `rating` are also dual-purpose** — same column, per-
  row meaning. Fine for v8.0; future v8.x might add separate scaling for
  manga ratings if AniList changes its score model.

### Capability surface
- **No new capabilities.** Phase 6, 7, and 8.0 each added zero capabilities;
  the v8.0 manga support runs on `proxy:http` (AniList queries),
  `storage:sql` (the shared `otaku_user_media` table), `storage:kv`
  (`last_manga:user:<id>`), and `interaction:respond` — all already
  declared since v2.0.

### Deferred to v8.x
- `/manga-watch`, `/manga-rate`, `/manga-progress`, `/manga-list`,
  `/manga-import` — these mirror v2.x anime commands but for manga and
  are layered atop the now-stable schema. Per the roadmap, v8.0 explicitly
  scoped to "search + discover + favorites," matching the listed minimum.
- The /recommend N+1 SQL fan-out, four read-then-write upsert tightenings,
  three dup-clusters, and custom_id dash-prefix normalization that v7.2.1
  CHANGELOG flagged remain deferred.
- `_make_anime_embed` → `_make_media_embed` consolidation is deferred —
  v8.0 ships the parallel `_make_manga_embed` instead. A future MAJOR can
  unify them if a third media_type (light novels?) makes the per-type
  helper a real burden.

## [7.2.1] - 2026-05-14

### Hygiene pass — clean baseline before Phase 8

Six-lens regression audit (clean-code / functionality / routing / SQL+KV /
naming / Phase-8 readiness) found zero high-severity bugs. This patch ships
the risk-free findings; the bigger items (`/recommend` N+1 SQL fan-out, four
read-then-write upserts, three dup-clusters, custom_id dash-prefix
normalization) stay deferred per the user's chosen scope.

### Removed
- Dead-code: `_options_list` (no callers; doc lied about test usage),
  `_get_user_progress` (v2.2 back-compat shim — `_get_user_tracking` replaced
  it years ago), `handle_similar_button` (alleged test convenience wrapper,
  zero references).
- Orphan `_Strings` constants: `SWL_REMOVE_USAGE`, `POLL_CREATED_HEADER`,
  `ADMIN_LOOKUP_FAILED` — all defined but never surfaced.

### Changed
- `WATCH_PARTY_STATUS_LABEL` now references `S.WP_STATUS_ACTIVE` /
  `_COMPLETED` / `_ABANDONED` constants instead of inlining their values —
  removes the duplication the audit flagged and keeps the i18n-readiness
  property the strings table is meant to give us.
- Two user-facing error strings that were inlined as f-strings (the /watch
  status-rejection message and the /season-premieres usage hint) now route
  through `_Strings` (`WATCH_INVALID_STATUS`, `PREMIERES_USAGE`) — every
  other user-facing string in the plugin already does.
- Module-level docstring updated from the v1.x "interaction-only, no SQL, no
  schedules" framing to reflect the current 35-command surface area.
- `_caller_is_admin` docstring corrected — used to promise it surfaced
  `ADMIN_LOOKUP_FAILED`, but callers always return `S.ADMIN_DENIED` on
  failure. Doc now matches behaviour.

### Fixed
- `_handle_reset_confirm` and `_handle_reset_cancel` now run through
  `_on_cooldown` like every other component handler. The SQL is idempotent
  so this wasn't a security or data bug — just consistent rate-limiting
  hygiene flagged by the routing-audit lens.

### Audit summary (recorded for the v8 session)
- Six review angles, 392 tests baseline. No 🔴 bugs found.
- 🟡 deferred to v7.2.2 or v8: collapse `/recommend` per-peer SQL fan-out
  into a single `WHERE user_id = ANY($1)` query (violates the roadmap's
  ≤3-SQL-per-command cross-cutting invariant); tighten four read-then-write
  upserts (`_upsert_review`, AOTW/poll create + vote handlers) to
  single-statement `INSERT … ON CONFLICT DO UPDATE`; collapse three
  duplicate sub-option-parsers and the `_top_genre_for_user` /
  `_user_top_genres` near-duplicate.
- 🟡 deferred to v8 (would force editing immutable regression tests):
  rename `otaku:<feature>-<sub>:*` custom_ids to `otaku:<feature>:<sub>:*`
  for `reset-confirm`, `reset-cancel`, `wp-join`, `aotw-vote`, `poll-vote`,
  `review-modal`.
- 🟢 v8-readiness inventory captured: 43 `otaku_user_anime` references to
  rename, 12 query constants to parameterise on `$mediaType`, PK extension
  to `(user_id, media_id, media_type)` required to disambiguate cross-type
  AniList ID collisions. `test_v2_0_0.py:72-73` asserts the literal DDL
  string — needs a pre-decision before v8 implementation starts.

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

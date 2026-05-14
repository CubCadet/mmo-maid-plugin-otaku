# OTAKU — Roadmap from v1.0 to v10.0

> **Live document.** Edit as priorities shift. Every change here gets its own commit (`docs(roadmap):`) so future-Claude can diff what changed and why.

This is the long-form path for the `otaku` MMO Maid plugin, from today's v1.0.0 (the "v0.01" foundation in colloquial terms — first thing that ships) to a v10.0.0 mature platform. It is designed to be read **by Claude Code in autonomous (bypass) mode**, in conjunction with the `mmo-maid-plugins` skill loaded into the session.

The document encodes:

1. **Working principles** — how commits, tests, and tags are sequenced.
2. **Self-healing problem-solving protocol** — what to do when an implementation attempt fails.
3. **Regression-test discipline** — how earlier features stay working as later ones land.
4. **The phased roadmap** — every major version (v1 → v10), each broken into minor versions, with concrete features, implementation hints, regression checks, and recovery paths.

Claude Code: when invoked to "execute the next phase of the roadmap," consult this document first, locate the current version in `manifest.json`, find the corresponding next-version section below, and follow it end-to-end. Do not advance to the next major version without all minor-version regression suites passing.

---

## Table of contents

- [Working principles](#working-principles)
- [Self-healing protocol](#self-healing-protocol)
- [Regression framework](#regression-framework)
- [Changelog & commit discipline](#changelog--commit-discipline)
- [Phase 1 — Solidify (v1.0 → v1.5)](#phase-1--solidify-v10--v15)
- [Phase 2 — Personal anime journey (v2.0 → v2.x)](#phase-2--personal-anime-journey-v20--v2x)
- [Phase 3 — Social & sharing (v3.0 → v3.x)](#phase-3--social--sharing-v30--v3x)
- [Phase 4 — Notifications & airing (v4.0 → v4.x)](#phase-4--notifications--airing-v40--v4x)
- [Phase 5 — Dashboard & insights (v5.0 → v5.x)](#phase-5--dashboard--insights-v50--v5x)
- [Phase 6 — Smart recommendations (v6.0 → v6.x)](#phase-6--smart-recommendations-v60--v6x)
- [Phase 7 — Community & engagement (v7.0 → v7.x)](#phase-7--community--engagement-v70--v7x)
- [Phase 8 — Media universe expansion (v8.0 → v8.x)](#phase-8--media-universe-expansion-v80--v8x)
- [Phase 9 — AI & multi-source integrations (v9.0 → v9.x)](#phase-9--ai--multi-source-integrations-v90--v9x)
- [Phase 10 — Maturity & marketplace-featured (v10.0)](#phase-10--maturity--marketplace-featured-v100)
- [Cross-cutting concerns](#cross-cutting-concerns)
- [Glossary](#glossary)

---

## Working principles

These are invariants. Every implementation step honors them. They override any other instruction except the non-negotiable rules in the `mmo-maid-plugins` skill (which always win).

### 1. Small, atomic commits

One concept per commit. A new slash command, the test for that command, the manifest change for that command, and the changelog entry for that command together form a single logical unit, but split them into 2–4 commits if the diff exceeds ~200 lines. Commits are cheap; rebase-able history is valuable.

### 2. Test-driven where reasonable

For pure logic (parsers, embed builders, KV-key derivers, recommendation scoring), write the failing test first, then the implementation. For glue code (handler wiring, slash command registration), test after — getting the shape right matters more than red-then-green.

### 3. Validator + tests + lint pass before *every* commit

```bash
python scripts/validate_plugin.py .   # from the mmo-maid-plugins skill
python -m pytest -q                   # all tests including regression
```

If either fails, fix it before committing. If a fix takes more than 3 attempts (see [Self-healing protocol](#self-healing-protocol)), stop and report.

### 4. Conventional Commits

```
<type>(<scope>): <subject>
```

Types used in this repo:

- `feat:` — new user-facing capability (new slash command, new event handled)
- `fix:` — bug fix
- `refactor:` — code change with no behavior change
- `test:` — adding or modifying tests
- `docs:` — README, CHANGELOG, ROADMAP changes
- `chore:` — scaffolding, deps, CI config
- `perf:` — performance-only changes
- `ci:` — GitHub Actions workflow changes

Scope is the area touched (`slash`, `kv`, `embed`, `recommend`, `dashboard`, `i18n`, etc.). Subject is imperative, no period, ≤72 chars.

Examples:
- `feat(slash): add /favorite to save the current anime`
- `fix(embed): truncate description before HTML strip not after`
- `refactor(recommend): factor scoring out of cmd_similar`
- `test(kv): cover TTL expiry for last_anime cache`

### 5. Tag-and-changelog cadence

Every minor or major version bump produces a git tag and a `CHANGELOG.md` entry. Patch versions (1.0.x) can batch into a single weekly tag if changes are small. The tag (`v1.2.3`) must match `manifest.json.version` exactly — the `release.yml` workflow enforces this.

### 6. Capability minimum-set

Each version that adds a capability to `manifest.json.capabilities_required` triggers a re-review by the marketplace. Plan capability additions deliberately — group them when possible (e.g., add `storage:sql` once and use it for multiple features in the same minor version, rather than adding it twice in successive patches).

### 7. KV key conventions stay consistent

All KV keys follow `<domain>:<scope>:<identifier>` shape, where `<scope>` is `user`, `guild`, or `global`. Existing keys (e.g., `last_anime:user:<id>`) are never renamed without a MAJOR version bump and a migration step.

### 8. Backwards compatibility within a major version

A v2.7.0 plugin must accept all the slash command invocations a v2.0.0 plugin accepted, and all KV state a v2.0.0 plugin wrote. Schema changes that require migration are MAJOR-only.

---

## Self-healing protocol

When an implementation step fails — tests don't pass, the validator rejects the manifest, the AniList response shape doesn't match, a button doesn't fire — follow this loop. Goal: stay autonomous as long as the problem is recoverable, stop and ask only when the problem genuinely needs human judgment.

### The 3-attempt rule

For any single failed step:

1. **Attempt 1 — direct fix.** Read the error, re-read the spec, fix the obvious issue.
2. **Attempt 2 — alternative implementation.** If attempt 1 fails for the same reason or a related one, try a different approach. Examples: change from `@plugin.on_component` to event-prefix routing, change from KV to ephemeral, use a different AniList query shape.
3. **Attempt 3 — minimal viable version.** If attempt 2 also fails, implement the simplest version that satisfies the user-visible goal even if it's less elegant. (Example: a /random command that picks from a hardcoded genre list instead of dynamically querying AniList for all genres.)

If attempt 3 fails, **stop and produce a structured report** rather than continuing to flail. The report goes in a new file at `notes/blockers/<YYYY-MM-DD>-<short-slug>.md` and contains:

```markdown
# Blocker: <short title>

**Date:** YYYY-MM-DD
**Version target:** vX.Y.Z
**Roadmap section:** [Phase X — ...](#...)

## What I was trying to do
<concise: the goal in user-visible terms>

## What I tried

### Attempt 1: <name>
<approach, file/line touched, what happened, error message verbatim>

### Attempt 2: <name>
<same>

### Attempt 3: <name>
<same>

## Suspected root cause
<best hypothesis with evidence>

## What I need
<the exact human decision needed: a spec clarification, a different API choice, an
explicit reduction in scope, etc.>
```

Commit this file with `docs(blocker): unable to implement <feature> at vX.Y.Z`. Do not advance the roadmap past this version. The next session can pick up from the blocker.

### What counts as a recoverable failure vs. a real blocker

**Recoverable** (use 3-attempt loop):
- Test fails because of a typo in an assertion
- AniList returns a slightly different field name than expected (`coverImage` vs `cover_image`)
- A button's `custom_id` collides with an existing one
- The validator rejects a malformed `slash_commands` entry
- Pytest can't import `__main__` directly (use the existing `plugin_main` symlink/import trick from v1.0)

**Genuine blocker** (stop and report after attempt 3):
- The required SDK capability doesn't exist yet (e.g., file uploads — no such capability)
- AniList rate limits the plugin so aggressively that real users can't use the feature
- A feature requires an SDK API that was deprecated or removed
- The scope of a feature is ambiguous in a way that affects user-facing behavior (e.g., "should /watchlist be per-user or per-server?" — ask, don't guess)
- Two roadmap features contradict each other (encoding error in this doc — flag it)

### Logging attempts

Inside the working session (before any blocker report), keep a scratch note at `notes/attempts/<YYYY-MM-DD>.md` listing each non-trivial attempt and outcome. This is for the agent's own context across long sessions. It's gitignored in `.gitignore` so it never enters history — only the blocker reports do.

```gitignore
# Add to .gitignore (already there if you cloned recent template)
notes/attempts/
!notes/blockers/
```

---

## Regression framework

### Where regression tests live

```
tests/
├── conftest.py
├── test_plugin.py            # ongoing dev tests
└── regression/
    ├── conftest.py
    ├── test_v1_0_0.py        # what worked at v1.0.0
    ├── test_v1_1_0.py        # what was added at v1.1.0
    ├── test_v2_0_0.py        # what was added at v2.0.0
    └── ...
```

When you tag a new version, **copy the parts of `tests/test_plugin.py` that verify the new behavior of that version into a new `tests/regression/test_v<X>_<Y>_<Z>.py`**. These regression tests are immutable — you don't edit them after tagging. They become the contract that future versions must not break.

Run regression alone: `pytest tests/regression -q`. Run everything: `pytest -q`. The CI workflow runs everything on every PR and tag.

### What goes in a regression test

The user-visible behavior of the version, expressed through `MockContext`:

- For each slash command added in that version: one happy-path test + one error-path test.
- For each KV key shape introduced: a test asserting the key format and TTL.
- For each component (button/select) introduced: a test asserting the `custom_id` prefix and routing.
- For each capability requested: a test asserting it appears in `manifest.json`.

Avoid testing internal helpers in regression — those can be refactored. Test the **contract** with the platform.

### When a regression test would have to change

If a regression test would have to change to accommodate a new version, that's a sign of a **breaking change** — bump MAJOR, not MINOR. Examples:

- A slash command's required option becomes optional → backwards compatible (MINOR is fine — old invocations still work).
- A slash command's required option's name changes → breaking (MAJOR).
- A KV value type changes from int to dict → breaking (MAJOR + migration).

If you're tempted to edit `tests/regression/test_v1_0_0.py` while building v1.7, **stop**. Either you're really doing a MAJOR version bump, or the test was wrong and needs a `# regression-fix:` documented commit explaining why it was wrong (rare).

### Smoke-test before every tag

Before tagging *any* version (patch, minor, or major), run:

```bash
python scripts/validate_plugin.py .
python -m pytest -q                              # all tests, including regression
python -m pytest tests/regression -q             # explicit regression-only pass
python scripts/build_release.py --output dist/   # the upload zip must build
```

All four must succeed. If any fail, follow the [Self-healing protocol](#self-healing-protocol).

---

## Changelog & commit discipline

### CHANGELOG.md format

Keep-a-changelog 1.1.0 format. Each version section has at most six subsections:

- **Added** — new features
- **Changed** — behavior changes to existing features
- **Deprecated** — features marked for removal
- **Removed** — features actually removed (MAJOR-only)
- **Fixed** — bug fixes
- **Security** — security-relevant changes

Always have an `[Unreleased]` section at the top. Move it down to a dated heading at tag time.

### Cadence

- **Patch (1.0.x → 1.0.x+1):** commit-by-commit additions to `[Unreleased]`. Move to dated section at tag time.
- **Minor (1.x.0 → 1.x+1.0):** same, but each minor version gets its own subsection in CHANGELOG even if the features were committed across days.
- **Major (1.x.x → 2.0.0):** dedicated CHANGELOG section with a **Migration notes** paragraph at the end if any data shape changed.

### Tag and release commands

```bash
# bump manifest.version and CHANGELOG.md first, then:
git add manifest.json CHANGELOG.md
git commit -m "chore(release): vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

The `release.yml` workflow runs the validator and tests one more time, builds the upload zip, and attaches it to the GitHub release. If that fails, delete the tag (`git tag -d vX.Y.Z && git push --delete origin vX.Y.Z`), fix the issue, re-tag.

---

## Phase 1 — Solidify (v1.0 → v1.5)

**Goal:** Take the v1.0.0 MVP from "works" to "polished and ready to scale." No major new features — small additive commands, hardening, and DX improvements.

### v1.0.1 — Hardening pass ✅ shipped 2026-05-14

**Target:** Patch. No new functionality, all robustness.

**Tasks:**
- ~~Add a network-error path test: mock `ctx.http.post` to raise `RpcTimeoutError`, assert `_reply_error` runs.~~
- ~~Add a malformed-response test: mock AniList returning `{"errors": [...]}` instead of `{"data": ...}`, assert graceful failure.~~
- ~~Add a "too many genres" edge case: mock 8 genres, assert only 5 are shown.~~
- ~~Move `plugin.run()` out from under `if __name__ == "__main__":` to be the unconditional last line — the skill's non-negotiable rule #1.~~
- ~~Add a `LICENSE` file scan for the MIT placeholder; confirm `[year] [author]` is filled in.~~

**Regression check:** ~~`tests/regression/test_v1_0_0.py` exists by now and still passes.~~ Created and green.

**Failure modes encountered:** the `plugin.run()` move did break tests as predicted — fixed by setting `OTAKU_SKIP_RUN=1` in both `tests/conftest.py` and `tests/regression/conftest.py` *before* loading the module, then guarding `plugin.run()` behind that env var at the bottom of `__main__.py`.

---

### v1.0.2 — Response caching for AniList ✅ shipped 2026-05-14

**Target:** Patch. Polite to AniList, faster for users.

**Tasks:**
- ~~Add a tiny in-process cache (5-minute TTL) keyed on the GraphQL query + variables hash.~~ ~~Use `ctx.ephemeral.set` with TTL.~~ — `ctx.ephemeral` has no `set/get` for arbitrary values; landed as a pure-Python module-level dict cache instead. This actually matches "in-process" more faithfully than the Redis-backed ephemeral would have.
- ~~Cache `/anime` lookups by normalized query string.~~ (lowercased before hashing)
- ~~Cache `/discover` page 1 results by `(genre, sort)`.~~
- ~~Cache `/trending` page 1 by `(season, year)`.~~
- ~~Do **not** cache `/similar` for cached-anime-id flow.~~

**Regression check:** ~~All existing `/anime`, `/discover`, `/trending` tests still pass (cache is transparent).~~ Verified — module-level cache reset via autouse fixtures in both conftests.

**Failure modes encountered:** SDK mismatch — `ctx.ephemeral` doesn't expose `set/get`. Per the self-healing protocol's recoverable list, switched to an in-memory dict, which has no quota or TTL ceiling concerns. Cache writes are wrapped in `try/except` so a future bug there can never raise into the handler.

---

### v1.1.0 — Random discovery + character lookup ✅ shipped 2026-05-14

**Target:** Minor. First feature additions.

**New slash commands:**

- ~~`/random [genre]`~~ — landed. Uses a meta query for `lastPage`, then rolls `randint(1, min(lastPage, 50))` and re-queries for that page.
- ~~`/character <query>`~~ — landed. First match only, with a footer that says so.

**Implementation hints:**
- ~~For `/random`, query `Page(perPage: 1, page: $page)` where `$page` is `randint(1, min(maxPage, 50))`. Don't pull all results.~~
- ~~For `/character`, the AniList GraphQL `Character` type has `name { full native }`, `image { large }`, `description`, and `media(perPage: 5) { nodes { ... } }`.~~

**Regression check:** ~~Copy v1.0.0 tests into `tests/regression/test_v1_0_0.py` (if not already).~~ Already done. Added `tests/regression/test_v1_1_0.py` for the two new commands.

**Failure modes encountered:**
- ~~AniList `Character` search returns multiple matches — use the first.~~ Footer says "first match only."
- ~~A character has no `description` — show "*(no description on AniList)*".~~ Implemented.
- ~~`randint` could land on an empty page if the user requests a niche genre — fall back to page 1 if the response is empty.~~ Implemented.

---

### v1.2.0 — Error UX polish + retry logic ✅ shipped 2026-05-14

**Target:** Minor. Make failures feel intentional.

**Tasks:**
- ~~Replace generic error strings with action-suggesting ones~~
- ~~Add automatic retry (up to 2 retries, exponential backoff at 0.5s and 1.5s)~~
- ~~Surface AniList's GraphQL `errors[]` to the user when they're user-fixable~~

**Regression check:** ~~All v1.0.x and v1.1.x tests pass. Add new tests for retry behavior~~. Retry tests live in `tests/test_plugin.py`; existing regression files unchanged.

**Failure modes:**
- ~~Retries during deferred interactions can blow past the 15-minute followup window for big payloads. Cap total retry budget at 4 seconds.~~ Budget is ~2s (0.5 + 1.5).
- ~~`RateLimitError` is **not retried** automatically.~~ Confirmed via test.

---

### v1.3.0 — Genre catalog + /help ✅ shipped 2026-05-14

**Target:** Minor. Discoverability of the plugin itself.

**New slash commands:**

- ~~`/help` — Lists every otaku command with a one-line description and an example. Pure interaction reply, no API call.~~ Generated from `manifest.json` at boot.
- ~~`/genres` — Shows the canonical AniList genre list as an ephemeral embed. Pulls from AniList's `GenreCollection` query and caches it for 24 hours.~~ Cached in KV at `genres:global`.

**Implementation hints:**
- ~~`GenreCollection` is a single GraphQL query returning a list of strings.~~
- ~~Store the genre list in KV at `genres:global` with a 24h TTL.~~
- ~~`/help` is static; no API dependency, no cache.~~

**Regression check:** ~~Existing tests pass. New tests verify `/help` lists all commands present in `manifest.json.slash_commands`.~~ Done in `tests/regression/test_v1_3_0.py`.

**Failure modes:**
- ~~The KV key `genres:global` could be evicted if the plugin's KV quota fills — fall back to an HTTP lookup if read returns None.~~ Implemented.
- ~~`/help` should auto-update if a new command is added — generate the content from `manifest.json` at boot, not hardcode it.~~ Implemented.

---

### v1.4.0 — i18n-ready string table ✅ shipped 2026-05-14

**Target:** Minor. Set up for Phase 9's localization without committing to it yet.

**Tasks:**
- ~~Extract every user-facing string in `__main__.py` into `strings.py` as a constant.~~ — `strings.py` would be stripped by the upload allowlist. Landed as `_Strings` (alias `S`) inside `__main__.py` instead. Same i18n properties (single source of truth, easy enumeration, swappable per-locale).
- ~~Replace inline strings with `S.NO_ANIME_FOUND.format(query=...)` style.~~
- ~~Don't add a language switcher yet — just the structural separation.~~

**Regression check:** ~~All v1.x.0 tests pass. The behavior is unchanged; this is purely a refactor.~~ Verified.

**Failure modes encountered:**
- The strings.py sibling-module approach hit the runtime allowlist (`{manifest.json, __main__.py, requirements.txt, dashboard_manifest.json, dashboard/}`). Solved by putting the namespace inside `__main__.py` — recorded here so v9 localization knows the shape.

---

### v1.5.0 — Final pre-v2 polish ✅ shipped 2026-05-14

**Target:** Minor. Sweep everything for consistency before opening v2.

**Tasks:**
- ~~Update `README.md` to reflect every command added across v1.x.~~ Table already complete (added each command as it shipped).
- ~~Lint pass: run `ruff check . && ruff format .` (add `ruff` to `requirements-dev.txt` and to `.github/workflows/ci.yml`).~~ — `ruff check` only. See note below.
- ~~Type-hint completeness: every public function in `__main__.py` has type annotations.~~ Verified — every top-level function has a return type and parameter types.
- ~~Add `pre-commit` config that runs `ruff` + `pytest -q` before every commit (optional but recommended for human sanity).~~ Deferred — `make release` chain already covers it (`lint validate test`).

**Regression check:** ~~Full regression suite (`tests/regression/`) passes.~~ Verified. ~~Add a v1.5 regression file documenting the strings.py module's stability.~~ Skipped — `S` is implementation detail, not a user-facing contract; v1.4 test already covers its existence.

**Failure modes encountered:** ~~Ruff's "fix" might reformat in a way that loses comments — review changes manually if formatting touches >50 lines.~~ Triggered. `ruff format` produced a 597-line diff that would inline multi-line GraphQL strings. Per the warning above, kept `ruff check` only.

---

## Phase 2 — Personal anime journey (v2.0 → v2.x)

**Goal:** Move the plugin from "I look stuff up" to "I track my journey." Users can save favorites, mark watched/dropped/plan-to-watch, rate, and progress through episodes. This is the first phase that introduces meaningful per-user state.

**Capability needed:** `storage:sql` arrives here (KV alone won't scale — leaderboards, watch history pagination, multi-field rows all want a relational shape).

### v2.0.0 — Favorites & watch status ✅ shipped 2026-05-15

**Target:** Major. First MAJOR bump.

**Schema landed:**

```sql
CREATE TABLE IF NOT EXISTS otaku_user_anime (
  user_id      TEXT NOT NULL,
  media_id     INTEGER NOT NULL,
  status       TEXT NOT NULL,   -- 'watching' | 'completed' | 'on_hold' | 'dropped' | 'plan'
  is_favorite  BOOLEAN NOT NULL DEFAULT FALSE,
  added_at     TIMESTAMP NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, media_id)
);

-- Added vs. roadmap: composite index for /list pagination and Phase-3
-- leaderboards.
CREATE INDEX IF NOT EXISTS otaku_user_anime_user_status_added_idx
  ON otaku_user_anime (user_id, status, added_at DESC);
```

**Slash commands shipped:**
- ~~`/favorite [add|remove]` — Mark/unmark the user's last `/anime` lookup (or pass an `anime` option). Persists to SQL.~~ — supports `anime:` option and a `remove:` boolean.
- ~~`/favorites [user]` — List a user's favorites. Defaults to the caller; takes an optional user mention.~~ Paginated.
- ~~`/watch <status>` — Set watch status for the last lookup.~~
- ~~`/list [status] [user]` — List a user's anime by status. Paginated.~~

**Pagination custom_id:** `otaku:list:<target_user_id>:<scope>:<page>` where scope is `all | favorites | watching | completed | on_hold | dropped | plan`. One AniList batch HTTP call per page via `Page.media(id_in: [...])`.

**Migration:** ~~None — first version with SQL.~~ KV key `last_anime:user:<id>` preserved verbatim.

**Regression check:** ~~All v1.x tests pass.~~ Confirmed. ~~v2.0.0 regression file added.~~ `tests/regression/test_v2_0_0.py`.

**Failure modes:**
- ~~SQL schema bootstrap belongs in `@plugin.on_install` (and now reliably also `@plugin.on_ready` for pool mode in SDK 0.5.2+).~~ Both wired; share a `_bootstrap_schema(ctx)` helper.
- ~~The `PRIMARY KEY (user_id, media_id)` means re-favoriting is a no-op — make sure the user gets feedback.~~ Done — separate "already in your favorites" / "wasn't in your favorites" messages.

---

### v2.1.0 — Ratings ✅ shipped 2026-05-15

**New slash commands:**
- ~~`/rate <score>` — Rate the last lookup (1–10, half-points allowed — score stored as int×2 to avoid floats).~~ Encoder/decoder unit-tested.
- ~~`/ratings [user]` — List a user's rated anime sorted by score.~~ Top 25.

**Schema change (additive — no MAJOR bump):**
```sql
ALTER TABLE otaku_user_anime ADD COLUMN IF NOT EXISTS rating SMALLINT;
```
Wired into `_bootstrap_schema`. Idempotent via `ADD COLUMN IF NOT EXISTS`.

**Regression check:** ~~All v2.0.x tests pass.~~ One regression-fix in `test_v2_0_0.py` documented in CHANGELOG (the original test over-specified DDL count).

---

### v2.2.0 — Episode progress tracking ✅ shipped 2026-05-15

**New slash command:**
- ~~`/progress <episodes>` — Set how many episodes you've watched of the last anime lookup. Validates against the anime's `episodes` count.~~ Over-cap input is capped + warned about; reaching total auto-promotes status to `completed`.
- ~~Display progress on `/anime` cards when the user has watched some episodes.~~ Wired in /anime, /random, and the expand select.

**Schema change:**
```sql
ALTER TABLE otaku_user_anime ADD COLUMN IF NOT EXISTS episodes_watched SMALLINT DEFAULT 0;
```

---

### v2.3.0 — Personal stats ✅ shipped 2026-05-15

**New slash command:**
- ~~`/stats [user]` — Total anime watched, completed, dropped, mean score, most-watched genre, total episodes, total estimated hours (24 min × episodes).~~

**Implementation:** ~~All from the existing `otaku_user_anime` table joined with the genre data we can either cache from AniList or query live.~~ Single `GROUP BY status` SQL for counts/episodes/mean-rating; AniList batch fetch for the 50 most-recent media to compute the top genre (sampled, not full table).

---

### v2.4.0 — Bulk import from AniList ✅ shipped 2026-05-15

**New slash command:**
- ~~`/import anilist <username>` — Pulls an existing AniList list for a user and seeds the local DB. Idempotent — re-imports update statuses but don't duplicate.~~ Status, episodes_watched, rating updated; is_favorite preserved.

**Capability:** ~~No new capabilities; uses existing `proxy:http`.~~ Confirmed.

**Failure modes:** ~~AniList lists can be huge (1000+). Stream in pages of 50; abort cleanly if the response is malformed mid-stream.~~ Capped at 100 pages (5000 anime); partial imports tell the user which page failed.

---

### v2.5.0 — Patch refinements ✅ shipped 2026-05-15

Polishing pass:
- ~~better UX on `/list` pagination~~ — pagination already polished in v2.0.0 (status emoji, prev/next, expand-select).
- ~~status emoji in embeds~~ — already in v2.0.0.
- ~~server-admin command `/otaku-admin reset-user @user` for moderation~~ — initially deferred, then **un-deferred in v2.6.0** (see below). The original "this would need `discord:read`" cost was real but I overstated it: `discord:read` is Safe tier so it doesn't shift the plugin's tier, and Phase 3 needs the same gating anyway.
- Added "Your rating" field to `/anime` / `/random` / expand-select cards.
- New `_get_user_tracking` helper combines progress + rating into a single SELECT.

### v2.6.0 — Admin gating (closes Phase 2) ✅ shipped 2026-05-15

Targeted patch to finish the work that v2.5 punted. Adds `discord:read`,
implements `_caller_is_admin` (guild-owner or role with ADMINISTRATOR /
MANAGE_GUILD bits), and ships `/otaku-admin reset-user <user>`. The helper
is reused by Phase 3's admin-only commands.

---

## Phase 3 — Social & sharing (v3.0 → v3.x)

**Goal:** Move from "my journey" to "our journey." Per-server shared lists, watch parties, comparing stats with friends.

### v3.0.0 — Server watchlists ✅ shipped 2026-05-15

**New slash commands:**
- ~~`/server-watchlist add` / `/server-watchlist remove` (admin-only) — Curate a per-server list of anime everyone can see.~~ Shipped as Discord sub-commands (`view`, `add`, `remove`) under a single `/server-watchlist` root since Discord doesn't allow a slash command to have both options and bare-invocation behavior. Both `add` and `remove` accept either a title (AniList search) or a numeric AniList media ID. `add` takes an optional `note:` field.
- ~~`/server-watchlist` — Browse the current server's watchlist.~~ Implemented as `/server-watchlist view`. Public (non-ephemeral); paginated via `otaku:swl:<page>` buttons.

**Schema:** ~~landed as written~~ — `CREATE TABLE IF NOT EXISTS otaku_server_watchlist` wired into `_bootstrap_schema`.

**Capability:** ~~Existing `storage:sql` covers this — schemas are per-server already.~~ Confirmed. Admin gating reuses `_caller_is_admin` (added in v2.6.0).

---

### v3.1.0 — Friend comparison ✅ shipped 2026-05-15

**New slash command:**
- ~~`/compare @user` — Side-by-side stats vs. another user in the server. Shared favorites, divergent ratings, "anime they've completed that you haven't" recommendations.~~ Four sections (Totals / Shared favorites / Divergent / Completion recs), each capped at 5 entries. One AniList batch HTTP call resolves titles. Divergence threshold ≥ 2 points (stored as ≥ 4 since rating is score × 2).

---

### v3.2.0 — Watch parties ✅ shipped 2026-05-15

**New slash commands:**
- ~~`/wp create <anime>` — Start a watch party for an anime. Returns an embed with a `[Join]` button.~~ Public embed, button uses `otaku:wp-join:<party_id>`.
- ~~`/wp join <id>` — Manually join (in case the button is missed).~~
- ~~`/wp status <id>` — See who's joined, current progress per member, target episode.~~
- ~~`/wp progress <id> <episode>` — Update your progress in the party. When everyone hits the same episode, fire an announcement.~~ Also auto-promotes party `status` to `completed` when everyone hits total episodes.

**Schema:**
```sql
CREATE TABLE otaku_watch_parties (
  party_id     SERIAL PRIMARY KEY,
  media_id     INTEGER NOT NULL,
  created_by   TEXT NOT NULL,
  created_at   TIMESTAMP NOT NULL DEFAULT now(),
  status       TEXT NOT NULL DEFAULT 'active'  -- 'active' | 'completed' | 'abandoned'
);

CREATE TABLE otaku_watch_party_members (
  party_id           INTEGER NOT NULL REFERENCES otaku_watch_parties(party_id),
  user_id            TEXT NOT NULL,
  episodes_watched   SMALLINT NOT NULL DEFAULT 0,
  joined_at          TIMESTAMP NOT NULL DEFAULT now(),
  PRIMARY KEY (party_id, user_id)
);
```

---

### v3.3.0 — Server leaderboard ✅ shipped 2026-05-15

~~`/leaderboard` — Server-wide leaderboard by completed count, mean score, total hours.~~ One slash with a `metric:` option (defaults to `completed`). Top 10 per board. Score board has a min-rated-rows threshold (3) to keep "one perfect rating" from sweeping the leaderboard. Hours uses the same 24min/episode heuristic as `/stats`.

---

## Phase 4 — Notifications & airing (v4.0 → v4.x)

**Goal:** Push, not just pull. Users get notified when episodes air for anime they care about.

**Capability needed:** `discord:send_message` arrives here (sending unprompted to a channel, not just responding to interactions). This is the **first Risky tier capability** — will re-trigger marketplace human review.

### v4.0.0 — Airing notifications (consolidated) ✅ shipped 2026-05-15

Two slices of the original Phase 4 plan rolled into one tag because of SDK gaps. See CHANGELOG v4.0.0 for the full deviation note. Summary:

**Slash commands:**
- ~~`/notify <anime>` — DMs the user when the episode airs~~ → posts in the announcement channel (or fallback per-subscription channel) with @mentions instead of DMs. The SDK doesn't expose a DM helper, only `send_message(channel_id=...)`.
- ~~`/notify-list`~~ ✓ (with live next-episode ETA from AniList).
- ~~`/unnotify <anime>`~~ ✓.
- **Added in v4.0**: `/otaku-admin set-channel` (was originally planned for v4.1). Admin-gated via the existing `_caller_is_admin` helper.

**Cron implementation:**
- ~~"register a server-side cron in the manifest"~~ — the manifest field name isn't documented in this skill ("consult the dev portal docs"). Shipped with `@plugin.cron("5 * * * *")` which works in single-tenant deployments. In pool mode it doesn't fire — documented as a known limitation. Lazy fallback: `/notify-list` exercises the live AniList query so users still see fresh data even without the cron.

**Schema:** added `channel_id TEXT` column vs. the roadmap's two-column shape — needed for the fallback channel when no announcement channel is set.

---

### v4.1.0 — vacant

Originally "server announcement channel." Shipped as part of v4.0.0 above.

---

### v4.2.0 — Seasonal premieres digest ✅ shipped 2026-05-15

**New slash command:**
- ~~`/season-premieres` — Show the upcoming season's premiering anime in a paginated embed.~~ Defaults to next season when args omitted; explicit `season:` and `year:` options for browsing past or further-ahead seasons.
- ~~Automatic weekly digest posted to the announcement channel during transition weeks.~~ Implemented as a piggyback on the v4.0 hourly cron — during the first 7 days of each season, the cron posts one digest per server (KV-dedup'd at `premieres_digest_last:guild`). "Weekly" became "once at season start" since that's the only week the digest is novel.

---

## Phase 5 — Dashboard & insights (v5.0 → v5.x)

**Goal:** A real plugin dashboard surfaces in the MMO Maid dev portal. Server admins see stats, history, trends.

### v5.0.0 — Manifest-mode dashboard ✅ shipped 2026-05-15

**New artifact:** ~~`dashboard_manifest.json` populated with widget definitions.~~ Two pages — Overview + Settings.

**Widgets:**
- ~~Top 5 anime by server installs (across the user table)~~ shipped as a table widget. The "by server installs" framing didn't quite fit — the plugin's data is per-server. Reinterpreted as "top 5 most-tracked anime in this server's collective tracker."
- **Mean score by genre — deferred to v5.3.x.** Implementing it requires per-media genre data we don't store in SQL (only `media_id`). Per-dashboard-load AniList lookups for ~50 anime would breach the 10s widget budget; needs proper genre caching, which is v5.3.x material.
- ~~Active users in the last 30 days~~ — stat card based on `MAX(added_at) > NOW() - INTERVAL '30 days'` per user.
- ~~Total episodes tracked~~ — stat card from `SUM(episodes_watched)`.
- **Extras:** total subscriptions stat card, status-distribution bar chart, and a Settings page form for the announce channel (mirrors `/otaku-admin set-channel`).

**Capability:** ~~None new — uses existing `storage:sql` for widget data via `@plugin.on_dashboard` handlers.~~ Confirmed.

---

### v5.1.0 — Personal stats page ✅ shipped 2026-05-15

~~Each user can view their own stats page via a `/my-stats` slash command that produces a richer embed than `/stats`.~~ Adds three list sections (top rated, top favorites, recently completed) plus a completion-percentage hint. One AniList batch HTTP call resolves titles for everything that lands in the embed.

---

### v5.2.0 — Iframe-mode dashboard ⏸ deferred to a future MAJOR

**Original plan:** upgrade to iframe mode, add `dashboard/` files, use the MaidSDK JS bridge.

**Why deferred:** the SDK makes manifest mode and iframe mode mutually exclusive — picking iframe requires deleting `dashboard_manifest.json`, which breaks the v5.0.0 regression contract that asserts the file's contents. Per the regression doctrine, "if a regression test would have to change, that's a sign of a breaking change — bump MAJOR." So an iframe switch can only ship as a MAJOR version.

**Decided** (with the user, 2026-05-15): skip v5.2.0 as a tag. The iframe switch is rolled forward to a future MAJOR bump that also lands the v5.3 chart features. The current manifest-mode dashboard from v5.0 + v5.1 stays in place until then.

---

### v5.3.x — Charts & trends ⏸ deferred to a future MAJOR

**Original plan:** Plotly or Chart.js embedded in the iframe dashboard. Time-series views of genre popularity, score distributions, watch-completion rates.

**Why deferred:** depends on v5.2's iframe-mode switch (see above) — JS chart libraries need a full HTML page to live in. Rolled forward to the same future MAJOR.

---

**Phase 5 status (2026-05-15):** closed at v5.1.0. The manifest-mode dashboard (`dashboard_manifest.json` + 8 `@plugin.on_dashboard` handlers) plus `/my-stats` cover the value of Phase 5 in v0.5.x SDK shape. v5.2 and v5.3.x land as v6.0.0 or later when iframe-only features (charts & trends, sortable tables, time-series, etc.) become user-visible priorities.

---

## Phase 6 — Smart recommendations (v6.0 → v6.x)

**Goal:** Beat AniList's default recommendations using the local watch history.

### v6.0.0 — Personal recommendation engine ✅ shipped 2026-05-14

**Algorithm v1:** Collaborative filtering across users in the same server.

- ~~For each user, compute a vector of (media_id, rating).~~ Vector is
  `rating / 2.0` on the 0.5–10.0 scale, built from rows with
  `rating IS NOT NULL`. Unrated tracked rows are intentionally omitted
  — they'd dilute the signal that the user actually liked the show.
- ~~For a target user, find the K most-similar users (cosine similarity over rating vectors).~~ Cosine over media_ids in both vectors. Peers
  must share **≥ 3 rated titles** with the target to qualify (filters
  out trivial single-overlap pairings).
- ~~Recommend anime those K users rated highly that the target hasn't watched.~~ Candidate score = `Σ over qualifying peers of (sim × peer_rating)`. Tie-break: peer count, then media_id asc. Top 5
  returned, each annotated with how many peers supported it.

**New slash command:**
- ~~`/recommend` — Returns 5 personalized recommendations.~~ Shipped.

**Implementation note:** Runs entirely off `otaku_user_anime`. One
AniList batch HTTP call resolves display titles for the top 5; nothing
else hits the network in the CF path.

**Failure modes:**
- ~~Small server, sparse data — fall back to AniList's `/similar` algorithm.~~ Triggers when target has <3 ratings OR no peer overlaps
  ≥3 titles. Fallback seed is the target's highest-rated tracked anime,
  falling through to newest favorite, then newest tracked.
- ~~Compute cost — cap the user count for similarity at 50, sample randomly if larger.~~ `RECOMMEND_PEER_CAP = 50`, sampled via
  `random.sample` when more peers exist.

**Capability:** No new capabilities — `storage:sql` covers everything.

---

### v6.1.0 — Mood-based suggestions ✅ shipped 2026-05-14

**New slash command:**
- ~~`/mood <feeling>` — Maps moods (e.g., "uplifting", "tense", "cathartic") to genre/tag combinations and returns matching anime.~~ Ten curated moods ship with `required` Discord choices (`uplifting`,
  `tense`, `cathartic`, `chill`, `epic`, `nostalgic`, `dark`, `funny`,
  `romantic`, `adventurous`).

~~Maintain a `moods.json` config mapping each mood to a weighted blend of AniList genres + tags.~~ — The runtime upload allowlist rejects sibling
`.json` modules (same constraint that pushed v1.4's strings inline). The
MOODS dict lives inside `__main__.py`. Half the moods carry a tag
enrichment (`chill` → Slice of Life + Iyashikei, `dark` → Horror +
Psychological + Gore, etc.); when the with-tags AniList query returns
no matches we transparently fall back to genres-only so a fragile or
missing tag never strands the user.

"Weighted blend" landed as **union semantics** — `genre_in`/`tag_in` are
OR-within-array, AND-across-arrays. True weighted reranking (boost
results matching multiple genres) is deferred; the empty-set fallback
already covers the worst case.

**Capability:** no new capabilities — reuses `proxy:http` and
`interaction:respond`.

---

### v6.2.0 — Genre-trend recommendations ✅ shipped 2026-05-14

~~Surface anime that are trending in genres the user already favors — bridges discovery and personalization.~~ Shipped as **`/genre-trends`**:
samples the caller's 50 most-recent tracked anime, computes the top 3
genres (count desc, alphabetic tie-break), then runs
`Page(media: genre_in: <top3>, sort: [TRENDING_DESC])` and filters
already-tracked media out of the result set. Ephemeral, 5 picks max,
fetches AniList at `perPage=15` so the post-filter surface still has
something to show.

**Edge cases handled:**
- Empty tracker → short-circuits before any AniList call with a
  pointer to `/favorite` / `/watch`.
- AniList sampling call fails or returns no genres → distinct
  "couldn't read your genres" error path.
- Every trending hit is already tracked → pointer to `/trending`
  rather than an empty embed.

**Capability:** no new capabilities — reuses `storage:sql` and
`proxy:http`.

---

## Phase 7 — Community & engagement (v7.0 → v7.x)

**Goal:** Make the server itself participate. Reviews, polls, awards.

### v7.0.0 — Reviews & ratings ✅ shipped 2026-05-14

**New slash commands:**
- ~~`/review <anime>` — Opens a modal to write a review (title + body). Stored per-user-per-anime.~~ Shipped as `/review` with **no
  options** — Discord's 3-second pre-`send_modal` wall clock makes a
  synchronous AniList title lookup unsafe before the modal, so the
  command uses cached `last_anime` only (same pattern as `/watch`,
  `/rate`, `/progress`). Users run `/anime query: <title>` first if
  they want to review something specific. The modal pre-fills the
  existing review (if any) so editing is one-shot.
- ~~`/reviews <anime>` — Show all reviews from this server for an anime.~~ Shipped as `/reviews [anime]`. `anime` accepts a title or
  numeric AniList ID, defaults to cached `last_anime`. Paginated 3 per
  page, sorted `updated_at DESC` so re-edited reviews resurface.

**Schema:**
```sql
CREATE TABLE otaku_reviews (
  user_id     TEXT NOT NULL,
  media_id    INTEGER NOT NULL,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMP NOT NULL DEFAULT NOW(),  -- added on top of the roadmap
  PRIMARY KEY (user_id, media_id)
);
```

Added `updated_at` so re-reviewing reorders the list freshness-first.

**Capability:** none new — `storage:sql` + `interaction:respond` cover
both the modal and the SQL upsert.

---

### v7.1.0 — Anime of the week voting ✅ shipped 2026-05-14

~~Server-wide weekly poll. Admins kick off; members vote via buttons. Winner pinned in the announcement channel.~~ Shipped as **`/aotw`**
with three subcommands:
- `/aotw start` (admin) — pulls top 5 from `otaku_server_watchlist`,
  creates poll, posts embed with 5 numbered vote buttons. Requires
  ≥2 entries in the watchlist. One active poll per server.
- `/aotw status` (public) — live standings.
- `/aotw end` (admin) — declares winner (max votes; tie-break by
  lowest media_id), posts to the v4.0 announcement channel (falls
  back to the run channel).

**No cron auto-end** — the roadmap's "weekly" framing collapsed to
admin-triggered. Cron-end adds time-tracking that isn't worth it for
admin-driven flows; can layer onto the v4.0 hourly cron later if
demand surfaces.

**Schema:** three new tables (`otaku_aotw_polls`,
`otaku_aotw_candidates`, `otaku_aotw_votes`) wired into
`_bootstrap_schema`. PK on votes is `(poll_id, user_id)` so changing
your vote UPDATEs the existing row.

**Capability:** none new — `discord:send_message` from v4.0 carries
the announcement.

---

### v7.2.0 — Server polls ✅ shipped 2026-05-14

~~General "which of these 4 anime should we watch next" polls with `/poll` and reactions.~~ Shipped as **`/poll`** with three
subcommands:
- `/poll create question:<…> a:<…> b:<…> [c] [d]` (admin) — creates
  a poll with 2–4 options, posts embed + numbered vote buttons.
- `/poll status id:<…>` (public) — live standings any time.
- `/poll end id:<…>` (admin) — closes the poll. No winner crowned —
  /poll is a discussion tool, use /aotw if you want a "winner."

**Buttons, not reactions.** Every other interactive surface in the
plugin uses buttons; staying consistent and avoiding reaction-event
capability bloat outweighs the literal roadmap wording.

**Multiple concurrent polls per server** are allowed (unlike /aotw
which enforces single-active). Each poll has its own poll_id.

**Schema:** three new tables (`otaku_polls`, `otaku_poll_options`,
`otaku_poll_votes`). PK on votes is `(poll_id, user_id)` so re-voting
UPDATEs the existing row.

**Capability:** none new. Closes Phase 7.

---

## Phase 8 — Media universe expansion (v8.0 → v8.x)

**Goal:** Anime is the start, not the end. Add manga, characters, voice actors, studios.

### v8.0.0 — Manga support ✅ shipped 2026-05-14

~~`/manga`, `/manga-discover`, `/manga-favorites` mirroring the anime commands.~~ All three shipped. ~~Most code reuses `_make_*_embed` helpers
with type-aware paths.~~ — Deviation: v8.0 ships a parallel
`_make_manga_embed` rather than unifying `_make_anime_embed` to handle both
types. The fields differ enough (episodes/season vs. chapters/volumes/year)
that the per-type helper is clearer than a type-branched union. A future
MAJOR can consolidate if a third media_type makes the duplication a real
burden.

~~**Schema change:** Add `media_type` column to `otaku_user_anime` (now `otaku_user_media`). MAJOR bump.~~ Shipped with the rename + column + PK
extension to `(user_id, media_id, media_type)`. The migration helper
`_migrate_v7_to_v8(ctx)` is idempotent (probes information_schema first)
and runs from `_bootstrap_schema` so both fresh installs and in-place
upgrades work without operator intervention.

**The test_v2_0_0.py literal-DDL gate** — resolved per the user-chosen
"documented regression-fix edit" path. Nine regression test files got
`# regression-fix (v8.0.0):` comments where their literal-SQL substring
assertions referenced `otaku_user_anime`. Per the ROADMAP doctrine, MAJOR
bumps explicitly carve this out.

**Deferred to v8.x:** `/manga-watch`, `/manga-rate`, `/manga-progress`,
`/manga-list`, `/manga-import` mirror their anime counterparts and layer
atop the now-stable schema. Roadmap scope for v8.0 was the three
search/discover/favorites commands — extras come as patches if demand
surfaces.

**Capability:** none new. `proxy:http`, `storage:sql`, `storage:kv`,
`interaction:respond` already covered everything.

---

### v8.1.0 — Voice actors & staff ✅ shipped 2026-05-14

~~`/voice-actor <name>` — Look up a VA with their notable roles.~~
~~`/staff <name>` — Director, writer, animator lookups.~~

Both shipped. AniList's single `Staff` type powers both; `QUERY_STAFF`
is one GraphQL constant. The embed builders (`_make_voice_actor_embed`
and `_make_staff_embed`) pull different field framings from the same
record — `characters` (top 5 by FAVOURITES_DESC, each with parent
media) for /voice-actor; `staffMedia.edges` with `staffRole` (top 5
by POPULARITY_DESC) for /staff. First-match-only, mirrors /character.

**Capability:** no new capabilities. Read-only AniList lookups.

---

### v8.2.0 — Studios

`/studio <name>` — Studio profile, popular works, current season's releases.

---

### v8.3.x — Character ranks

`/character-popular` — Top characters by AniList popularity.

---

## Phase 9 — AI & multi-source integrations (v9.0 → v9.x)

**Goal:** Get smarter than the underlying APIs.

### v9.0.0 — Natural-language search

`/find <english description>` — Takes free-form English ("a slow romance set in a school with supernatural elements") and translates to genre + tag filters.

Backed by a small mapping table maintained in code. No external LLM call required at this stage — purely lexical/tag-based.

---

### v9.1.0 — Multi-source aggregation

Add MyAnimeList (Jikan) and Kitsu as secondary sources. When AniList misses a title, fall back to MAL.

**Capability:** New proxy domains added.

---

### v9.2.0 — AI-powered summaries

Optional — only if the platform exposes an LLM proxy by then. Per-anime "personality-tailored" summaries.

If the LLM proxy is unavailable, this version slips and we go straight to v9.3.

---

### v9.3.x — Translation & spoiler control

Auto-translate descriptions to the user's preferred language. Detect spoilers in user-submitted reviews via a small heuristic + blur them by default.

---

## Phase 10 — Maturity & marketplace-featured (v10.0)

**Goal:** Bench-press-worthy plugin. Featured on the marketplace.

### v10.0.0 — The mature platform release

**Targets:**

- **Localization:** Real language support using the v1.4 string table. At least English + Japanese + Spanish.
- **Accessibility:** Alt-text on every embed image, screen-reader-friendly text descriptions for charts.
- **Gamification:** Achievements ("completed 50 anime," "watched all of 2025's premieres," "rated 100 anime"). Per-server leaderboard.
- **Monetization-ready:** If the platform supports paid plugin tiers by then, define free vs. paid features (e.g., personal stats free, server-wide analytics paid).
- **Documentation:** Full per-command docs site, embedded help walkthrough, video demo.
- **Marketplace submission:** Apply for "Featured" status on the MMO Maid marketplace.

This is a deliberate cap. After v10.0.0, future development goes back into the v10.x line, then potentially a v11+ if a real new direction emerges. The number "10" isn't sacred — it represents the moment the plugin moves from "growing" to "running a stable, mature product."

---

## Cross-cutting concerns

### Performance budget

- Every slash command responds (or defers) within 1 second under nominal AniList latency.
- No single GraphQL query asks for more than 50 results.
- KV writes per command: ≤2.
- SQL queries per command: ≤3.
- Total bytes written to logs per command: ≤2 KB.

If a feature breaches any of these, optimize before tagging the version.

### Security

- Never log Discord user IDs to public channels (only to plugin logs).
- Never expose another user's data via `/list @user` if they've opted out (add `/otaku-privacy hide` slash command in v2.x).
- Validate all slash command options server-side — never trust the client format.
- Parameterized SQL only. Always. Forever.

### Cost awareness

The MMO Maid platform meters HTTP, KV, and SQL usage per plugin per server. From the cost analysis Paul has done on the platform side, the cost drivers are:

- **HTTP** — by request count. Cache aggressively; respect AniList rate limits.
- **KV** — by key count and write frequency. Use SQL for anything user-volume-scaling.
- **SQL** — by row count and query frequency. Index hot columns.

After v5 (dashboard), monitor the per-server cost shape and add a `notes/cost-profile.md` documenting where each plugin dollar goes. If a single feature consumes >30% of the plugin's per-server cost, that's a red flag — surface in the next version's planning.

### Documentation expectations

Every version updates:
- `manifest.json` (version, possibly capabilities/commands)
- `CHANGELOG.md`
- `README.md` — if new commands or capabilities
- `ROADMAP.md` — strike-through completed milestones, add new ones discovered along the way (this very document)

---

## Glossary

- **MVP** — Minimum Viable Product. v1.0.0 is the otaku MVP.
- **Pool mode** — MMO Maid runtime mode where one plugin worker serves many low-traffic servers. Affects `on_ready` and `@plugin.schedule`/`@plugin.cron` semantics. See the skill's `decorators-and-events.md`.
- **AniList** — The free public GraphQL API at `graphql.anilist.co`. Primary data source through v8; supplemented by MAL/Kitsu in v9.
- **Capability tier** — Safe / Risky / Dangerous. Higher tiers re-trigger marketplace human review. Default to Safe.
- **Regression test** — A test added when a version ships, never edited afterward, used to verify later versions don't break what shipped.
- **Self-healing protocol** — The 3-attempt loop described above for autonomous problem-solving.
- **Blocker report** — A structured note in `notes/blockers/` written when the 3-attempt loop exhausts, requesting human input.
- **Watch party** — A group of users coordinating progress through the same anime, with shared state and notifications.
- **Conventional Commits** — Commit message format with type/scope/subject. See [conventionalcommits.org](https://www.conventionalcommits.org/).

---

## How to use this document (Claude Code)

When the user says "execute the next phase" or "advance the roadmap":

1. Read `manifest.json` to find current version.
2. Locate the corresponding section in this document (search for `vX.Y.Z`).
3. Find the **next** version's section.
4. Implement every task listed under that section, committing as described.
5. Run the smoke test before tagging: validator, all tests, regression, build_release.
6. Update CHANGELOG.md and ROADMAP.md (strike completed items).
7. Tag the new version. Push.
8. Stop and surface the release URL to the user.

When the user says "execute Phase X":

Repeat steps 1–7 for every version in that phase, only stopping if a blocker emerges per the self-healing protocol.

When the user says "execute until v10":

Same, but for every version. Plan for this taking many sessions — checkpoint after every major version.

When the user says something else:

Read the roadmap for context, then handle the specific request. The roadmap is the plan, not the only thing this plugin can do.

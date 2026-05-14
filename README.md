# Otaku

> Discover, search, and learn about anime from inside Discord — powered by AniList.

A plugin for [MMO Maid](https://mmomaid.com) — runs sandboxed in the platform and reacts to slash-command interactions on installed servers.

## What it does

Otaku gives your server a small set of slash commands for finding anime: look one up by name, browse a genre, see what's trending this season, or pull recommendations similar to a show you've already looked at. Every command answers with a rich Discord embed (cover art, score, episode count, genre tags, a short description, and a link to the AniList page). The plugin caches each user's most recent `/anime` lookup for 7 days so `/similar` works with no arguments — handy when you've already found the show you care about and just want "more like this." All data comes from AniList's public GraphQL API; the plugin only reaches `graphql.anilist.co` and stores nothing about the user beyond their last-viewed anime ID.

## Capabilities

This plugin requests the following capabilities. Each is listed in `manifest.json` with a one-line rationale so server admins know *why* it's needed before they install:

| Capability | Tier | Why |
|---|---|---|
| `interaction:respond` | Safe | Reply to slash commands and component (button/select) clicks. Auto-added because the manifest declares `slash_commands`. |
| `proxy:http` | Safe | Call AniList's GraphQL endpoint (`graphql.anilist.co`) — the only outbound host. |
| `storage:kv` | Safe | Cache each user's last-viewed anime ID for 7 days so `/similar` can default to it. Also caches AniList's genre list for 24h. |
| `storage:sql` | Risky | Per-user anime tracking (`/favorite`, `/watch`, `/list`, `/favorites`). Single table `otaku_user_anime`, auto-scoped per server. |
| `discord:read` | Safe | Read the caller's guild membership and roles to gate `/otaku-admin` to server admins (anyone with `Administrator` or `Manage Server`). Used purely for permission checks. |

No Discord-side write capabilities are requested — the plugin never sends, edits, or deletes channel content directly; everything is an interaction reply.

If/when new capabilities are added, update this table *and* `CHANGELOG.md`.

## Slash commands

| Command | Description |
|---|---|
| `/anime <query>` | Search AniList by title. Replies with a full anime card (cover, score, episodes, status, genres, description, AniList link) plus `[🔁 Similar]` and `[🌐 Open on AniList]` buttons. Also caches the result as your "last anime" for 7 days. |
| `/discover <genre> [sort]` | Browse a genre. `sort` is one of `popular` (default), `trending`, or `score`. Replies with a paginated list of 5 results plus `[⬅️ Prev]` / `[Next ➡️]` buttons and a select menu to expand any result into the full anime card. |
| `/trending` | Top 5 trending anime for the current season (Winter/Spring/Summer/Fall) — same paginated style as `/discover`. |
| `/similar [anime]` | Top 5 AniList-recommended anime for a given title. If `anime` is omitted, uses your cached last `/anime` lookup (if any); otherwise tells you ephemerally to run `/anime` first. |
| `/random [genre]` | Roll a single random anime. If `genre` is given, the roll is constrained to that genre. Caches the result as your "last anime." |
| `/character <query>` | Look up an AniList character by name. Returns a card with image, native + romaji name, description, and the top 5 media they appear in. First match only. |
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

### Politeness throttle

Every command checks an ephemeral per-user cooldown (`otaku:user:<id>`, 2 s) before hitting AniList. The cooldown is sandbox-side only — AniList itself permits ~90 req/min globally, and the platform proxy enforces 30/min per (server, plugin). The 2 s per-user throttle stops a single chatty user from monopolising either budget.

## KV key convention

The plugin uses two KV keys:

```
last_anime:user:<discord_user_id>   →   <anilist_media_id>   (TTL: 7 days, per-user)
genres:global                       →   ["Action", ...]      (TTL: 24h, server-wide)
```

KV is per-server and per-plugin, so the same user is tracked independently on each server. The 7-day TTL means an inactive user's cache expires on its own — no explicit cleanup needed. KV is wiped automatically on uninstall.

## SQL schema

v2.0.0 added one table for tracking commands. Rows are auto-scoped to `ctx.server_id` by the runner, so the schema has no explicit `server_id` column.

```sql
CREATE TABLE IF NOT EXISTS otaku_user_anime (
  user_id      TEXT NOT NULL,
  media_id     INTEGER NOT NULL,
  status       TEXT NOT NULL,                 -- watching | completed | on_hold | dropped | plan
  is_favorite  BOOLEAN NOT NULL DEFAULT FALSE,
  added_at     TIMESTAMP NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, media_id)
);
CREATE INDEX IF NOT EXISTS otaku_user_anime_user_status_added_idx
  ON otaku_user_anime (user_id, status, added_at DESC);
-- v2.1.0 (additive):
ALTER TABLE otaku_user_anime ADD COLUMN IF NOT EXISTS rating SMALLINT;  -- score × 2 (2..20)
-- v2.2.0 (additive):
ALTER TABLE otaku_user_anime ADD COLUMN IF NOT EXISTS episodes_watched SMALLINT DEFAULT 0;
```

The DDL is idempotent (`IF NOT EXISTS`) and runs from both `@plugin.on_install` and `@plugin.on_ready`. The `on_ready` path covers pool-mode workers picking up a tenant that upgraded from v1.x (where `on_install` does not re-fire).

## Custom_id namespace

All component custom_ids are prefixed with `otaku:`:

| custom_id | Source | Notes |
|---|---|---|
| `otaku:similar:<media_id>` | `[🔁 Similar]` button on the `/anime` card | Stateless — the media ID is in the ID itself. |
| `otaku:page:<genre>:<sort>:<page>` | `/discover` prev/next buttons | Re-queries AniList on each click. |
| `otaku:trend:<page>` | `/trending` prev/next buttons | Re-queries the current season. |
| `otaku:expand` | List-view select menu | The selected option's `value` is the media ID. |
| `otaku:list:<user>:<scope>:<page>` | `/list` and `/favorites` prev/next buttons | scope is `all`, `favorites`, or one of the watch statuses. |

## Quick start (development)

```bash
# 1. Clone & install
git clone https://github.com/CubCadetXT1/mmo-maid-plugin-otaku.git
cd mmo-maid-plugin-otaku
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 2. Local dev loop (hot-reload + mock host)
mmo dev --watch

# 3. Tests
python -m pytest -q

# 4. Lint (ruff — also runs in CI)
make lint

# 5. Pre-flight validation (also runs in CI)
python scripts/validate_plugin.py .
```

`mmo dev` fires events from `events.yaml` against a `MockContext`, prints every action the plugin takes, and reloads on file change. See [SDK docs](https://mmomaid.com/dev/docs) for the full developer workflow.

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

The MMO Maid dev portal links to this repo and pulls a specific tag for review. Review turnaround is typically 1–3 business days. The reviewer checks the manifest, scans for disallowed imports and unparameterised SQL, and re-prompts users on any tier shift.

## License

MIT — see [`LICENSE`](LICENSE).

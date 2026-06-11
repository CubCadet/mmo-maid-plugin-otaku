"""Regression contract for otaku v10.0.0 — Phase 10 maturity release.

IMMUTABLE — what shipped at v10.0.0:

ACHIEVEMENTS
- New SQL table `otaku_achievements (user_id, achievement_key, awarded_at,
  PRIMARY KEY (user_id, achievement_key))`. Bootstrapped via
  `_SCHEMA_ACHIEVEMENTS_DDL` in `_bootstrap_schema`.
- New slash command `/achievements [user]` — displays earned + in-progress
  achievements for self (default) or another server member. Runs every
  predicate on access, awards newly-met ones via INSERT ON CONFLICT DO
  NOTHING, surfaces "✨ newly earned" line.
- `ACHIEVEMENTS` registry — list of 10 entries shipped at v10.0:
  first_anime, first_favorite, first_review, completed_10, completed_50,
  rated_25, rated_100, community (5 reviews), seasonal_subscriber (3
  notify subs), polyglot (any language pref set).
- `_check_and_award_achievements(ctx, user_id)` returns the list of keys
  awarded THIS call. Existing rows are NEVER overwritten — keys are
  immutable identifiers stored forever once earned.
- Predicates are pure SQL/KV reads; each guarded by try/except so a
  predicate raising can never strand the handler.

LOCALIZATION (i18n)
- `TRANSLATIONS` dict maps lang code → {`_Strings` key → translated
  value}. v10.0 ships partial coverage for `ja` and `es` covering
  ~20 most-visible strings (anime/manga/discover/trending/similar
  usage + not-found + achievements headers + footer attribution).
- `T(key, *, lang=None, **fmt)` returns the translated string if a
  TRANSLATIONS[lang][key] exists; otherwise falls back to the English
  value from `_Strings`. Missing English keys raise AttributeError
  (caller bug, not a translation gap).
- `T_for(ctx, user_id, key, **fmt)` is the per-user shorthand —
  reads `pref:lang:user:<id>` and routes through T().
- Spoiler-control / language pref pipeline from v9.3 stays the
  single source of truth for which language a user prefers.
- Other PREF_LANGUAGE_CHOICES (ko, zh, de, fr) declare their codes
  but have no v10.0 translations — T() falls back to English
  silently. v10.x can fill them in without code changes.

ACCESSIBILITY
- Every embed builder now produces a `description` field with
  meaningful (non-image-only) content. v10 audited and fixed
  `_make_studio_embed` which previously had no description. All
  other v1-v9 embed builders already had descriptions; documented
  in CHANGELOG as "audit clean."

CAPABILITIES
- No new capabilities. v10.0 ships entirely on existing surfaces:
  `storage:sql` (achievements + reads), `storage:kv` (language pref
  already from v9.3), `interaction:respond` (slash + embeds).

ROADMAP DEFERRALS
- Auto-translation: still blocked on SDK exposing an LLM/translation
  proxy. v9.3's deferral carries forward.
- Monetization-ready: SDK doesn't expose paid-tier capability. Not
  shipped; documented in CHANGELOG.
- Marketplace-featured application + documentation site + video demo:
  out of scope for a code change.
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from yourbot_sdk.testing import MockContext, make_event


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


def _slash(name: str, options: dict | None = None, **extra) -> dict:
    return make_event(
        "interaction_create", interaction_type=2,
        command_name=name,
        options=[{"name": k, "value": v} for k, v in (options or {}).items()],
        **extra,
    )


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_achievements():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "achievements" in names


def test_achievements_user_option_is_optional_user_type():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "achievements")
    user = next(o for o in cmd["options"] if o["name"] == "user")
    assert user["required"] is False
    assert user["type"] == 6  # USER


# ── Schema bootstrap ───────────────────────────────────────────────────────


def test_achievements_table_is_bootstrapped():
    ctx = MockContext()
    p._bootstrap_schema(ctx)
    sqls = " ".join(c["sql"] for c in (ctx.sql.executed or []))
    assert "CREATE TABLE IF NOT EXISTS otaku_achievements" in sqls
    assert "PRIMARY KEY (user_id, achievement_key)" in sqls


# ── ACHIEVEMENTS registry contract ─────────────────────────────────────────


def test_achievements_registry_has_expected_keys():
    """v10.0 ships these 10 keys. Adding more is fine; removing any is a
    breaking change (users would lose visible achievements)."""
    keys = {a["key"] for a in p.ACHIEVEMENTS}
    expected = {
        "first_anime", "first_favorite", "first_review",
        "completed_10", "completed_50",
        "rated_25", "rated_100",
        "community", "seasonal_subscriber", "polyglot",
    }
    assert expected.issubset(keys), f"missing: {expected - keys}"


def test_every_achievement_has_name_description_check():
    for ach in p.ACHIEVEMENTS:
        assert ach.get("key"), f"missing key: {ach}"
        assert ach.get("name"), f"missing name: {ach['key']}"
        assert ach.get("description"), f"missing description: {ach['key']}"
        assert callable(ach.get("check")), f"missing check predicate: {ach['key']}"


# ── _check_and_award_achievements ──────────────────────────────────────────


def test_check_and_award_returns_empty_for_user_with_nothing():
    """Brand-new user — none of the predicates fire."""
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []
    awarded = p._check_and_award_achievements(ctx, "fresh-user")
    assert awarded == []


# regression-fix (v10.0.1): the v10 doctrine ran one COUNT query per
# predicate (16 round-trips per /achievements call). v10.0.1 collapses them
# into a single FILTER-aggregate over otaku_user_media plus one subquery
# pair for reviews + notifications. Business behavior is unchanged — same
# predicates, same awards — but the mock SQL shape changes from
# `{"n": ...}` per-predicate to a single aggregate row with named
# columns. Original assertion (first_anime + completed_10 fire, not
# completed_50) is preserved verbatim.
def test_check_and_award_inserts_only_newly_met_achievements():
    """User has 12 completed anime → first_anime + completed_10 fire."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "otaku_achievements" in sql:
            return []  # no prior awards
        if "FROM otaku_user_media" in sql and "FILTER" in sql:
            return [{"total": 12, "favorites": 0, "completed": 12, "rated": 0}]
        if "FROM otaku_reviews WHERE user_id = %s" in sql and "FROM otaku_notifications" in sql:
            return [{"reviews": 0, "subs": 0}]
        return [{"n": 0}]

    ctx.sql.query = _q
    awarded = p._check_and_award_achievements(ctx, "binge-user")
    assert "first_anime" in awarded
    assert "completed_10" in awarded
    assert "completed_50" not in awarded


def test_check_and_award_skips_already_earned():
    """A user with first_anime already in the achievements table should
    NOT re-award it — even if the predicate still qualifies."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "otaku_achievements" in sql:
            return [{"achievement_key": "first_anime"}]
        # Same data as the binge-user case — predicate still qualifies.
        if "AND status = 'completed'" in sql:
            return [{"n": 1}]
        return [{"n": 1}]

    ctx.sql.query = _q
    awarded = p._check_and_award_achievements(ctx, "already-earned")
    assert "first_anime" not in awarded


# regression-fix (v10.0.1): see comment on
# test_check_and_award_inserts_only_newly_met_achievements. The
# `ON CONFLICT DO NOTHING` assertion on the INSERT is preserved verbatim;
# only the predicate-feeding mock returns the new aggregate row shape.
def test_check_and_award_uses_idempotent_insert():
    """The INSERT must use ON CONFLICT DO NOTHING so a race or retry can't
    error out the handler."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "otaku_achievements" in sql:
            return []
        if "FROM otaku_user_media" in sql and "FILTER" in sql:
            return [{"total": 5, "favorites": 5, "completed": 5, "rated": 5}]
        if "FROM otaku_reviews WHERE user_id = %s" in sql and "FROM otaku_notifications" in sql:
            return [{"reviews": 5, "subs": 5}]
        return [{"n": 5}]

    executes_seen: list = []

    def _e(sql, params=None):
        executes_seen.append(sql)

    ctx.sql.query = _q
    ctx.sql.execute = _e
    p._check_and_award_achievements(ctx, "x")
    inserts = [s for s in executes_seen if "INSERT INTO otaku_achievements" in s]
    assert inserts, "expected at least one INSERT"
    assert all("ON CONFLICT (user_id, achievement_key) DO NOTHING" in s for s in inserts)


def test_check_and_award_swallows_predicate_exceptions():
    """A predicate raising must NOT strand the entire check."""
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: (_ for _ in ()).throw(RuntimeError("boom"))
    # Should not raise.
    awarded = p._check_and_award_achievements(ctx, "x")
    assert awarded == []


# ── /achievements handler ──────────────────────────────────────────────────


def test_achievements_empty_user_shows_empty_state():
    ctx = MockContext()
    ctx.sql.query = lambda sql, params=None: []
    p.cmd_achievements(ctx, _slash("achievements", {}, user_id="newbie"))
    follow = ctx.interaction.followups[-1]
    assert "haven't earned any achievements" in (follow.get("content") or "")


def test_achievements_self_view_when_some_earned():
    """User has first_anime in DB. Embed surfaces it + the in-progress list."""
    ctx = MockContext()

    def _q(sql, params=None):
        if "otaku_achievements" in sql:
            return [{"achievement_key": "first_anime"}]
        # No counts qualify any other predicate.
        return [{"n": 0}]

    ctx.sql.query = _q
    p.cmd_achievements(ctx, _slash("achievements", {}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    body = json.dumps(embed)
    # Earned section shows First Look.
    assert "First Look" in body
    # In-progress section shows other achievements.
    assert "In progress" in body


# ── Localization (i18n) ────────────────────────────────────────────────────


def test_translations_dict_has_ja_and_es():
    """v10.0 promised Japanese + Spanish. Adding more languages is fine;
    removing either is a breaking change."""
    assert "ja" in p.TRANSLATIONS
    assert "es" in p.TRANSLATIONS


def test_translations_cover_anime_not_found_for_ja_and_es():
    """The user-visible /anime not-found is the canonical 'is this
    localised?' check."""
    assert "ANIME_NOT_FOUND" in p.TRANSLATIONS["ja"]
    assert "ANIME_NOT_FOUND" in p.TRANSLATIONS["es"]


def test_T_returns_english_by_default():
    """No lang specified → English."""
    assert p.T("ANIME_NOT_FOUND", query="X") == p._Strings.ANIME_NOT_FOUND.format(query="X")


def test_T_returns_translated_when_lang_has_entry():
    out_ja = p.T("ANIME_NOT_FOUND", lang="ja", query="X")
    assert out_ja != p._Strings.ANIME_NOT_FOUND.format(query="X")
    # JA value should contain the query, since the translation uses {query}.
    assert "X" in out_ja


def test_T_falls_back_to_english_for_unknown_lang():
    """A user sets lang=fr but TRANSLATIONS doesn't have fr coverage —
    they still get the English message instead of a crash or empty string."""
    out_fr = p.T("ANIME_NOT_FOUND", lang="fr", query="X")
    assert out_fr == p._Strings.ANIME_NOT_FOUND.format(query="X")


def test_T_falls_back_to_english_for_missing_key_in_lang():
    """A key not yet translated in a covered lang → English fallback."""
    # FAVORITE_ADDED is not in TRANSLATIONS["ja"] yet.
    out = p.T("FAVORITE_ADDED", lang="ja", title="X")
    # Falls back to the English template formatted with the title.
    assert "X" in out
    # And matches what English would give.
    assert out == p._Strings.FAVORITE_ADDED.format(title="X")


def test_T_for_reads_user_language_preference():
    """T_for routes through KV. A user with `pref:lang:user:u = "ja"`
    sees Japanese."""
    ctx = MockContext()
    ctx.kv.set("pref:lang:user:multi", "ja")
    out = p.T_for(ctx, "multi", "ANIME_NOT_FOUND", query="X")
    # The JA template includes "アニメ" (Japanese for "anime").
    assert "アニメ" in out


def test_T_for_returns_english_for_user_with_no_pref():
    ctx = MockContext()
    out = p.T_for(ctx, "no-pref-user", "ANIME_NOT_FOUND", query="X")
    assert out == p._Strings.ANIME_NOT_FOUND.format(query="X")


def test_cmd_anime_uses_T_for_localisation(monkeypatch):
    """When a user has lang=es set, /anime's not-found message should
    surface in Spanish."""
    ctx = MockContext()
    ctx.kv.set("pref:lang:user:spanish-user", "es")
    monkeypatch.setattr(p, "_search_media", lambda *a, **kw: None)
    p.cmd_anime(ctx, _slash("anime", {"query": "missing-anime"}, user_id="spanish-user"))
    follow = ctx.interaction.followups[-1]
    content = follow.get("content") or ""
    # ES translation contains "encontró".
    assert "encontró" in content


# ── Accessibility — every embed has a description ──────────────────────────


def test_studio_embed_has_description_field():
    """v10.0 fixed _make_studio_embed which previously had no description.
    Screen readers depend on this field."""
    studio = {
        "id": 1, "name": "X", "isAnimationStudio": True,
        "siteUrl": "u",
        "media": {"nodes": []},
    }
    embed = p._make_studio_embed(studio)
    assert embed.get("description"), "studio embed must have a description for accessibility"


def test_anime_embed_already_has_description():
    """Pre-existing audit: _make_anime_embed always renders a description
    (the AniList synopsis or the no-description placeholder)."""
    media = {"id": 1, "title": {"romaji": "X", "english": "X"},
              "description": None, "averageScore": 80, "genres": [],
              "siteUrl": "u"}
    embed = p._make_anime_embed(media)
    assert embed.get("description"), "anime embed must always have a description"

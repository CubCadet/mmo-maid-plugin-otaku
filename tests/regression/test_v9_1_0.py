"""Regression contract for otaku v9.1.0 — multi-source aggregation.

IMMUTABLE — what shipped at v9.1.0:
- /anime and /manga search now fall through AniList → MAL (Jikan v4) →
  Kitsu when AniList misses or fails. All three sources use the same
  in-process cache (keyed on `(source, query, params)`).
- New transport helpers `_jikan_query` (REST GET against
  https://api.jikan.moe/v4) and `_kitsu_query` (JSON:API GET against
  https://kitsu.io/api/edge).
- Per-source token buckets in `_RATE_BUCKETS` (`_rate_acquire` admits
  immediately or sleeps until the source's per-window budget allows
  the next request). Limits frozen: AniList 90/min, Jikan 3/sec,
  Kitsu 10/sec.
- New canonical media dict: any source's response is mapped onto the
  AniList-shape via `_canonicalize_anilist_media`,
  `_canonicalize_jikan_media`, or `_canonicalize_kitsu_media`. Each
  stamps a `source` annotation ("anilist" | "mal" | "kitsu") and a
  `source_id`. The embed builders consume the AniList shape directly.
- Aggregator `_search_media(ctx, query, *, media_type)` returns the
  canonical dict from whichever source served the result, or None if
  all three failed.
- /anime + /manga surface attribution: footer reads "Data from
  AniList" / "Data from MyAnimeList" / "Data from Kitsu". Button
  label reads "Open on <Source>".
- `last_anime`/`last_manga` KV cache is ONLY populated when the
  successful source is AniList — downstream commands like /similar,
  /watch, /rate assume AniList IDs and would break if a MAL ID
  silently landed in the cache. v9.1 known limitation.
- `/similar` button is ONLY shown when source == anilist — manga has
  no /similar in v8.0, and MAL/Kitsu fallbacks don't propagate to
  AniList's recommendation graph.
- manifest `proxy_domains_requested` now includes `api.jikan.moe` and
  `kitsu.io` (bare hosts; validator requires no schema/path).
- `_cache_key` signature changed from `(query, variables)` to
  `(*parts)` so the same cache can serve all three sources without
  collision. AniList callers pass `(query, vars)`; Jikan/Kitsu pass
  `("jikan", path, params)` / `("kitsu", path, params)`.
- No new capabilities. `proxy:http` covers the new hosts; the only
  marketplace impact is the proxy_domains addition triggering
  re-review per ROADMAP working principle #6.
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


def test_manifest_proxy_domains_include_all_three_sources():
    """v9.1 added Jikan + Kitsu. AniList stays primary."""
    domains = _manifest().get("proxy_domains_requested") or []
    assert "graphql.anilist.co" in domains, "AniList must stay"
    assert "api.jikan.moe" in domains, "MAL (Jikan) must be declared"
    assert "kitsu.io" in domains, "Kitsu must be declared (bare host)"


def test_proxy_domains_are_bare_hosts_not_urls():
    """The validator requires bare hosts (no scheme, no path). Common
    mistake: declaring 'kitsu.io/api' instead of 'kitsu.io'."""
    domains = _manifest().get("proxy_domains_requested") or []
    for d in domains:
        assert "://" not in d, f"{d}: scheme present"
        assert "/" not in d, f"{d}: path present"


# ── Rate-bucket constants frozen ───────────────────────────────────────────


def test_source_rate_limits_match_published_apis():
    """Anyone changing these MUST update both the constant AND the v9.1
    contract docs (CHANGELOG + ROADMAP). Frozen to catch accidental drift."""
    assert p.SOURCE_RATE_LIMITS["anilist"] == (90, 60)
    assert p.SOURCE_RATE_LIMITS["jikan"] == (3, 1)
    assert p.SOURCE_RATE_LIMITS["kitsu"] == (10, 1)


def test_source_labels_cover_every_source_in_rate_limits():
    """Every rate-limited source must have a user-visible label for the
    footer + 'Open on X' button."""
    for src in p.SOURCE_RATE_LIMITS:
        # SOURCE_LABEL maps the canonical source key. Jikan responses are
        # tagged source="mal" so SOURCE_LABEL has "mal" instead of "jikan".
        canonical_src = "mal" if src == "jikan" else src
        assert canonical_src in p.SOURCE_LABEL, f"missing label for {canonical_src}"


# ── Rate-bucket behavior ───────────────────────────────────────────────────


def test_rate_acquire_admits_within_budget(monkeypatch):
    """Calls within the budget shouldn't sleep."""
    monkeypatch.setattr(p, "_RATE_BUCKETS", {s: [] for s in p.SOURCE_RATE_LIMITS})
    slept_total = {"s": 0.0}

    def _no_sleep(s):
        slept_total["s"] += s

    monkeypatch.setattr(p, "_sleep_for_retry", _no_sleep)
    # AniList budget: 90/min. Make 5 calls — all should admit.
    for _ in range(5):
        slept = p._rate_acquire("anilist")
        assert slept == 0.0
    assert slept_total["s"] == 0.0


def test_rate_acquire_sleeps_when_budget_exhausted(monkeypatch):
    """When the bucket hits the limit, _rate_acquire sleeps until the
    oldest in-window entry would expire."""
    monkeypatch.setattr(p, "_RATE_BUCKETS", {s: [] for s in p.SOURCE_RATE_LIMITS})
    sleeps: list = []

    def _record_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(p, "_sleep_for_retry", _record_sleep)
    # Jikan: 3 per 1 second. First 3 admit; 4th sleeps.
    for _ in range(3):
        p._rate_acquire("jikan")
    p._rate_acquire("jikan")
    assert sleeps, "4th Jikan call within 1s should sleep"
    assert sleeps[0] > 0.0


def test_rate_acquire_unknown_source_is_unrestricted(monkeypatch):
    """Sources not in SOURCE_RATE_LIMITS admit unconditionally — useful
    for tests and for keep-AniList-only paths that need a no-op shim."""
    slept = p._rate_acquire("not_a_source")
    assert slept == 0.0


# ── Canonicalisers ─────────────────────────────────────────────────────────


def test_canonicalize_anilist_passes_through_and_stamps_source():
    """AniList responses already satisfy the canonical contract; the
    canonicaliser just annotates."""
    raw = {
        "id": 555,
        "title": {"romaji": "X", "english": "X"},
        "siteUrl": "https://anilist.co/anime/555",
        "averageScore": 80,
    }
    out = p._canonicalize_anilist_media(raw)
    assert out is not None
    assert out["source"] == "anilist"
    assert out["source_id"] == 555
    # Original AniList shape preserved (the embed builders read these keys).
    assert out["title"] == raw["title"]
    assert out["siteUrl"] == raw["siteUrl"]
    assert out["averageScore"] == 80


def test_canonicalize_anilist_rejects_invalid_input():
    assert p._canonicalize_anilist_media(None) is None
    assert p._canonicalize_anilist_media({}) is None
    assert p._canonicalize_anilist_media({"title": "no id"}) is None


def test_canonicalize_jikan_maps_score_to_anilist_scale():
    """Jikan returns score 0..10; canonical dict uses 0..100 (AniList scale)."""
    raw = {
        "mal_id": 11061,
        "url": "https://myanimelist.net/anime/11061",
        "title": "Hunter x Hunter (2011)",
        "title_english": "Hunter x Hunter",
        "title_japanese": "ハンター×ハンター",
        "images": {"jpg": {"large_image_url": "u"}},
        "synopsis": "Synopsis text.",
        "score": 9.04,
        "members": 1500000,
        "type": "TV",
        "episodes": 148,
        "status": "Finished Airing",
        "season": "fall",
        "year": 2011,
        "genres": [{"name": "Action"}, {"name": "Adventure"}],
    }
    out = p._canonicalize_jikan_media(raw)
    assert out is not None
    assert out["source"] == "mal"
    assert out["source_id"] == 11061
    # Score rescaled to 0..100.
    assert out["averageScore"] == 90  # round(9.04 * 10)
    # Title shape matches AniList: title.romaji + .english + .native.
    assert out["title"]["english"] == "Hunter x Hunter"
    assert out["title"]["native"] == "ハンター×ハンター"
    # Genres flattened to a list of strings.
    assert out["genres"] == ["Action", "Adventure"]
    # Season uppercased to match AniList.
    assert out["season"] == "FALL"


def test_canonicalize_jikan_handles_null_optional_fields():
    """A Jikan response missing score, images, or season shouldn't crash."""
    raw = {"mal_id": 1, "title": "X"}
    out = p._canonicalize_jikan_media(raw)
    assert out is not None
    assert out["averageScore"] is None
    assert out["season"] is None


def test_canonicalize_jikan_rejects_missing_mal_id():
    assert p._canonicalize_jikan_media({"title": "no mal_id"}) is None


def test_canonicalize_kitsu_parses_string_rating():
    """Kitsu returns averageRating as a string '0'..'100'; canonicaliser
    parses it to int."""
    raw = {
        "id": "1",
        "type": "anime",
        # regression-fix (v10.0.12): slug moved into attributes where Kitsu's
        # real JSON:API puts it — the fixture had mirrored the canonicaliser's
        # swapped-levels bug (slug read from top level, type from attributes).
        "attributes": {
            "slug": "cowboy-bebop",
            "canonicalTitle": "Cowboy Bebop",
            "titles": {"en": "Cowboy Bebop", "en_jp": "Cowboy Bebop",
                        "ja_jp": "カウボーイビバップ"},
            "synopsis": "...",
            "posterImage": {"large": "u"},
            "averageRating": "82.45",
            "userCount": 300000,
            "showType": "TV",
            "episodeCount": 26,
            "status": "finished",
            "startDate": "1998-04-03",
        },
    }
    out = p._canonicalize_kitsu_media(raw)
    assert out is not None
    assert out["source"] == "kitsu"
    assert out["source_id"] == "1"
    assert out["averageScore"] == 82  # round(82.45) → 82
    assert out["seasonYear"] == 1998
    # Status uppercased; spaces replaced with underscores.
    assert out["status"] == "FINISHED"
    # siteUrl built from type + slug.
    assert "kitsu.io/anime/cowboy-bebop" in out["siteUrl"]


def test_canonicalize_kitsu_handles_malformed_rating():
    """A Kitsu response with rating=None or unparseable should default
    averageScore to None, not crash."""
    raw = {"id": "1", "attributes": {"averageRating": None, "startDate": ""}}
    out = p._canonicalize_kitsu_media(raw)
    assert out is not None
    assert out["averageScore"] is None
    assert out["seasonYear"] is None


# ── _search_media fallback chain ───────────────────────────────────────────


def test_search_media_returns_anilist_on_first_hit(monkeypatch):
    """Happy path: AniList returns a Media. No fallback fires."""
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 1, "title": {"romaji": "X", "english": "X"},
                                       "siteUrl": "https://anilist.co/anime/1"}},
    )
    jikan_called = {"n": 0}

    def _jikan_spy(*a, **kw):
        jikan_called["n"] += 1
        return None

    monkeypatch.setattr(p, "_jikan_query", _jikan_spy)
    monkeypatch.setattr(p, "_kitsu_query", _jikan_spy)
    ctx = MockContext()
    media = p._search_media(ctx, "X")
    assert media is not None
    assert media["source"] == "anilist"
    assert jikan_called["n"] == 0, "MAL fallback must not fire on AniList hit"


def test_search_media_falls_through_to_jikan(monkeypatch):
    """AniList misses (returns None for Media). MAL fallback wins."""
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Media": None})
    monkeypatch.setattr(
        p, "_jikan_query",
        lambda *a, **kw: [{
            "mal_id": 99, "title": "Y",
            "url": "https://myanimelist.net/anime/99",
        }],
    )
    kitsu_called = {"n": 0}

    def _kitsu_spy(*a, **kw):
        kitsu_called["n"] += 1
        return None

    monkeypatch.setattr(p, "_kitsu_query", _kitsu_spy)
    ctx = MockContext()
    media = p._search_media(ctx, "Y")
    assert media is not None
    assert media["source"] == "mal"
    assert media["source_id"] == 99
    assert kitsu_called["n"] == 0, "Kitsu fallback must not fire on MAL hit"


def test_search_media_falls_through_to_kitsu(monkeypatch):
    """AniList misses; MAL misses; Kitsu wins."""
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Media": None})
    monkeypatch.setattr(p, "_jikan_query", lambda *a, **kw: None)
    monkeypatch.setattr(
        p, "_kitsu_query",
        lambda *a, **kw: [{
            "id": "z", "type": "anime", "slug": "z",
            "attributes": {"canonicalTitle": "Z", "titles": {}, "averageRating": "70"},
        }],
    )
    ctx = MockContext()
    media = p._search_media(ctx, "Z")
    assert media is not None
    assert media["source"] == "kitsu"


def test_search_media_returns_none_when_all_three_miss(monkeypatch):
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Media": None})
    monkeypatch.setattr(p, "_jikan_query", lambda *a, **kw: None)
    monkeypatch.setattr(p, "_kitsu_query", lambda *a, **kw: None)
    ctx = MockContext()
    assert p._search_media(ctx, "missing") is None


# ── /anime + /manga route through _search_media ────────────────────────────


def test_anime_calls_search_media(monkeypatch):
    seen: list = []

    def _spy(ctx, query, *, media_type):
        seen.append((query, media_type))
        return {
            "source": "anilist", "source_id": 1, "id": 1,
            "title": {"romaji": "X", "english": "X"},
            "siteUrl": "https://anilist.co/anime/1",
            "averageScore": 80, "genres": [],
        }

    monkeypatch.setattr(p, "_search_media", _spy)
    ctx = MockContext()
    p.cmd_anime(ctx, _slash("anime", {"query": "X"}, user_id="u"))
    assert seen == [("X", "anime")]


def test_manga_calls_search_media(monkeypatch):
    seen: list = []

    def _spy(ctx, query, *, media_type):
        seen.append((query, media_type))
        return {
            "source": "anilist", "source_id": 2, "id": 2,
            "title": {"romaji": "Y", "english": "Y"},
            "siteUrl": "https://anilist.co/manga/2",
            "averageScore": 75, "genres": [],
        }

    monkeypatch.setattr(p, "_search_media", _spy)
    ctx = MockContext()
    p.cmd_manga(ctx, _slash("manga", {"query": "Y"}, user_id="u"))
    assert seen == [("Y", "manga")]


def test_anime_skips_last_anime_cache_on_mal_fallback(monkeypatch):
    """v9.1 known limitation: last_anime KV stores AniList IDs only.
    When a MAL fallback wins, we DON'T pollute the cache with a MAL ID
    (which would break /similar, /watch, /rate downstream)."""
    monkeypatch.setattr(
        p, "_search_media",
        lambda ctx, q, *, media_type: {
            "source": "mal", "source_id": 99, "id": 99,
            "title": {"romaji": "Z", "english": "Z"},
            "siteUrl": "https://myanimelist.net/anime/99",
            "averageScore": 70, "genres": [],
        },
    )
    ctx = MockContext()
    p.cmd_anime(ctx, _slash("anime", {"query": "Z"}, user_id="mal-user"))
    # No `last_anime` cache write since source != anilist.
    assert ctx.kv.get("last_anime:user:mal-user") is None


def test_anime_skips_similar_button_on_non_anilist_source(monkeypatch):
    """The /similar button uses AniList's recommendation graph keyed on
    AniList IDs. A MAL/Kitsu fallback result must NOT show the button."""
    monkeypatch.setattr(
        p, "_search_media",
        lambda ctx, q, *, media_type: {
            "source": "mal", "source_id": 99, "id": 99,
            "title": {"romaji": "Z", "english": "Z"},
            "siteUrl": "https://myanimelist.net/anime/99",
            "averageScore": 70, "genres": [],
        },
    )
    ctx = MockContext()
    p.cmd_anime(ctx, _slash("anime", {"query": "Z"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    serialized = json.dumps(
        follow.get("components") or [],
        default=lambda o: getattr(o, "__dict__", str(o)),
    )
    # No "Similar" button when source != anilist.
    assert "otaku:similar:" not in serialized


def test_anime_footer_attributes_source(monkeypatch):
    monkeypatch.setattr(
        p, "_search_media",
        lambda ctx, q, *, media_type: {
            "source": "mal", "source_id": 99, "id": 99,
            "title": {"romaji": "Z", "english": "Z"},
            "siteUrl": "https://myanimelist.net/anime/99",
            "averageScore": 70, "genres": [],
        },
    )
    ctx = MockContext()
    p.cmd_anime(ctx, _slash("anime", {"query": "Z"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    assert embed["footer"]["text"] == "Data from MyAnimeList"


def test_anime_open_button_label_reflects_source(monkeypatch):
    monkeypatch.setattr(
        p, "_search_media",
        lambda ctx, q, *, media_type: {
            "source": "kitsu", "source_id": "z", "id": "z",
            "title": {"romaji": "Z", "english": "Z"},
            "siteUrl": "https://kitsu.io/anime/z",
            "averageScore": 70, "genres": [],
        },
    )
    ctx = MockContext()
    p.cmd_anime(ctx, _slash("anime", {"query": "Z"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    serialized = json.dumps(
        follow.get("components") or [],
        default=lambda o: getattr(o, "__dict__", str(o)),
    )
    assert "Open on Kitsu" in serialized
    assert "Open on AniList" not in serialized


# ── Cache key extension ────────────────────────────────────────────────────


def test_cache_key_namespaces_by_source():
    """Calling with the SAME query+vars but DIFFERENT source produces
    different cache keys — so AniList ↔ Jikan responses don't collide."""
    anilist_key = p._cache_key("query string", {"q": "X"})
    jikan_key = p._cache_key("jikan", "/anime", {"q": "X"})
    kitsu_key = p._cache_key("kitsu", "/anime", {"q": "X"})
    assert anilist_key != jikan_key
    assert jikan_key != kitsu_key
    assert anilist_key != kitsu_key


def test_cache_max_entries_bumped_for_multi_source():
    """v9.1 bumped from 128 to 256 to accommodate ~3x working set."""
    assert p.ANILIST_CACHE_MAX_ENTRIES == 256

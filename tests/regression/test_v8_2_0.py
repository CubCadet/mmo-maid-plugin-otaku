"""Regression contract for otaku v8.2.0 — /studio.

IMMUTABLE — what shipped at v8.2.0:
- New /studio query:<name> slash command.
- Queries AniList's `Studio` type via QUERY_STUDIO; `isMain: true` so only
  main-production credits surface (not OP/ED composition or licensing).
- Fetches `media(perPage: 10, sort: POPULARITY_DESC, isMain: true)`.
- Embed builder `_make_studio_embed` splits the results into "Recent
  (≤ 2y)" and "Popular works" sections based on seasonYear vs current
  year. Up to 5 entries each section.
- `isAnimationStudio` flag toggles the header prefix between 🎬 (animation
  house) and 🏢 (other production org).
- Empty query → ephemeral usage hint. Studio not found → friendly error.
- Open-on-AniList link button when siteUrl is present.
- STUDIO_RECENT_WITHIN_YEARS = 2 (frozen).
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from mmo_maid_sdk.testing import MockContext, make_event


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


def test_manifest_includes_studio():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "studio" in names


def test_studio_query_option_required_string():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "studio")
    q = next(o for o in cmd["options"] if o["name"] == "query")
    assert q["required"] is True and q["type"] == 3


# ── Constants frozen ───────────────────────────────────────────────────────


def test_studio_recency_cutoff_is_two_years():
    assert p.STUDIO_RECENT_WITHIN_YEARS == 2


def test_query_studio_uses_anilist_studio_type():
    assert "Studio(search: $q)" in p.QUERY_STUDIO


def test_query_studio_filters_to_main_credits_only():
    """`isMain: true` so the studio's licensing/distribution credits don't
    pollute their production credits."""
    assert "isMain: true" in p.QUERY_STUDIO


def test_query_studio_sorts_by_popularity_desc_and_fetches_ten():
    """Pre-filter we fetch 10; embed splits into recent (≤2y) + catalog,
    each capped at 5."""
    assert "perPage: 10" in p.QUERY_STUDIO
    assert "sort: POPULARITY_DESC" in p.QUERY_STUDIO


def test_query_studio_requests_is_animation_studio_flag():
    """The header toggles 🎬 vs 🏢 based on this flag."""
    assert "isAnimationStudio" in p.QUERY_STUDIO


# ── /studio handler ────────────────────────────────────────────────────────


def test_studio_empty_query_short_circuits(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: (called.__setitem__("n", called["n"] + 1), None)[-1])
    ctx = MockContext()
    p.cmd_studio(ctx, _slash("studio", {"query": ""}, user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "Usage" in (resp.get("content") or "")
    assert called["n"] == 0


def test_studio_anilist_miss_surfaces_friendly_error(monkeypatch):
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Studio": None})
    ctx = MockContext()
    p.cmd_studio(ctx, _slash("studio", {"query": "Nobody Studios"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert "No studio found" in (follow.get("content") or "")


# ── _make_studio_embed contract ────────────────────────────────────────────


def _studio_payload(name: str = "Trigger", is_anim: bool = True, media: list | None = None) -> dict:
    return {
        "id": 1, "name": name, "isAnimationStudio": is_anim,
        "siteUrl": "https://anilist.co/studio/1",
        "media": {"nodes": media or []},
    }


def test_studio_animation_header_uses_film_emoji():
    studio = _studio_payload(name="Trigger", is_anim=True)
    embed = p._make_studio_embed(studio)
    assert embed["title"].startswith("🎬"), \
        "animation studios get the film-clapper prefix"
    assert "Trigger" in embed["title"]


def test_studio_non_animation_header_uses_office_emoji():
    studio = _studio_payload(name="Aniplex", is_anim=False)
    embed = p._make_studio_embed(studio)
    assert embed["title"].startswith("🏢"), \
        "non-animation production orgs (distributors, etc.) get the office prefix"
    assert "Aniplex" in embed["title"]


def test_studio_splits_recent_and_catalog():
    """Works within last 2 years go in 'Recent'; older ones in 'Popular works'."""
    from datetime import datetime, timezone
    now_year = datetime.now(timezone.utc).year
    studio = _studio_payload(media=[
        {
            "id": 1,
            "title": {"romaji": "Recent Hit", "english": "Recent Hit"},
            "seasonYear": now_year,
            "siteUrl": "https://anilist.co/anime/1",
        },
        {
            "id": 2,
            "title": {"romaji": "Old Classic", "english": "Old Classic"},
            "seasonYear": now_year - 10,
            "siteUrl": "https://anilist.co/anime/2",
        },
    ])
    embed = p._make_studio_embed(studio)
    field_names = {f["name"] for f in embed["fields"]}
    # Field name includes the cutoff window, e.g. "Recent (≤ 2y)".
    assert any("Recent" in n for n in field_names)
    assert "Popular works" in field_names
    serialized = json.dumps(embed)
    assert "Recent Hit" in serialized
    assert "Old Classic" in serialized


def test_studio_no_works_gracefully_empty():
    """A studio with no main-credit works renders an empty-state field, not
    a crash."""
    studio = _studio_payload(media=[])
    embed = p._make_studio_embed(studio)
    assert any(f["name"] == "Works" for f in embed["fields"])
    assert any("no main-work" in f["value"] for f in embed["fields"])


def test_studio_works_include_year_suffix():
    """Each work line includes the seasonYear suffix when AniList provides it."""
    from datetime import datetime, timezone
    now_year = datetime.now(timezone.utc).year
    studio = _studio_payload(media=[{
        "id": 5,
        "title": {"romaji": "Yearful", "english": "Yearful"},
        "seasonYear": now_year - 5,  # in catalog (old)
        "siteUrl": "https://anilist.co/anime/5",
    }])
    embed = p._make_studio_embed(studio)
    body = json.dumps(embed)
    assert f"({now_year - 5})" in body


def test_studio_open_on_anilist_button_appears(monkeypatch):
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Studio": _studio_payload()})
    ctx = MockContext()
    p.cmd_studio(ctx, _slash("studio", {"query": "Trigger"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    serialized = json.dumps(
        follow.get("components") or [],
        default=lambda o: getattr(o, "__dict__", str(o)),
    )
    assert "https://anilist.co/studio/1" in serialized
    assert "Open on AniList" in serialized

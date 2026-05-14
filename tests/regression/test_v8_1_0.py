"""Regression contract for otaku v8.1.0 — voice actors + staff.

IMMUTABLE — what shipped at v8.1.0:
- Two new slash commands: /voice-actor query:<name> and /staff query:<name>.
- Both query AniList's single `Staff` type via QUERY_STAFF; the embed
  builders pull different field framings of the same record:
  - /voice-actor surfaces `characters` (top 5 by FAVOURITES_DESC) with
    each character's most-popular parent media.
  - /staff surfaces `staffMedia.edges` (top 5 by POPULARITY_DESC) with
    each edge's `staffRole` prefix.
- Both commands honor the existing cooldown (`_on_cooldown`) and defer
  before the AniList round-trip.
- Empty `query:` short-circuits with a usage hint (ephemeral).
- AniList returning no Staff match → friendly "not found" error.
- Open-on-AniList link button when `siteUrl` is present.
- `_staff_display_name` formats `Full (Native)` when both present and
  different, falls back to whichever is set.

No new capabilities, no schema changes. Phase 8 read-only continuation
of v8.0.
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


def test_manifest_includes_voice_actor_and_staff():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "voice-actor" in names
    assert "staff" in names


def test_voice_actor_query_option_required_string():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "voice-actor")
    q = next(o for o in cmd["options"] if o["name"] == "query")
    assert q["required"] is True and q["type"] == 3


def test_staff_query_option_required_string():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "staff")
    q = next(o for o in cmd["options"] if o["name"] == "query")
    assert q["required"] is True and q["type"] == 3


# ── Query constant ─────────────────────────────────────────────────────────


def test_query_staff_uses_anilist_staff_type():
    assert "Staff(search: $q)" in p.QUERY_STAFF


def test_query_staff_fetches_characters_with_parent_media():
    """The /voice-actor embed needs characters + their parent media."""
    assert "characters(perPage: 5, sort: FAVOURITES_DESC)" in p.QUERY_STAFF
    assert "media(perPage: 1, sort: POPULARITY_DESC)" in p.QUERY_STAFF


def test_query_staff_fetches_staff_media_with_role():
    """The /staff embed needs staffMedia edges + each edge's staffRole."""
    assert "staffMedia(perPage: 5, sort: POPULARITY_DESC)" in p.QUERY_STAFF
    assert "staffRole" in p.QUERY_STAFF


# ── _staff_display_name contract ───────────────────────────────────────────


def test_display_name_renders_both_when_present_and_different():
    name = p._staff_display_name({"name": {"full": "Aoi Yuuki", "native": "悠木碧"}})
    assert name == "Aoi Yuuki (悠木碧)"


def test_display_name_collapses_when_only_full_present():
    name = p._staff_display_name({"name": {"full": "Hayao Miyazaki", "native": ""}})
    assert name == "Hayao Miyazaki"


def test_display_name_falls_back_to_unknown_when_both_empty():
    name = p._staff_display_name({"name": {}})
    assert name == "Unknown"


# ── /voice-actor handler ───────────────────────────────────────────────────


def test_voice_actor_empty_query_short_circuits(monkeypatch):
    """Empty query must short-circuit BEFORE any AniList call."""
    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return None

    monkeypatch.setattr(p, "_anilist_query", _spy)
    ctx = MockContext()
    p.cmd_voice_actor(ctx, _slash("voice-actor", {"query": ""}, user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "Usage" in (resp.get("content") or "")
    assert called["n"] == 0


def test_voice_actor_anilist_miss_surfaces_friendly_error(monkeypatch):
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Staff": None})
    ctx = MockContext()
    p.cmd_voice_actor(ctx, _slash("voice-actor", {"query": "nobody"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert "No voice actor found" in (follow.get("content") or "")


def test_voice_actor_renders_character_roles_with_parent(monkeypatch):
    """Happy path: each character role shows the character name + parent media link."""
    staff_payload = {
        "id": 1,
        "name": {"full": "Aoi Yuuki", "native": "悠木碧"},
        "image": {"large": "u"},
        "description": "Japanese voice actress.",
        "primaryOccupations": ["Voice Actor"],
        "languageV2": "Japanese",
        "siteUrl": "https://anilist.co/staff/1",
        "characters": {
            "nodes": [
                {
                    "id": 11,
                    "name": {"full": "Madoka Kaname"},
                    "media": {"nodes": [
                        {
                            "id": 100,
                            "title": {
                                "romaji": "Mahou Shoujo Madoka Magica",
                                "english": "Puella Magi Madoka Magica",
                            },
                            "siteUrl": "https://anilist.co/anime/100",
                        },
                    ]},
                },
            ],
        },
        "staffMedia": {"edges": []},
    }
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Staff": staff_payload})
    ctx = MockContext()
    p.cmd_voice_actor(ctx, _slash("voice-actor", {"query": "Aoi Yuuki"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    body = json.dumps(embed)
    assert "Aoi Yuuki" in body
    assert "Madoka Kaname" in body
    assert "Madoka Magica" in body
    # Language field surfaces when AniList provides it.
    assert "Japanese" in body


def test_voice_actor_handles_no_roles_gracefully(monkeypatch):
    """A staff with no character entries renders an empty-state line, not a crash."""
    staff_payload = {
        "name": {"full": "Mystery VA", "native": ""},
        "image": None,
        "description": None,
        "primaryOccupations": [],
        "languageV2": "",
        "siteUrl": "",
        "characters": {"nodes": []},
        "staffMedia": {"edges": []},
    }
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Staff": staff_payload})
    ctx = MockContext()
    p.cmd_voice_actor(ctx, _slash("voice-actor", {"query": "Mystery VA"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    body = json.dumps(follow["embeds"][0])
    assert "Mystery VA" in body
    # The empty-state filler appears so users see "we looked but nothing notable."
    assert "no notable character" in body or "no notable" in body


# ── /staff handler ─────────────────────────────────────────────────────────


def test_staff_empty_query_short_circuits(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: (called.__setitem__("n", called["n"] + 1), None)[-1])
    ctx = MockContext()
    p.cmd_staff(ctx, _slash("staff", {"query": "  "}, user_id="u"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    assert "Usage" in (resp.get("content") or "")
    assert called["n"] == 0


def test_staff_renders_production_credits_with_role(monkeypatch):
    """Happy path: each production credit shows the staffRole + media title link."""
    staff_payload = {
        "id": 2,
        "name": {"full": "Hayao Miyazaki", "native": "宮崎駿"},
        "image": {"large": "u"},
        "description": "Director.",
        "primaryOccupations": ["Director", "Writer"],
        "languageV2": "Japanese",
        "siteUrl": "https://anilist.co/staff/2",
        "characters": {"nodes": []},
        "staffMedia": {
            "edges": [
                {
                    "staffRole": "Director",
                    "node": {
                        "id": 200,
                        "title": {
                            "romaji": "Sen to Chihiro no Kamikakushi",
                            "english": "Spirited Away",
                        },
                        "siteUrl": "https://anilist.co/anime/200",
                    },
                },
                {
                    "staffRole": "Director, Original Creator",
                    "node": {
                        "id": 201,
                        "title": {
                            "romaji": "Mononoke Hime",
                            "english": "Princess Mononoke",
                        },
                        "siteUrl": "https://anilist.co/anime/201",
                    },
                },
            ],
        },
    }
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Staff": staff_payload})
    ctx = MockContext()
    p.cmd_staff(ctx, _slash("staff", {"query": "Hayao Miyazaki"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    body = json.dumps(embed)
    assert "Hayao Miyazaki" in body
    assert "Director" in body
    assert "Spirited Away" in body
    assert "Princess Mononoke" in body


def test_staff_anilist_miss_surfaces_friendly_error(monkeypatch):
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Staff": None})
    ctx = MockContext()
    p.cmd_staff(ctx, _slash("staff", {"query": "nobody"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    assert "No staff found" in (follow.get("content") or "")


def test_staff_open_on_anilist_button_appears_when_site_url_present(monkeypatch):
    staff_payload = {
        "name": {"full": "X", "native": ""},
        "image": None, "description": None, "primaryOccupations": [],
        "languageV2": "", "siteUrl": "https://anilist.co/staff/3",
        "characters": {"nodes": []}, "staffMedia": {"edges": []},
    }
    monkeypatch.setattr(p, "_anilist_query", lambda *a, **kw: {"Staff": staff_payload})
    ctx = MockContext()
    p.cmd_staff(ctx, _slash("staff", {"query": "X"}, user_id="u"))
    follow = ctx.interaction.followups[-1]
    serialized = json.dumps(
        follow.get("components") or [],
        default=lambda o: getattr(o, "__dict__", str(o)),
    )
    assert "https://anilist.co/staff/3" in serialized
    assert "Open on AniList" in serialized


# ── Shared query routing — both commands use the same constant ─────────────


def test_both_commands_route_to_query_staff(monkeypatch):
    """Both /voice-actor and /staff must hit QUERY_STAFF (single Staff type
    on AniList; embed-side differentiation only)."""
    seen: list = []
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda _ctx, query, variables=None, cache=False: (
            seen.append(query),
            {"Staff": None},
        )[-1],
    )
    # Use different user_ids so the per-user cooldown doesn't block the
    # second call.
    ctx = MockContext()
    p.cmd_voice_actor(ctx, _slash("voice-actor", {"query": "a"}, user_id="va-user"))
    p.cmd_staff(ctx, _slash("staff", {"query": "b"}, user_id="staff-user"))
    assert seen == [p.QUERY_STAFF, p.QUERY_STAFF]

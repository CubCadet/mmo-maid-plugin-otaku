"""Regression contract for otaku v1.1.0.

IMMUTABLE — describes the user-visible behavior that shipped at v1.1.0:
- /random [genre] returns one anime and caches the resolved id
- /character <query> returns a character card with image + media
- Both commands present in manifest.json.slash_commands
"""
from __future__ import annotations

import json
from pathlib import Path

import plugin_main as p
from yourbot_sdk.testing import MockContext, make_event


def _slash(name: str, options: dict | None = None, **extra) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name=name,
        options=[{"name": k, "value": v} for k, v in (options or {}).items()],
        **extra,
    )


def _mock(ctx: MockContext, data: dict) -> None:
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": data}))


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


# ── Manifest contract ───────────────────────────────────────────────────────

def test_manifest_includes_random_and_character():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert {"random", "character"}.issubset(names)


def test_random_command_has_optional_genre():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "random")
    opts = {o["name"]: o for o in cmd.get("options", [])}
    assert "genre" in opts and opts["genre"].get("required") is not True


def test_character_command_requires_query():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "character")
    opts = {o["name"]: o for o in cmd.get("options", [])}
    assert opts["query"]["required"] is True


# ── /random ─────────────────────────────────────────────────────────────────

def test_random_returns_one_anime_and_caches():
    ctx = MockContext()
    media = {
        "id": 777,
        "title": {"romaji": "Random Pick", "english": ""},
        "description": "",
        "coverImage": {"large": ""},
        "bannerImage": None,
        "averageScore": 70,
        "popularity": 100,
        "format": "TV",
        "episodes": 12,
        "status": "FINISHED",
        "season": "SPRING",
        "seasonYear": 2020,
        "genres": ["Action"],
        "siteUrl": "https://anilist.co/anime/777",
    }
    _mock(ctx, {"Page": {
        "pageInfo": {"lastPage": 5, "hasNextPage": True},
        "media": [media],
    }})

    p.cmd_random(ctx, _slash("random", {"genre": "Action"}, user_id="reg-rnd"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("embeds")
    assert ctx.kv.get("last_anime:user:reg-rnd") == 777


# ── /character ──────────────────────────────────────────────────────────────

def test_character_returns_card_with_image_and_appearances():
    ctx = MockContext()
    _mock(ctx, {"Character": {
        "id": 1,
        "name": {"full": "Edward Elric", "native": "エドワード・エルリック"},
        "image": {"large": "https://img.example.com/ed.jpg"},
        "description": "The Fullmetal Alchemist.",
        "siteUrl": "https://anilist.co/character/1",
        "media": {"nodes": [
            {"id": 5114, "title": {"romaji": "Hagane no Renkinjutsushi", "english": "FMA: Brotherhood"},
             "siteUrl": "https://anilist.co/anime/5114"},
        ]},
    }})

    p.cmd_character(ctx, _slash("character", {"query": "edward"}, user_id="reg-chr"))

    follow = ctx.interaction.followups[-1]
    embed = follow["embeds"][0]
    assert "Edward" in embed["title"]
    assert embed["thumbnail"]["url"].startswith("https://")
    assert any(f["name"] == "Appears in" for f in embed.get("fields") or [])


def test_character_no_result_is_ephemeral():
    ctx = MockContext()
    _mock(ctx, {"Character": None})

    p.cmd_character(ctx, _slash("character", {"query": "zzz"}, user_id="reg-chr2"))

    follow = ctx.interaction.followups[-1]
    assert follow.get("ephemeral") is True

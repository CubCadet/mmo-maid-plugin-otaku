"""Regression contract for otaku v2.4.0 — bulk import.

IMMUTABLE — what shipped at v2.4.0:
- /import anilist <username> slash command (anilist is required string).
- Status map (AniList → ours): CURRENT/REPEATING → watching, COMPLETED → completed,
  PAUSED → on_hold, DROPPED → dropped, PLANNING → plan.
- Score (POINT_10) converts to our SMALLINT rating via score × 2 (range 2..20).
- Re-importing updates existing rows; never creates duplicates (PRIMARY KEY).
- is_favorite is left alone on re-import.
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


def _manifest() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text())


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_includes_import():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "import" in names


def test_import_anilist_option_is_required_string():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "import")
    opt = next(o for o in cmd["options"] if o["name"] == "anilist")
    assert opt["type"] == 3  # STRING
    assert opt["required"] is True


# ── Status mapping ──────────────────────────────────────────────────────────


def test_anilist_status_mapping():
    assert p.ANILIST_STATUS_MAP["CURRENT"] == "watching"
    assert p.ANILIST_STATUS_MAP["REPEATING"] == "watching"
    assert p.ANILIST_STATUS_MAP["COMPLETED"] == "completed"
    assert p.ANILIST_STATUS_MAP["PAUSED"] == "on_hold"
    assert p.ANILIST_STATUS_MAP["DROPPED"] == "dropped"
    assert p.ANILIST_STATUS_MAP["PLANNING"] == "plan"


def test_row_from_medialist_encodes_score_x2():
    row = p._row_from_medialist(
        {"status": "COMPLETED", "progress": 12, "score": 9.0, "media": {"id": 101}}
    )
    assert row == (101, "completed", 12, 18)


def test_row_from_medialist_returns_none_for_missing_id():
    row = p._row_from_medialist({"status": "CURRENT", "progress": 0, "score": 0, "media": {}})
    assert row is None


# ── Re-import upserts without duplicating ───────────────────────────────────


def test_import_writes_upsert_preserving_is_favorite():
    ctx = MockContext()
    ctx.http.mock_response("graphql.anilist.co", status=200, body=json.dumps({"data": {"Page": {
        "pageInfo": {"hasNextPage": False, "currentPage": 1},
        "mediaList": [
            {"status": "COMPLETED", "progress": 12, "score": 8, "media": {"id": 555}},
        ],
    }}}))

    p.cmd_import(ctx, _slash("import", {"anilist": "reg-user"}, user_id="reg-imp"))

    # regression-fix (v8.0.0): otaku_user_anime renamed to otaku_user_media.
    inserts = [c for c in ctx.sql.executed if "INSERT INTO otaku_user_media" in c["sql"]]
    assert inserts
    # Importantly, the UPDATE clause must NOT touch is_favorite.
    assert "is_favorite" not in inserts[0]["sql"].split("DO UPDATE SET")[1]
    # And rating is COALESCE'd so a zero/null score doesn't wipe a previously-set rating.
    assert "COALESCE(EXCLUDED.rating" in inserts[0]["sql"]

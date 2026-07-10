"""v11.0.0 regression contracts — reserved-name renames + TTL determinism.

The platform (mirrored by yourbot-sdk 0.8.3's vendored publish gate) now
reserves /stats, /poll, and /leaderboard for built-in YourBot plugins —
artifact_store_put refuses any zip that declares them. Pinned here:

  (1) The three commands are renamed otaku-stats / otaku-poll /
      otaku-leaderboard in BOTH manifest.json and the @on_slash_command
      decorators (the gate applies to the union of the two, so a legacy
      decorator alias would still block publish). /my-stats is unchanged.
  (2) Live parity guard: NO command name (manifest or decorator) may appear
      in the SDK's _RESERVED_COMMAND_NAMES — so a future reserved-list
      growth that collides with us fails HERE, not at upload.
  (3) User-facing strings that named /poll (S.POLL_NOT_ADMIN,
      S.POLL_VOTE_CLOSED, the poll-status option description) now say
      /otaku-poll — after the rename, /poll belongs to the platform's
      built-in, and pointing voters there would misdirect them.
  (4) _route_components forwards ALL otaku:* component ids to
      _component_dispatch (single prefix list — the dual-list footgun is
      gone): otaku:expand stays excluded (owned by @on_component), unknown
      otaku:* ids are a silent no-op, non-otaku ids are never forwarded.
  (5) TTL behavior is deterministic under MockClock: the 2s command
      cooldown replies then expires, airing dedup re-opens after
      NOTIFY_DEDUP_TTL, and the last-anime / genres KV caches expire after
      LAST_ANIME_TTL / GENRES_TTL.

These contracts are immutable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import plugin_main as p
from yourbot_sdk.testing import MockClock, MockContext, make_event

_REPO = Path(__file__).resolve().parents[2]

RENAMED = {"otaku-stats", "otaku-poll", "otaku-leaderboard"}
OLD_NAMES = {"stats", "poll", "leaderboard"}

# Same pattern the SDK's publish gate (and the platform's auto-merge) scans
# source with — vendored so this contract can't drift from what upload sees.
_SLASH_CMD_RE = re.compile(
    r'@\w+\.on_slash_command\s*\(\s*["\']([a-zA-Z0-9_\-]+)["\']\s*\)'
)


def _manifest() -> dict:
    return json.loads((_REPO / "manifest.json").read_text())


def _manifest_names() -> set[str]:
    return {c["name"] for c in _manifest()["slash_commands"]}


def _decorator_names() -> set[str]:
    return set(_SLASH_CMD_RE.findall((_REPO / "__main__.py").read_text()))


# ── (1) the renames themselves ──────────────────────────────────────────────

def test_renamed_commands_declared_and_old_names_gone():
    names = _manifest_names()
    assert RENAMED <= names
    assert not (OLD_NAMES & names), "reserved names must not reappear"
    assert "my-stats" in names, "/my-stats is NOT reserved and must survive"


def test_decorators_match_manifest_exactly():
    """Manifest ↔ decorator drift ships a command that never answers."""
    assert _decorator_names() == _manifest_names()


# ── (2) live reserved-name parity guard ─────────────────────────────────────

def test_no_command_name_is_platform_reserved():
    from yourbot_sdk._validation import _RESERVED_COMMAND_NAMES

    taken = (_manifest_names() | _decorator_names()) & _RESERVED_COMMAND_NAMES
    assert not taken, (
        f"{sorted(taken)} are reserved by built-in YourBot plugins; "
        "the platform refuses the zip at upload (artifact_store_put raises)"
    )


# ── (3) user-facing strings point at the new name ───────────────────────────

def test_poll_strings_reference_otaku_poll():
    assert "`/otaku-poll {action}`" in p.S.POLL_NOT_ADMIN
    assert "`/otaku-poll status id: {poll_id}`" in p.S.POLL_VOTE_CLOSED
    # "`/poll" would now point users at the platform's built-in /poll.
    assert "`/poll" not in p.S.POLL_NOT_ADMIN
    assert "`/poll" not in p.S.POLL_VOTE_CLOSED


def test_poll_status_option_description_updated():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "otaku-poll")
    status = next(o for o in cmd["options"] if o["name"] == "status")
    id_opt = next(o for o in status["options"] if o["name"] == "id")
    assert "/otaku-poll create" in id_opt["description"]


# ── (4) single-prefix component routing ─────────────────────────────────────

def _component_event(custom_id: str, itype: int = 3) -> dict:
    return make_event(
        "interaction_create", interaction_type=itype,
        custom_id=custom_id, user_id="router-u",
    )


def test_route_components_forwards_any_otaku_prefix(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        p, "_component_dispatch", lambda ctx, ev: seen.append(ev["custom_id"])
    )
    ctx = MockContext()
    p._route_components(ctx, _component_event("otaku:page:Action:POP:2"))
    p._route_components(ctx, _component_event("otaku:poll-vote:7:a"))
    # New in v11: prefixes need only ONE registration site (_component_dispatch);
    # an id unknown to the dispatcher is still forwarded and no-ops there.
    p._route_components(ctx, _component_event("otaku:not-a-real-prefix:1"))
    assert seen == [
        "otaku:page:Action:POP:2",
        "otaku:poll-vote:7:a",
        "otaku:not-a-real-prefix:1",
    ]


def test_route_components_still_excludes_expand_foreign_and_modals(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        p, "_component_dispatch", lambda ctx, ev: seen.append(ev["custom_id"])
    )
    ctx = MockContext()
    p._route_components(ctx, _component_event("otaku:expand"))       # @on_component owns it
    p._route_components(ctx, _component_event("otherplugin:button")) # not ours
    p._route_components(ctx, _component_event("otaku:page:A:POP:1", itype=5))  # modal, not component
    assert seen == []


def test_unknown_otaku_component_is_silent_noop():
    """Dispatch fall-through must not respond or defer (matches the old
    filter's behavior of ignoring unregistered ids)."""
    ctx = MockContext()
    p._component_dispatch(ctx, _component_event("otaku:not-a-real-prefix:1"))
    assert ctx.interaction.responses == []
    assert ctx.interaction.defers == []


def test_review_modal_still_routed(monkeypatch):
    called: list[dict] = []
    monkeypatch.setattr(p, "_handle_review_submit", lambda ctx, ev: called.append(ev))
    p._route_components(MockContext(), _component_event("otaku:review-modal:42", itype=5))
    assert len(called) == 1


# ── (5) deterministic TTLs under MockClock ──────────────────────────────────

def test_cooldown_replies_then_expires():
    clock = MockClock(start=1_000.0)
    ctx = MockContext(clock=clock)

    assert p._on_cooldown(ctx, "cd-user") is False   # first call claims
    assert p._on_cooldown(ctx, "cd-user") is True    # inside the window
    wait = ctx.interaction.responses[-1]
    assert wait["ephemeral"] is True
    assert wait["content"] == p.S.COOLDOWN_WAIT.format(
        remaining=int(p.COOLDOWN_SECONDS)
    )

    clock.advance(p.COOLDOWN_SECONDS + 0.1)
    assert p._on_cooldown(ctx, "cd-user") is False   # window expired


def _airing_payload(media_id: int, episode: int) -> dict:
    """Minimal AniList airingSchedules payload (mirrors tests/test_plugin.py).

    airingAt uses WALL clock on purpose: _dispatch_airing_announcements
    computes its lookahead window from datetime.now(), while only the dedup
    claim runs on the ctx clock — exactly the split this contract exercises.
    """
    import datetime as _dt
    now_ts = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return {
        "Page": {
            "pageInfo": {"hasNextPage": False},
            "airingSchedules": [
                {
                    "id": 1,
                    "episode": episode,
                    "airingAt": now_ts,
                    "media": {
                        "id": media_id,
                        "episodes": 12,
                        "siteUrl": f"https://anilist.co/anime/{media_id}",
                        "title": {"romaji": "Show", "english": ""},
                        "coverImage": {"large": "https://img.example.com/a.jpg"},
                    },
                },
            ],
        },
    }


def test_airing_dedup_reopens_after_ttl():
    """Drives the REAL call site (__main__.py `_dispatch_airing_announcements`)
    so the ttl_seconds it passes is pinned — a hardcoded TTL there would
    break the reopen timing asserted below."""
    clock = MockClock(start=1_000.0)
    ctx = MockContext(clock=clock)
    ctx.kv.set(p.NOTIFY_CHANNEL_KV, "announce-1")
    ctx.http.mock_response(
        "graphql.anilist.co", status=200,
        body=json.dumps({"data": _airing_payload(4242, 7)}),
    )
    ctx.sql.query = lambda sql, params=None: [
        {"user_id": "alice", "channel_id": "chan-1"},
    ]

    assert p._dispatch_airing_announcements(ctx) == 1   # sends + claims dedup
    assert p._dispatch_airing_announcements(ctx) == 0   # inside the window
    # The claim the plugin just wrote must expire exactly NOTIFY_DEDUP_TTL
    # from claim time (binds the call site's ttl_seconds= argument).
    key = p._airing_dedup_key(4242, 7)
    assert ctx.ephemeral._seen[key] == 1_000.0 + p.NOTIFY_DEDUP_TTL

    clock.advance(p.NOTIFY_DEDUP_TTL + 1)
    assert p._dispatch_airing_announcements(ctx) == 1   # window reopened
    assert len(ctx.discord.messages_sent) == 2


def test_last_anime_kv_expires_to_none():
    clock = MockClock(start=1_000.0)
    ctx = MockContext(clock=clock)
    ctx.kv.set("last_anime:user:u1", 777, ttl_seconds=p.LAST_ANIME_TTL)

    assert p._resolve_last_anime_id(ctx, "u1") == 777
    clock.advance(p.LAST_ANIME_TTL + 1)
    assert p._resolve_last_anime_id(ctx, "u1") is None


def test_genres_cache_expiry_falls_back_to_fetch(monkeypatch):
    clock = MockClock(start=1_000.0)
    ctx = MockContext(clock=clock)
    fetches: list[dict] = []
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **k: fetches.append({}) or {"GenreCollection": ["Action", "Drama"]},
    )
    ctx.kv.set(p.GENRES_KV_KEY, ["Action", "Drama"], ttl_seconds=p.GENRES_TTL)

    event = make_event(
        "interaction_create", interaction_type=2,
        command_name="genres", options=[], user_id="g-user",
    )
    p.cmd_genres(ctx, event)
    assert fetches == [], "within TTL the cached list must serve the reply"
    assert ctx.interaction.responses, "cached path responds directly"

    clock.advance(p.GENRES_TTL + 1)  # also clears g-user's 2s cooldown
    p.cmd_genres(ctx, event)
    assert len(fetches) == 1, "expired cache must fall through to AniList"
    # The re-cache write must carry the plugin's TTL (binds cmd_genres'
    # ttl_seconds= argument, not just the mock's expiry mechanics).
    write = ctx.kv.writes[-1]
    assert write["key"] == p.GENRES_KV_KEY
    assert write["ttl_seconds"] == p.GENRES_TTL

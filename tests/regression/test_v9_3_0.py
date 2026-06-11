"""Regression contract for otaku v9.3.0 — spoiler control + /preferences.

IMMUTABLE — what shipped at v9.3.0:
- New `_redact_spoilers(text, *, show_unhidden=False)` helper. Wraps lines
  beginning with SPOILER: / [SPOILER] / (spoiler) / # spoiler (case-
  insensitive) in Discord's `||...||` syntax. Idempotent — never
  double-wraps content that already contains `||`. Empty input + empty
  body lines pass through unchanged. The `show_unhidden=True` opt-out
  returns text verbatim.
- `_render_reviews(viewer_id=...)` reads the viewer's per-user
  `pref:spoilers:user:<id>` KV setting and applies `_redact_spoilers`
  to BOTH the title and body of every rendered review. Default is
  "hide"; the v9.3 contract is "safe by default, opt-in to see plain".
- New `/preferences [language: <choice>] [spoilers: <choice>]` slash
  command. Both options optional; calling with no options just shows
  current state. Updates are per-user, persisted in KV (`pref:lang:
  user:<id>`, `pref:spoilers:user:<id>`).
- v9.3 ships the LANGUAGE preference SCAFFOLD but doesn't translate
  anything — explicitly documented in the embed and CHANGELOG. The
  preference is reserved for v9.x/v10 when a translation proxy lands.
- v9.2 (AI summaries) was SKIPPED per the roadmap fallback: the SDK
  doesn't expose an LLM proxy capability. v9.3 ships only the
  proxy-free portions of the original v9.3 spec.
- Constants frozen: PREF_LANGUAGE_KV, PREF_SPOILERS_KV,
  PREF_LANGUAGE_CHOICES (7 entries), PREF_SPOILERS_CHOICES (2),
  PREF_SPOILERS_DEFAULT="hide".
- No new capabilities. Uses existing `storage:kv` + `interaction:respond`.
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


def test_manifest_includes_preferences():
    names = {c["name"] for c in _manifest().get("slash_commands", [])}
    assert "preferences" in names


def test_preferences_options_both_optional():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "preferences")
    opts = {o["name"]: o for o in cmd.get("options", [])}
    assert "language" in opts and opts["language"]["required"] is False
    assert "spoilers" in opts and opts["spoilers"]["required"] is False


def test_preferences_language_choices_match_constant():
    """Choice list in manifest must match the in-code PREF_LANGUAGE_CHOICES."""
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "preferences")
    lang = next(o for o in cmd["options"] if o["name"] == "language")
    manifest_choices = {c["value"] for c in lang.get("choices", [])}
    assert manifest_choices == set(p.PREF_LANGUAGE_CHOICES)


def test_preferences_spoilers_choices_match_constant():
    cmd = next(c for c in _manifest()["slash_commands"] if c["name"] == "preferences")
    spo = next(o for o in cmd["options"] if o["name"] == "spoilers")
    manifest_choices = {c["value"] for c in spo.get("choices", [])}
    assert manifest_choices == set(p.PREF_SPOILERS_CHOICES)


# ── Constants frozen ───────────────────────────────────────────────────────


def test_pref_kv_prefixes_frozen():
    assert p.PREF_LANGUAGE_KV == "pref:lang:user"
    assert p.PREF_SPOILERS_KV == "pref:spoilers:user"


def test_pref_spoilers_default_is_hide():
    assert p.PREF_SPOILERS_DEFAULT == "hide"


def test_pref_language_choices_count():
    """v9.3 ships with 7 languages. Adding more is fine; dropping any
    would be a breaking change."""
    assert len(p.PREF_LANGUAGE_CHOICES) >= 7


# ── _redact_spoilers contract ──────────────────────────────────────────────


def test_redact_spoilers_wraps_explicit_spoiler_prefix():
    out = p._redact_spoilers("SPOILER: the protagonist dies")
    assert "||the protagonist dies||" in out


def test_redact_spoilers_handles_bracketed_marker():
    out = p._redact_spoilers("[SPOILER] big reveal")
    assert "||big reveal||" in out


def test_redact_spoilers_handles_parenthesised_marker():
    out = p._redact_spoilers("(spoiler) ending detail")
    assert "||ending detail||" in out


def test_redact_spoilers_case_insensitive():
    out_lower = p._redact_spoilers("spoiler: x")
    out_upper = p._redact_spoilers("SPOILER: x")
    out_mixed = p._redact_spoilers("Spoiler: x")
    assert "||x||" in out_lower
    assert "||x||" in out_upper
    assert "||x||" in out_mixed


def test_redact_spoilers_idempotent_on_pre_wrapped_content():
    """If the user already wrapped the spoiler body, leave it alone —
    don't produce `||||content||||`."""
    pre_wrapped = "SPOILER: ||already hidden||"
    out = p._redact_spoilers(pre_wrapped)
    assert out == pre_wrapped


def test_redact_spoilers_show_unhidden_passes_through():
    """show_unhidden=True is the opt-out — returns text verbatim."""
    text = "SPOILER: x"
    assert p._redact_spoilers(text, show_unhidden=True) == text


def test_redact_spoilers_empty_input_returns_empty():
    assert p._redact_spoilers("") == ""


def test_redact_spoilers_non_spoiler_lines_pass_through():
    """Lines without a spoiler marker are unchanged."""
    text = "Great anime. Loved the OST. 10/10."
    assert p._redact_spoilers(text) == text


def test_redact_spoilers_bare_marker_with_no_body_kept_intact():
    """`SPOILER:` with nothing after isn't a meaningful redaction target."""
    out = p._redact_spoilers("SPOILER:")
    assert "||||" not in out  # never produce empty wrap


def test_redact_spoilers_preserves_non_spoiler_lines_in_multiline():
    """Multi-line text mixes spoiler + non-spoiler lines correctly."""
    text = "Loved this show.\nSPOILER: the ending\nWould rewatch."
    out = p._redact_spoilers(text)
    assert "Loved this show." in out
    assert "||the ending||" in out
    assert "Would rewatch." in out


# ── /preferences handler ────────────────────────────────────────────────────


def test_preferences_view_shows_defaults_for_new_user():
    ctx = MockContext()
    p.cmd_preferences(ctx, _slash("preferences", {}, user_id="new"))
    resp = ctx.interaction.responses[-1]
    assert resp.get("ephemeral") is True
    embed = resp["embeds"][0]
    body = embed["description"]
    # Default spoilers = "hide".
    assert "`hide`" in body
    # Language not set.
    assert "(not set)" in body


def test_preferences_set_spoilers_show_persists_to_kv():
    ctx = MockContext()
    p.cmd_preferences(ctx, _slash("preferences", {"spoilers": "show"}, user_id="u1"))
    assert ctx.kv.get(f"{p.PREF_SPOILERS_KV}:u1") == "show"
    # Embed reflects the new state.
    resp = ctx.interaction.responses[-1]
    body = resp["embeds"][0]["description"]
    assert "`show`" in body


def test_preferences_set_language_persists_to_kv():
    ctx = MockContext()
    p.cmd_preferences(ctx, _slash("preferences", {"language": "ja"}, user_id="u2"))
    assert ctx.kv.get(f"{p.PREF_LANGUAGE_KV}:u2") == "ja"
    body = ctx.interaction.responses[-1]["embeds"][0]["description"]
    assert "`ja`" in body


def test_preferences_set_both_in_one_call():
    ctx = MockContext()
    p.cmd_preferences(
        ctx, _slash("preferences", {"language": "ko", "spoilers": "show"}, user_id="u3"),
    )
    assert ctx.kv.get(f"{p.PREF_LANGUAGE_KV}:u3") == "ko"
    assert ctx.kv.get(f"{p.PREF_SPOILERS_KV}:u3") == "show"


def test_preferences_rejects_invalid_language():
    """A bogus language value passed somehow must not write to KV."""
    ctx = MockContext()
    p.cmd_preferences(
        ctx, _slash("preferences", {"language": "klingon"}, user_id="u4"),
    )
    assert ctx.kv.get(f"{p.PREF_LANGUAGE_KV}:u4") is None


def test_preferences_view_after_set_shows_translation_note():
    """The language-pref note must surface so users understand it's
    a scaffold (no translation yet)."""
    ctx = MockContext()
    p.cmd_preferences(ctx, _slash("preferences", {"language": "es"}, user_id="u5"))
    body = ctx.interaction.responses[-1]["embeds"][0]["description"]
    assert "translate" in body.lower() or "translation" in body.lower()


# ── _get_pref_* helpers ────────────────────────────────────────────────────


def test_get_pref_spoilers_returns_hide_by_default():
    ctx = MockContext()
    assert p._get_pref_spoilers(ctx, "fresh-user") == "hide"


def test_get_pref_spoilers_returns_stored_show():
    ctx = MockContext()
    ctx.kv.set("pref:spoilers:user:opted-in", "show")
    assert p._get_pref_spoilers(ctx, "opted-in") == "show"


def test_get_pref_spoilers_rejects_garbage_kv_value():
    """A stored value not in PREF_SPOILERS_CHOICES is treated as unset
    (returns default). Defense against tampered/corrupted KV."""
    ctx = MockContext()
    ctx.kv.set("pref:spoilers:user:corrupt", "lolnope")
    assert p._get_pref_spoilers(ctx, "corrupt") == "hide"


def test_get_pref_language_returns_none_when_unset():
    ctx = MockContext()
    assert p._get_pref_language(ctx, "no-pref") is None


def test_get_pref_language_returns_stored_code():
    ctx = MockContext()
    ctx.kv.set("pref:lang:user:multi", "fr")
    assert p._get_pref_language(ctx, "multi") == "fr"


# ── /reviews integration: spoiler redaction per viewer ─────────────────────


def _slash_reviews(anime: str, viewer: str) -> dict:
    return _slash("reviews", {"anime": anime}, user_id=viewer)


def test_reviews_redacts_spoilers_for_default_viewer(monkeypatch):
    """A viewer who hasn't set spoilers: show sees ||wrapped|| spoilers."""
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 42, "title": {"romaji": "X", "english": "X"}}},
    )
    ctx = MockContext()

    def _q(sql, params=None):
        if "COUNT(*)" in sql:
            return [{"n": 1}]
        return [{
            "user_id": "author",
            "title": "Solid",
            "body": "Great visuals.\nSPOILER: the dog dies.",
            "created_at": None, "updated_at": None,
        }]

    ctx.sql.query = _q
    p.cmd_reviews(ctx, _slash_reviews("42", "default-viewer"))
    follow = ctx.interaction.followups[-1]
    body = json.dumps(follow["embeds"][0])
    assert "||the dog dies.||" in body
    # Non-spoiler line still rendered.
    assert "Great visuals" in body


def test_reviews_shows_plain_for_show_pref_viewer(monkeypatch):
    """A viewer with spoilers: show sees the raw text."""
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 42, "title": {"romaji": "X", "english": "X"}}},
    )
    ctx = MockContext()
    ctx.kv.set("pref:spoilers:user:plain-viewer", "show")

    def _q(sql, params=None):
        if "COUNT(*)" in sql:
            return [{"n": 1}]
        return [{
            "user_id": "author",
            "title": "Solid",
            "body": "SPOILER: the dog dies.",
            "created_at": None, "updated_at": None,
        }]

    ctx.sql.query = _q
    p.cmd_reviews(ctx, _slash_reviews("42", "plain-viewer"))
    follow = ctx.interaction.followups[-1]
    body = json.dumps(follow["embeds"][0])
    # Plain text — no Discord spoiler wrap.
    assert "||" not in body or body.count("||") == 0
    assert "the dog dies." in body


def test_reviews_redaction_applies_to_title_too(monkeypatch):
    """A spoiler-prefixed review TITLE must also be redacted."""
    monkeypatch.setattr(
        p, "_anilist_query",
        lambda *a, **kw: {"Media": {"id": 42, "title": {"romaji": "X", "english": "X"}}},
    )
    ctx = MockContext()

    def _q(sql, params=None):
        if "COUNT(*)" in sql:
            return [{"n": 1}]
        return [{
            "user_id": "author",
            "title": "SPOILER: ending reveal",
            "body": "Body without markers.",
            "created_at": None, "updated_at": None,
        }]

    ctx.sql.query = _q
    p.cmd_reviews(ctx, _slash_reviews("42", "default-viewer"))
    follow = ctx.interaction.followups[-1]
    body = json.dumps(follow["embeds"][0])
    assert "||ending reveal||" in body

#!/usr/bin/env python3
"""
validate_plugin.py — pre-flight validator for MMO Maid plugins (SDK v0.5.1).

Usage:
    python validate_plugin.py [plugin_dir]

Default plugin_dir is the current working directory.

Checks (all reported, exit code 0 only if everything passes):

 1. Required files: manifest.json, __main__.py
 2. Manifest:
       - parses as JSON
       - has required fields: id, name, version, description
       - 'id' matches /^[a-z][a-z0-9_]{2,31}$/
       - uses 'capabilities_required' (NOT the legacy 'capabilities_requested')
       - all capabilities are from the canonical v0.5.1 set
       - 'slash_commands' entries have 'name' + 'description'
       - 'proxy_domains_requested' entries are bare hosts (no scheme, no path)
 3. Source code (__main__.py):
       - imports Plugin and Context from mmo_maid_sdk
       - has `plugin = Plugin()` at module level
       - calls `plugin.run()` somewhere (and warns if it's not the last
         non-blank, non-comment line)
       - every `ctx.discord.*` / `ctx.kv.*` / `ctx.sql.*` / `ctx.http.*` call
         has its required capability declared
       - every `ctx.sql.execute(...)` uses a string literal — never an
         f-string or % formatting (the upload reviewer auto-rejects f-strings)
       - every literal https:// URL has its host in 'proxy_domains_requested'
       - every `message_create` handler has an early-return for `author_bot`
 4. Layout:
       - no __pycache__/, .venv/, .git/, .DS_Store at the top level
       - estimated zipped size < 10 MB, uncompressed < 40 MB, <= 200 files

The script doesn't run the plugin (no Discord, no Docker) — it's a static
audit, fast enough to put in a pre-commit hook. Anything that needs runtime
behaviour goes in `tests/` with MockContext.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Iterable

# ── Canonical capability list (v0.5.1) ─────────────────────────────────────
SAFE_CAPS = {
    "storage:kv",
    "discord:send_message",
    "discord:edit_message",
    "discord:add_reaction",
    "discord:read",
    "interaction:respond",
    "proxy:http",
}
RISKY_CAPS = {
    "discord:delete_message",
    "discord:manage_channels",
    "discord:manage_webhooks",
    "storage:sql",
}
DANGEROUS_CAPS = {
    "discord:moderate_members",
    "discord:kick_members",
    "discord:ban_members",
    "discord:manage_roles",
}
ALL_CAPS = SAFE_CAPS | RISKY_CAPS | DANGEROUS_CAPS

# Legacy capability names that the v0.5.1 CLI scaffold still emits but the
# runtime no longer recognises — flag them as ERRORS.
LEGACY_CAPS = {
    "kv:read", "kv:write",
    "events:message_content", "events:member_join", "events:member_leave",
    "events:reaction_add", "events:reaction_remove",
    "discord:respond_to_interaction",
}

# Map ctx.* method calls → required capability.
# Format: dotted call path → capability string.
CAPABILITY_REQUIREMENTS = {
    # Discord
    "ctx.discord.send_message":      "discord:send_message",
    "ctx.discord.edit_message":      "discord:edit_message",
    "ctx.discord.delete_message":    "discord:delete_message",
    "ctx.discord.add_reaction":      "discord:add_reaction",
    "ctx.discord.remove_reaction":   "discord:add_reaction",
    "ctx.discord.fetch_messages":    "discord:read",
    "ctx.discord.fetch_member":      "discord:read",
    "ctx.discord.fetch_channel":    "discord:read",
    "ctx.discord.fetch_role":        "discord:read",
    # v0.5.2+ SDK names (kept alongside the older fetch_* aliases above).
    "ctx.discord.get_messages":      "discord:read",
    "ctx.discord.get_message":       "discord:read",
    "ctx.discord.get_member":        "discord:read",
    "ctx.discord.list_members":      "discord:read",
    "ctx.discord.search_members":    "discord:read",
    "ctx.discord.list_roles":        "discord:read",
    "ctx.discord.get_channel":       "discord:read",
    "ctx.discord.list_channels":     "discord:read",
    "ctx.discord.get_guild":         "discord:read",
    "ctx.discord.create_channel":    "discord:manage_channels",
    "ctx.discord.delete_channel":    "discord:manage_channels",
    "ctx.discord.edit_channel":      "discord:manage_channels",
    "ctx.discord.create_webhook":    "discord:manage_webhooks",
    "ctx.discord.delete_webhook":    "discord:manage_webhooks",
    "ctx.discord.add_role":          "discord:manage_roles",
    "ctx.discord.remove_role":       "discord:manage_roles",
    "ctx.discord.create_role":       "discord:manage_roles",
    "ctx.discord.delete_role":       "discord:manage_roles",
    "ctx.discord.timeout_member":    "discord:moderate_members",
    "ctx.discord.kick_member":       "discord:kick_members",
    "ctx.discord.ban_member":        "discord:ban_members",
    # Storage
    "ctx.kv.get":         "storage:kv",
    "ctx.kv.set":         "storage:kv",
    "ctx.kv.delete":      "storage:kv",
    "ctx.kv.list":        "storage:kv",
    "ctx.kv.increment":   "storage:kv",
    "ctx.sql.execute":    "storage:sql",
    # HTTP
    "ctx.http.get":       "proxy:http",
    "ctx.http.post":      "proxy:http",
    "ctx.http.put":       "proxy:http",
    "ctx.http.patch":     "proxy:http",
    "ctx.http.delete":    "proxy:http",
    "ctx.http.request":   "proxy:http",
}

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
HTTPS_URL_RE = re.compile(r"https://([a-zA-Z0-9_.\-]+)")


# ── Findings ────────────────────────────────────────────────────────────────
class Findings:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.info.append(msg)

    def ok(self) -> bool:
        return not self.errors

    def print_report(self) -> None:
        for m in self.info:
            print(f"  · {m}")
        for m in self.warnings:
            print(f"  ! WARN  {m}")
        for m in self.errors:
            print(f"  X ERROR {m}")
        print()
        if self.errors:
            print(f"FAILED — {len(self.errors)} error(s), {len(self.warnings)} warning(s)")
        elif self.warnings:
            print(f"PASSED with {len(self.warnings)} warning(s) — review before uploading")
        else:
            print("PASSED — ready to zip")


# ── Manifest checks ─────────────────────────────────────────────────────────
def check_manifest(plugin_dir: Path, f: Findings) -> dict:
    p = plugin_dir / "manifest.json"
    if not p.exists():
        f.error("manifest.json is missing")
        return {}
    try:
        manifest = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        f.error(f"manifest.json is not valid JSON: {exc}")
        return {}

    # Field name
    if "capabilities_requested" in manifest and "capabilities_required" not in manifest:
        f.error(
            "manifest uses legacy field 'capabilities_requested'. "
            "Rename to 'capabilities_required' (the runtime-canonical name)."
        )
    elif "capabilities_requested" in manifest:
        f.warn("manifest has both 'capabilities_required' and 'capabilities_requested' — drop the latter.")

    # Required keys
    for k in ("id", "name", "version", "description"):
        if not manifest.get(k):
            f.error(f"manifest.{k} is missing or empty")

    # id format
    pid = manifest.get("id")
    if pid and not PLUGIN_ID_RE.match(pid):
        f.error(
            f"manifest.id {pid!r} fails regex ^[a-z][a-z0-9_]{{2,31}}$ "
            "(3–32 chars, lowercase, starts with a letter)"
        )

    # Capabilities
    caps = manifest.get("capabilities_required") or manifest.get("capabilities_requested") or []
    if not isinstance(caps, list):
        f.error("capabilities_required must be a JSON array of strings")
        caps = []
    for c in caps:
        if c in LEGACY_CAPS:
            f.error(f"capability {c!r} is a legacy/CLI-scaffold name; not recognised by the runtime")
        elif c not in ALL_CAPS:
            f.warn(
                f"capability {c!r} is not in the canonical v0.5.1 list — "
                "could be a typo (see references/manifest-and-capabilities.md)"
            )
    f.note(f"declared capabilities ({len(caps)}): {', '.join(sorted(caps)) or '(none)'}")

    # Tier
    tier = "Safe"
    if any(c in DANGEROUS_CAPS for c in caps):
        tier = "Dangerous"
    elif any(c in RISKY_CAPS for c in caps):
        tier = "Risky"
    f.note(f"plugin tier: {tier}")

    # slash_commands
    cmds = manifest.get("slash_commands") or []
    if cmds and "interaction:respond" not in caps:
        # auto-added at runtime, but better to make it explicit
        f.warn(
            "slash_commands declared but 'interaction:respond' not in capabilities_required. "
            "The runtime auto-adds it; declaring explicitly avoids surprises in reviews."
        )
    for i, c in enumerate(cmds):
        if not isinstance(c, dict):
            f.error(f"slash_commands[{i}] must be an object")
            continue
        if not c.get("name"):
            f.error(f"slash_commands[{i}] missing 'name'")
        if not c.get("description"):
            f.error(f"slash_commands[{i}] missing 'description'")

    # proxy_domains_requested
    domains = manifest.get("proxy_domains_requested") or []
    if domains and "proxy:http" not in caps:
        f.warn("proxy_domains_requested declared but 'proxy:http' not in capabilities_required.")
    for d in domains:
        if "://" in d or "/" in d:
            f.error(f"proxy_domains_requested entry {d!r} must be a bare host (no scheme, no path)")

    return manifest


# ── Source checks ───────────────────────────────────────────────────────────
def _walk_attribute_chain(node: ast.AST) -> str | None:
    """Turn an AST attribute chain into a dotted string (e.g. ctx.discord.send_message)."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    else:
        return None
    return ".".join(reversed(parts))


def check_source(plugin_dir: Path, manifest: dict, f: Findings) -> None:
    p = plugin_dir / "__main__.py"
    if not p.exists():
        f.error("__main__.py is missing")
        return
    src = p.read_text(encoding="utf-8")

    # plugin.run() last?
    lines = [ln for ln in src.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        f.error("__main__.py is empty")
        return
    if "plugin.run()" not in src:
        f.error("__main__.py never calls plugin.run() — the process will exit immediately")
    else:
        last = lines[-1].strip()
        # Accept either bare or guarded forms
        if last not in {"plugin.run()", "    plugin.run()"} and "plugin.run()" not in last:
            f.warn(
                "plugin.run() should be the last executable line of __main__.py "
                f"(found {last!r}). Code after it will not run until shutdown."
            )

    # AST analysis
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        f.error(f"__main__.py has a syntax error: {exc}")
        return

    # Imports
    imports_plugin = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "mmo_maid_sdk":
            for alias in node.names:
                if alias.name == "Plugin":
                    imports_plugin = True
    if not imports_plugin:
        f.error("__main__.py does not import Plugin from mmo_maid_sdk")

    # plugin = Plugin() at module level
    has_module_plugin = False
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id == "Plugin":
                if any(isinstance(t, ast.Name) and t.id == "plugin" for t in node.targets):
                    has_module_plugin = True
    if not has_module_plugin:
        f.warn("could not find `plugin = Plugin()` at module level — decorator wiring requires it")

    declared_caps = set(
        manifest.get("capabilities_required") or manifest.get("capabilities_requested") or []
    )
    declared_domains = set(manifest.get("proxy_domains_requested") or [])
    needed_caps: dict[str, set[str]] = {}  # cap → set of call paths
    sql_lineos_with_fstrings: list[int] = []
    sql_lineos_with_percent: list[int] = []
    http_call_urls: list[tuple[int, str]] = []  # (lineno, host) for real ctx.http.* calls
    has_message_create_handler = False
    message_create_has_guard = False
    message_create_lineno: int | None = None

    # First pass: scan for ctx.* calls and SQL string interpolation
    for node in ast.walk(tree):
        # Function decorated with @plugin.on_event("message_create") → check guard
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if (isinstance(deco, ast.Call)
                        and isinstance(deco.func, ast.Attribute)
                        and deco.func.attr == "on_event"
                        and deco.args
                        and isinstance(deco.args[0], ast.Constant)
                        and deco.args[0].value == "message_create"):
                    has_message_create_handler = True
                    message_create_lineno = node.lineno
                    # Check the very first statement for an author_bot guard
                    body = node.body
                    if body:
                        first = body[0]
                        # Pattern: if event.get("author_bot"): return
                        # We accept anything containing 'author_bot' in the first 5 statements
                        for stmt in body[:5]:
                            try:
                                stmt_src = ast.unparse(stmt) if hasattr(ast, "unparse") else ""
                            except Exception:
                                stmt_src = ""
                            if "author_bot" in stmt_src or "is_bot" in stmt_src:
                                message_create_has_guard = True
                                break

        # ctx.* call → check capabilities
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            chain = _walk_attribute_chain(node.func)
            if chain and chain.startswith("ctx."):
                cap = CAPABILITY_REQUIREMENTS.get(chain)
                if cap:
                    needed_caps.setdefault(cap, set()).add(chain)
                # SQL string interpolation
                if chain == "ctx.sql.execute" and node.args:
                    sql_arg = node.args[0]
                    if isinstance(sql_arg, ast.JoinedStr):
                        sql_lineos_with_fstrings.append(node.lineno)
                    elif isinstance(sql_arg, ast.BinOp) and isinstance(sql_arg.op, ast.Mod):
                        sql_lineos_with_percent.append(node.lineno)
                    elif (isinstance(sql_arg, ast.Call)
                          and isinstance(sql_arg.func, ast.Attribute)
                          and sql_arg.func.attr == "format"):
                        sql_lineos_with_percent.append(node.lineno)
                # HTTP URL extraction — only from real ctx.http.* call args
                if chain.startswith("ctx.http.") and node.args:
                    url_arg = node.args[0]
                    url_text: str | None = None
                    if isinstance(url_arg, ast.Constant) and isinstance(url_arg.value, str):
                        url_text = url_arg.value
                    elif isinstance(url_arg, ast.JoinedStr):
                        # f"https://api.x.com/{word}" — pull literal prefix
                        for part in url_arg.values:
                            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                                url_text = part.value
                                break
                    if url_text:
                        m = HTTPS_URL_RE.match(url_text)
                        if m:
                            http_call_urls.append((node.lineno, m.group(1)))

    # Bot guard
    if has_message_create_handler and not message_create_has_guard:
        f.warn(
            f"message_create handler at line {message_create_lineno} has no obvious "
            "`if event.get('author_bot'): return` guard — risk of bot-reply loops"
        )

    # SQL safety
    for ln in sql_lineos_with_fstrings:
        f.error(
            f"ctx.sql.execute at line {ln} uses an f-string — upload reviewer auto-rejects. "
            "Use $1, $2, … placeholders with a separate params list instead."
        )
    for ln in sql_lineos_with_percent:
        f.error(
            f"ctx.sql.execute at line {ln} uses %-formatting or .format() — same SQL-injection risk. "
            "Use $1, $2, … placeholders instead."
        )

    # Capability coverage
    for cap, calls in needed_caps.items():
        if cap not in declared_caps:
            sample = ", ".join(sorted(calls)[:3])
            f.error(
                f"call(s) {sample} require capability {cap!r} "
                "but it's not in manifest.capabilities_required"
            )

    # Reverse — caps declared but never used
    used = set(needed_caps.keys())
    # interaction:respond and proxy:http are auto-added; allow them even if not in needed_caps
    AUTO_OR_RUNTIME = {"interaction:respond", "proxy:http"}
    extra = declared_caps - used - AUTO_OR_RUNTIME - LEGACY_CAPS
    # Subtract caps that may be satisfied by less granular helpers we don't statically detect
    for cap in sorted(extra):
        f.warn(f"capability {cap!r} declared but no matching ctx.* call was detected — drop if unused")

    # HTTPS hosts vs proxy_domains_requested — only from real ctx.http.* call args
    for lineno, host in http_call_urls:
        if host not in declared_domains:
            f.error(
                f"ctx.http.* at line {lineno} targets https://{host}/... but {host!r} is not in "
                "manifest.proxy_domains_requested — the runtime will reject the request"
            )


# ── Layout / size checks ────────────────────────────────────────────────────
# Top-level entries that should never appear *in a release context*. .git is
# intentionally NOT in this set — it's standard repo metadata that
# scripts/build_release.py already excludes from the upload zip via its runtime
# allowlist, and flagging it when validate runs against the repo root (as CI
# does) produces a false-positive failure on every git-tracked plugin.
DISALLOWED_TOPLEVEL = {"__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".DS_Store"}

# Mirror scripts/build_release.py — the runtime allowlist. These are the files
# that actually go into the upload zip. Anything else in the repo (tests, CI,
# README, LICENSE, .gitignore, .github/) is meta and stripped at release time.
RUNTIME_REQUIRED = {"manifest.json", "__main__.py"}
RUNTIME_OPTIONAL_FILES = {"requirements.txt", "dashboard_manifest.json"}
RUNTIME_OPTIONAL_DIRS = {"dashboard"}


def _is_runtime(rel: Path) -> bool:
    """Match the path against the runtime allowlist used by build_release.py."""
    parts = rel.parts
    if not parts:
        return False
    first = parts[0]
    if len(parts) == 1:
        return first in RUNTIME_REQUIRED or first in RUNTIME_OPTIONAL_FILES
    # Nested file — only allowed under one of the runtime dirs.
    return first in RUNTIME_OPTIONAL_DIRS


def check_layout(plugin_dir: Path, f: Findings) -> None:
    # Disallowed top-level entries (build cruft, .git, etc.)
    for name in os.listdir(plugin_dir):
        if name in DISALLOWED_TOPLEVEL:
            f.error(f"disallowed top-level entry: {name}/ (strip before zipping)")

    # Build the runtime upload zip in memory for an accurate size estimate.
    # Meta files (README, LICENSE, tests/, .github/, .gitignore, Makefile, etc.)
    # are intentionally excluded — they don't ship.
    buf = BytesIO()
    runtime_files = 0
    runtime_uncompressed = 0
    meta_files = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(plugin_dir):
            # Don't even descend into disallowed dirs
            dirs[:] = [d for d in dirs if d not in DISALLOWED_TOPLEVEL]
            for name in files:
                if name.endswith(".pyc") or name == ".DS_Store":
                    continue
                fp = Path(root) / name
                rel = fp.relative_to(plugin_dir)
                if not _is_runtime(rel):
                    meta_files += 1
                    continue
                runtime_files += 1
                try:
                    sz = fp.stat().st_size
                except OSError:
                    sz = 0
                runtime_uncompressed += sz
                zf.write(fp, str(rel))
    zipped = len(buf.getvalue())

    f.note(
        f"runtime files (ship): {runtime_files}, "
        f"uncompressed: {runtime_uncompressed/1024:.1f} KB, "
        f"estimated zipped: {zipped/1024:.1f} KB"
    )
    if meta_files:
        f.note(f"repo meta files (do not ship): {meta_files} — stripped by scripts/build_release.py")
    if runtime_files == 0:
        f.error("no runtime files found — manifest.json and __main__.py are required")
    if runtime_files > 200:
        f.error(f"too many runtime files: {runtime_files} (limit 200)")
    if runtime_uncompressed > 40 * 1024 * 1024:
        f.error(f"runtime uncompressed size > 40 MB ({runtime_uncompressed/1024/1024:.1f} MB)")
    if zipped > 10 * 1024 * 1024:
        f.error(f"runtime zipped size > 10 MB ({zipped/1024/1024:.1f} MB)")


# ── Entry point ─────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    plugin_dir = Path(argv[1] if len(argv) > 1 else ".").resolve()
    if not plugin_dir.is_dir():
        print(f"error: {plugin_dir} is not a directory", file=sys.stderr)
        return 2

    print(f"validating plugin at {plugin_dir}")
    print()
    f = Findings()

    manifest = check_manifest(plugin_dir, f)
    check_source(plugin_dir, manifest, f)
    check_layout(plugin_dir, f)
    f.print_report()
    return 0 if f.ok() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

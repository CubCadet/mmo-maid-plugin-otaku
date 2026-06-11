#!/usr/bin/env python3
"""
build_release.py — produce the YourBot upload zip from this repo.

Why this exists: the GitHub repo contains lots of meta files (tests, .github,
README, LICENSE, .gitignore, the SDK wheel, …) that must NOT end up in the
plugin's upload zip. The platform only accepts the runtime files, and shipping
.git/ or tests/ blows past the 10 MB / 200-file limits anyway.

A naive `zip -r plugin.zip .` from the repo root will include all the meta
files. This script uses an explicit ALLOWLIST instead — safer, because new
files that don't match the allowlist won't accidentally end up in the upload.

Usage:
    python scripts/build_release.py              # writes ./<plugin_id>-<version>.zip
    python scripts/build_release.py -o dist/     # writes dist/<plugin_id>-<version>.zip
    python scripts/build_release.py --dry-run    # list what would be included

Exit codes:
    0  success
    1  manifest missing or invalid
    2  validation failed (run scripts/validate_plugin.py for details)
    3  output path problem
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import zipfile
from pathlib import Path

# ── Runtime allowlist ──────────────────────────────────────────────────────
# Exact paths (relative to repo root). Order matters only for the printout.
REQUIRED_FILES = [
    "manifest.json",
    "__main__.py",
]
OPTIONAL_FILES = [
    "requirements.txt",
    "dashboard_manifest.json",
]
# Optional directory trees — everything inside is included.
OPTIONAL_DIRS = [
    "dashboard",
]
# Within optional dirs, exclude these patterns (build artifacts).
EXCLUDE_PATTERNS = [
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
]

# Platform limits (v0.5.1).
MAX_FILES = 200
MAX_ZIPPED_BYTES = 10 * 1024 * 1024     # 10 MB
MAX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024  # 40 MB


def _excluded(path: Path) -> bool:
    """Match the path's name or any segment against EXCLUDE_PATTERNS."""
    parts = path.parts
    for pat in EXCLUDE_PATTERNS:
        if fnmatch.fnmatch(path.name, pat):
            return True
        if any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


def collect_files(repo: Path) -> list[Path]:
    """Walk the runtime allowlist and return every file to include (relative)."""
    included: list[Path] = []

    for name in REQUIRED_FILES:
        p = repo / name
        if not p.exists():
            raise FileNotFoundError(f"required file missing: {name}")
        included.append(Path(name))

    for name in OPTIONAL_FILES:
        p = repo / name
        if p.exists():
            included.append(Path(name))

    for d in OPTIONAL_DIRS:
        root = repo / d
        if not root.is_dir():
            continue
        for fp in sorted(root.rglob("*")):
            if fp.is_dir():
                continue
            rel = fp.relative_to(repo)
            if _excluded(rel):
                continue
            included.append(rel)

    return included


def load_manifest(repo: Path) -> dict:
    p = repo / "manifest.json"
    if not p.exists():
        print("error: manifest.json missing — are you running from the repo root?", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: manifest.json invalid JSON: {exc}", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Build the YourBot plugin upload zip.")
    ap.add_argument("-o", "--output", default=".",
                    help="output directory (default: cwd). Created if missing.")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be included, but write nothing")
    ap.add_argument("--repo", default=".",
                    help="repo root (default: cwd)")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    manifest = load_manifest(repo)
    plugin_id = manifest.get("id") or "plugin"
    version = manifest.get("version") or "0.0.0"
    zip_name = f"{plugin_id}-{version}.zip"

    try:
        files = collect_files(repo)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"Would build {zip_name} containing:")
        total = 0
        for f in files:
            sz = (repo / f).stat().st_size
            total += sz
            print(f"  {sz:>8}  {f}")
        print(f"\n{len(files)} files, {total/1024:.1f} KB uncompressed")
        return 0

    if len(files) > MAX_FILES:
        print(f"error: too many files: {len(files)} (limit {MAX_FILES})", file=sys.stderr)
        return 2

    out_dir = Path(args.output).resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: cannot create output dir {out_dir}: {exc}", file=sys.stderr)
        return 3
    out_path = out_dir / zip_name

    total_uncompressed = 0
    print(f"Building {out_path}:")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            src = repo / f
            sz = src.stat().st_size
            total_uncompressed += sz
            zf.write(src, str(f))
            print(f"  + {f}  ({sz:,} bytes)")

    zipped = out_path.stat().st_size
    print(f"\n{len(files)} files")
    print(f"  uncompressed:  {total_uncompressed/1024:.1f} KB  (limit {MAX_UNCOMPRESSED_BYTES/1024/1024:.0f} MB)")
    print(f"  zipped:        {zipped/1024:.1f} KB  (limit {MAX_ZIPPED_BYTES/1024/1024:.0f} MB)")
    print(f"\nwrote {out_path}")

    if zipped > MAX_ZIPPED_BYTES:
        print(f"\nERROR: zip exceeds {MAX_ZIPPED_BYTES/1024/1024:.0f} MB limit", file=sys.stderr)
        return 2
    if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
        print(f"\nERROR: uncompressed size exceeds {MAX_UNCOMPRESSED_BYTES/1024/1024:.0f} MB limit",
              file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

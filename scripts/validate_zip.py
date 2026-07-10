#!/usr/bin/env python3
"""
validate_zip.py — run the SDK's platform publish-gate validator against a
built release zip.

Usage:
    python scripts/validate_zip.py dist/otaku-11.0.0.zip
    python scripts/validate_zip.py dist/          # newest *.zip in the dir

This is the artifact-parity layer: yourbot_sdk._validation.validate_artifact
is vendored from the platform's artifact_store checks ("what `yourbot
validate` checks is what the platform enforces"), and the platform scans the
UPLOADED ZIP — not the working tree. scripts/validate_plugin.py stays the
repo-hygiene layer (SQL interpolation, proxy-domain coverage, layout/size);
`yourbot validate --path .` covers the tree during dev. This script gates the
exact bytes we attach to a release, so a zip the platform would refuse at
artifact_store_put fails CI first.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path


def _resolve_zip(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        zips = sorted(p.glob("*.zip"), key=lambda z: z.stat().st_mtime)
        if not zips:
            sys.exit(f"validate_zip: no *.zip in {p}/")
        return zips[-1]
    if not p.is_file():
        sys.exit(f"validate_zip: {p} not found")
    return p


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[4].strip())

    try:
        from yourbot_sdk._validation import validate_artifact
    except ImportError as e:  # pragma: no cover — pin guarantees >=0.8.3
        sys.exit(f"validate_zip: yourbot-sdk >=0.8.3 required ({e})")

    zip_path = _resolve_zip(sys.argv[1])
    artifact = zip_path.read_bytes()

    # id/version come from the manifest INSIDE the zip — the same values the
    # platform reads at upload — so a stale dist/ zip can't pass on the
    # working tree's manifest. On a malformed zip/manifest, fall back to
    # placeholder id/version so validate_artifact renders its own
    # artifact_unreadable / manifest-missing findings instead of a traceback.
    try:
        with zipfile.ZipFile(io.BytesIO(artifact)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        plugin_id = manifest["id"]
        version = manifest["version"]
    except Exception as e:  # noqa: BLE001 — BadZipFile/KeyError/JSONDecodeError
        print(f"validate_zip: {zip_path.name}: could not read manifest ({e}); "
              "running the gate with placeholder id/version")
        plugin_id, version = "unknown", "0.0.0"

    result = validate_artifact(
        plugin_id=plugin_id,
        version=version,
        artifact_bytes=artifact,
    )

    for f in result.errors + result.warnings:
        print(f"  [{f.severity}] {f.code}: {f.message}")
        if f.hint:
            print(f"      hint: {f.hint}")

    n_err = len(result.errors)
    n_warn = len(result.warnings)
    if result.has_errors:
        print(f"validate_zip: FAILED — {n_err} error(s), {n_warn} warning(s) in {zip_path.name}")
        return 1
    print(f"validate_zip: OK — {zip_path.name} passes the platform publish gate ({n_warn} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())

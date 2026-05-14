# My Plugin

> A short, one-sentence description of what this plugin does.

A plugin for [MMO Maid](https://mmomaid.com) — runs sandboxed in the platform and reacts to Discord events on installed servers.

## What it does

Describe the user-facing behaviour in 2–3 paragraphs. Cover the main slash commands, automatic behaviours (welcomes, scheduled posts, etc.), and any per-server configuration.

## Capabilities

This plugin requests the following capabilities. Each is listed in the manifest with a one-line rationale so server admins know *why* it's needed before they install:

| Capability | Tier | Why |
|---|---|---|
| `discord:send_message` | Safe | Reply to commands and post automatic messages. |

If/when new capabilities are added, update this table *and* `CHANGELOG.md`.

## Slash commands

| Command | Description |
|---|---|
| `/example` | What this command does and any options it takes. |

## Quick start (development)

```bash
# 1. Clone & install
git clone https://github.com/your-org/my-plugin.git
cd my-plugin
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 2. Local dev loop (hot-reload + mock host)
mmo dev --watch

# 3. Tests
python -m pytest -q

# 4. Pre-flight validation (also runs in CI)
python scripts/validate_plugin.py .
```

`mmo dev` fires events from `events.yaml` against a `MockContext`, prints every action the plugin takes, and reloads on file change. See [SDK docs](https://mmomaid.com/dev/docs) for the full developer workflow.

## Release process

Releases are tagged on `main` with semver tags (`v1.2.3`), which triggers `.github/workflows/release.yml` to validate, test, build the upload zip, and attach it to the GitHub release.

```bash
# 1. Bump manifest.json "version" and update CHANGELOG.md
# 2. Verify locally
make release          # validates, tests, builds dist/<plugin_id>-<version>.zip

# 3. Commit, tag, push
git commit -am "Release v1.2.3"
git tag v1.2.3
git push && git push --tags
```

The tag's version (`v1.2.3` → `1.2.3`) must match `manifest.json`'s `version` field; CI rejects the release otherwise.

## Submitting for review

The MMO Maid dev portal links to this repo and pulls a specific tag for review. Review turnaround is typically 1–3 business days. The reviewer checks the manifest, scans for disallowed imports and unparameterised SQL, and re-prompts users on any tier shift.

## License

MIT — see [`LICENSE`](LICENSE).

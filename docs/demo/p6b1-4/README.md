# Read-only TUI commands — #64

Captured on 2026-10-22 UTC from code commit
`35626fca9dcc9bd7da31f3256010569df14b95f3` using the local mock stack.
Implementation is proposed for review, not yet merged. The release stays v0.4.2.

## Reproduce

Install Playwright outside this repository if it is not already available; no
application dependency was added. Point `PLAYWRIGHT_MODULE` at its `index.mjs`
and `CHROMIUM_PATH` at an existing Chromium executable when necessary.

```sh
scripts/tui_dev.sh up
node scripts/tui_replay_check.mjs run:7e57d0c0ffee
SHOT_DIR=docs/demo/p6b1-4 node scripts/tui_commands_check.mjs
LC_ALL=C shasum -a 256 docs/demo/p6b1-4/*.png
```

The actual capture used `PLAYWRIGHT_MODULE=/tmp/mailroom-browser-proof/node_modules/playwright/index.mjs`
and `CHROMIUM_PATH=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
Run without an API token against the seeded, no-token mock harness. The command
script refuses non-loopback targets, blocks external browser requests and checks
secret presence markers before capture. API/Node tests cover configured-token
authentication and seeded-secret non-disclosure separately.

## Executed gates

| Command | Result | Raw output |
| --- | --- | --- |
| `.venv/bin/ruff check src tests` | All checks passed! | [ruff.log](ruff.log) |
| `PYTHONPATH=src .venv/bin/pytest -p no:cacheprovider tests -q` | 1842 passed, 3 skipped, 3 deselected, 136 warnings | [pytest.log](pytest.log) |
| `PYTHONPATH=src .venv/bin/pytest -p no:cacheprovider tests/api/test_config_route.py -q` | 17 passed, 1 warning | [config-api.log](config-api.log) |
| `node --test tests/tui/js/*.test.mjs` | 378 passed, 0 failed | [tui-node.log](tui-node.log) |
| `node --test tests/ui/js/*.test.mjs` | 12 passed, 0 failed | [ui-node.log](ui-node.log) |
| `node --test tests/tui/js/{links-command,ready-command,config-command,replay-export}.test.mjs` | 78 passed, 0 failed | [targeted-node.log](targeted-node.log) |
| `node scripts/tui_replay_check.mjs run:7e57d0c0ffee` | all checks passed | [replay-browser.log](replay-browser.log) |
| `SHOT_DIR=docs/demo/p6b1-4 node scripts/tui_commands_check.mjs` | all command checks passed | [commands-browser.log](commands-browser.log) |

The preserved pre-change baseline was 300 TUI + 12 UI Node passes. The final TUI
suite has 78 additional tests. The new 2050 ms readiness-response regression was
red against the two-second client deadline and green with a three-second
readiness-specific deadline; cancellation tests remain green. The generic HTTP
default remains two seconds.

The export test compares browser-downloaded bytes with the actual API export.
`test_replay_export_matches_cli_bytes_and_excludes_document_content` additionally
compares CLI output: it equals API bytes plus the CLI's existing trailing newline,
and seeded private document/completion text does not enter the export.

## Screenshots and scope

[manifest.json](manifest.json) pins the source commit, capture timestamp,
commands, dimensions, byte sizes and SHA-256 hashes for `links.png`, `ready.png`,
`config.png` and `replay-export.png`. Each is below 400 KiB. The export screenshot
shows successful completion; the manifest records the downloaded payload hash.

The existing `docs/demo/` tooling and gallery are reused, with a separate
`p6b1-4/` manifest so the release gallery is unchanged. This is the scoped
screenshot-location resolution recorded on #64 and in the archived plans.

Independent review found and reproduced the readiness deadline issue; it is
fixed and covered. No control endpoints were introduced. Public `/links` and
aggregate `/ready` semantics are unchanged; detailed config remains token-gated.

Not verified: live providers, GPUs, Docker, external collectors, real Gmail,
production deployments, external anchors, release packaging and merged-commit
closure evidence. Mock browser success is not live-stack proof for #69/#73.
The initial T3 preview snapshot timed out twice; the documented repository
Playwright workflow supplied the reproducible browser evidence instead.

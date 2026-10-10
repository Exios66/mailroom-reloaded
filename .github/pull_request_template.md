## Summary

<!-- One or two sentences: what this PR does. -->

## Type of change

<!-- Tick all that apply. -->

- [ ] Bug fix
- [ ] Feature / enhancement
- [ ] Refactor / chore
- [ ] Docs / plan
- [ ] Tests only
- [ ] CI / repo governance

## Why

<!-- Motivation, linked issues ("Closes #12"), master plan item ids (for example R-13). -->

## Changes

<!-- Bullet the files or areas changed. Note the stack parent if this PR is stacked. -->

## Validation

<!-- Tick what you actually ran; note every skip under "Not verified". -->

- [ ] `ruff check src tests`
- [ ] `PYTHONPATH=src pytest -p no:cacheprovider tests -q --ignore=tests/sandbox`
- [ ] `pytest tests/sandbox -q` (sandbox changes)
- [ ] `node --test tests/tui/js/*.test.mjs`
- [ ] `scripts/tui_replay_check.mjs` browser check (TUI changes)

## Safety

- [ ] No secrets, tokens or real personal data in code, fixtures or logs
- [ ] `CHANGELOG.md` `[Unreleased]` entry added
- [ ] Docs and master plan updated where behaviour changed

## Not verified / follow-ups

<!-- Be explicit about every gate skipped and every claim not checked. -->

<!-- Fill this block; agents must keep the exact shape. Use null or [] when empty. -->
```yaml
agent-report:
  schema: 1
  change_type: fix|feature|refactor|docs|tests|governance
  scopes: []            # paths or areas, e.g. src/mailroom_reloaded/api/tui/replay
  linked_issues: []     # "#12" style
  plan_items: []        # master plan ids, e.g. R-13
  stack_parent: null    # PR number this is stacked on, or null
  gates:
    ruff: pass|fail|skipped
    pytest: pass|fail|skipped
    pytest_sandbox: pass|fail|skipped
    node_test: pass|fail|skipped
    browser_check: pass|fail|skipped
  coderabbit: addressed|pending|none
  not_verified: []
  follow_ups: []
```

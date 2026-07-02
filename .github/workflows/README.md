# GitHub Actions — workflow conventions

> **All sweep/backtest workflows were removed 2026-07-03.** They were one-off
> research sweeps (and several referenced now-pruned strategies), so they were
> deleted to clean up the Actions tab. **Recover any from git history** if you need
> to re-run one (`git log --all --diff-filter=D -- .github/workflows/`, then
> `git checkout <sha>^ -- .github/workflows/<file>.yml`). The sweeps themselves are
> just `tools/sweep_*.py` runs — you can also run them locally without a workflow.
> The action-version conventions below still apply to any workflow you re-add.

When you **create or edit** any workflow in this folder, always pin the **latest
Node-24-native major versions** of the standard actions. GitHub forces Node 20
actions to Node 24 from **2026-06-16** and removes Node 20 from runners on
**2026-09-16**, so anything older triggers a deprecation warning and will
eventually fail. Don't ship a workflow on a deprecated runtime.

## Use these versions (latest, Node 24)

| Action | Pin | Avoid |
|---|---|---|
| `actions/checkout` | `@v5` | `@v4`, `@v3` |
| `actions/setup-python` | `@v6` | `@v5`, `@v4` |
| `actions/upload-artifact` | `@v4` (current major) | `@v3` |
| `actions/download-artifact` | `@v4` (current major) | `@v3` |

> **Always check the action's GitHub Releases page for a newer major before
> pinning** — the table above is current as of 2026-06. Take the newest major
> that supports Node 24; do not copy an old `@v4`/`@v5` out of an existing file
> without checking.

## If an action you need only has a Node-20 major
Some actions (e.g. the artifact actions while still on `@v4`) don't yet have a
Node-24 major. Force them to Node 24 with a top-level env var rather than
running on deprecated Node 20:

```yaml
env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"
```

This forces **all** JavaScript actions in the workflow onto Node 24 regardless
of their pinned version — a safe catch-all.

## Checklist for a new / edited workflow
- [ ] `actions/checkout@v5`, `actions/setup-python@v6` (or newer majors)
- [ ] Any action still on a Node-20-only major is covered by
      `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"`
- [ ] `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/<file>.yml'))"` parses
- [ ] First run's logs show **no** Node-20 deprecation warnings

_Reference: <https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/>_

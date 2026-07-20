# Worktree Runtime Efficiency

Hermes-managed linked worktrees reuse dependencies only when the primary
worktree has the same lockfile content. The reuse target is always the durable
primary checkout, never another disposable worker.

## Dependency reuse

Discord action worktrees and coding workers prepare dependencies automatically.
For native or manually-created worktrees, run:

```bash
hermes worktrees prepare /absolute/path/to/worktree
```

This may link:

- `.venv` when the Python lock signature matches the primary worktree;
- pnpm `node_modules` when the package-relative lockfile matches.

The links are shared runtime state. Unlink them before running a dependency
install/sync or changing dependency manifests; a changed lock signature will
not be reused in newly-created worktrees.

Disable either behavior in `config.yaml`:

```yaml
worktrees:
  dependency_reuse:
    python_venv: false
    pnpm: false
```

`HERMES_WORKTREE_PYTHON_VENV_LINKS` and
`HERMES_CODING_WORKER_PNPM_LINKS` remain explicit environment overrides.

## Cleanup

Inspect eligible worktrees first:

```bash
hermes worktrees cleanup --root /home/droid/workspaces --older-than-days 7
```

Apply the same bounded selection with `--apply`. Cleanup skips primary,
locked, active, recent, dirty, remote-less, and unpushed worktrees. It removes
the worktree only; local branches are preserved.

To reclaim duplicated Python environments without deleting worktrees:

```bash
hermes worktrees dedupe --root /path/to/worktree-parent
hermes worktrees dedupe --root /path/to/worktree-parent --apply
```

The gateway runs a bounded daily cleanup pass for action worktrees whose ledger
users have all been terminal longer than `worktrees.cleanup.retention_days`.
Dirty, active, locked, and unpushed worktrees still remain untouched.

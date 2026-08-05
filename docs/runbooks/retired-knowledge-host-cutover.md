# Retired Knowledge Host Cutover

This is a **post-merge operator runbook** for retiring the legacy desktop-note
integration from a host. Do not run it from a feature branch, worktree, CI job,
or active development session. The code change does not mutate host state.

## Preconditions and stop conditions

Proceed only after the removal PR is merged and the canonical checkout is at
the verified merge SHA. Record that SHA, the operator, UTC start time, active
Hermes profiles, gateway unit names, vault path, QMD collection name, and
archive root in the change ticket.

Stop immediately if any path or service name is uncertain, the vault contains
symlinks, the archive is on the same path as the vault, the Honcho config has
unexpected hosts, QMD reports another collection backed by the same vault, or
the gateway cannot be cleanly stopped and restarted.

## 1. Audit only

Do not print Honcho secrets. Inspect only keys and non-secret routing fields:

```bash
git -C /path/to/canonical-hermes status --short
git -C /path/to/canonical-hermes rev-parse HEAD
hermes gateway status
qmd collection list
python scripts/archive_legacy_vault.py \
  --source /absolute/path/to/legacy-vault \
  --archive-root /absolute/path/to/operator-archives
```

Inspect each active profile's `$HERMES_HOME/honcho.json` and confirm that its
resolved `hosts` map contains the intended `hermes` or `hermes_<profile>` key.
An empty object is valid membership and inherits root fields. Unknown profile
hosts must remain disabled. Back up every edited config with restrictive
permissions before changing it.

## 2. Disable legacy QMD ingestion

Use the installed QMD version's documented collection-removal command to remove
only the collection whose configured source path exactly equals the legacy
vault. Do not delete the QMD database or unrelated collections. Then run:

```bash
qmd collection list
qmd status
```

Stop if the retired collection remains, another collection points at the same
path, or QMD reports an unhealthy index. Preserve the before/after command
output in the ticket.

## 3. Archive the vault

Review the dry-run JSON from step 1, including source, destination, file count,
and byte count. Then execute exactly once:

```bash
python scripts/archive_legacy_vault.py \
  --source /absolute/path/to/legacy-vault \
  --archive-root /absolute/path/to/operator-archives \
  --execute
```

The helper atomically renames the source to a same-filesystem staging path,
copies it to a timestamped destination, verifies every file's size and SHA-256
digest, writes `.hermes-archive-manifest.json`, and only then removes staging.
It restores the original path if copying or verification fails. Stop if it
exits non-zero. Never substitute `rm -rf` or an unverified move.

## 4. Confirm Honcho host boundaries

For every active profile, confirm the appropriate host key is present under
`hosts`. Keep common connectivity and defaults at the root only when intended;
host fields override root fields. Preserve explicit `enabled: false` blocks.
Legacy `hermes.<profile>` aliases remain supported but should not be newly
introduced.

Run the profile's normal Honcho status/health command and verify:

- configured empty blocks inherit the intended root workspace/connectivity;
- explicitly disabled hosts remain disabled;
- an unlisted synthetic profile host is rejected;
- no retired-vault or QMD path appears in the effective configuration.

## 5. Restart and smoke-test the gateway

Restart only now, after confirming no development worker is active:

```bash
hermes gateway restart
hermes gateway status
```

For profile-specific services, run the same commands with the profile selector
used on that host. Verify the service reaches healthy/active state, Discord or
other configured messaging transport can receive and answer a test message,
Honcho recall succeeds for an allowed host, and logs show no retired integration
or vault path. Record exact unit names and timestamps.

## 6. Rollback

If the smoke test fails, stop the gateway, restore the backed-up Honcho config,
restore the vault from the verified archive to its original path, recreate only
the previously recorded QMD collection, and restart. Do not roll back by
checking out the feature branch or by deleting the archive. Capture failure
logs and leave the host in its last known-good state.

## Acceptance evidence

The cutover is complete only when the ticket contains the merge SHA, dry-run and
execute summaries, archive manifest path, QMD before/after status, redacted
Honcho host membership audit, gateway status, transport smoke result, Honcho
recall result, and explicit confirmation that rollback was not required.

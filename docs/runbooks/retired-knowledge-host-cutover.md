# Retired Knowledge Host Cutover

This is the **post-merge** operator runbook for the executable cutover in
`scripts/archive_legacy_vault.py`. The helper is dry-run by default and never
prints secrets, memory content, message content, or vault filenames. Do not run
`apply` from this PR branch. Independent review, merge, and canonical checkout
fast-forward are mandatory first.

## Safety model

The helper has four modes:

| Mode | Mutation | Purpose |
| --- | --- | --- |
| `preflight` | none | Inventory every selectable Honcho host/scope, planned skill removals, service gates, and the vault tree hash. |
| `apply` | yes | Execute the all-host cutover transaction. Requires the exact confirmation string and merged SHA. |
| `verify` | receipt only | Re-verify services, archive receipts/hashes/metadata, source absence, and controlled non-recreation. |
| `restore` | yes | Restore only receipt-owned changes; stop on concurrent drift rather than overwrite. |

The durable receipt root defaults to:

```text
$HERMES_HOME/retired-note-cutover-receipts/
```

It is created with mode `0700`; receipt and backup files use `0600`.

## STOP gates

Stop without improvising when the helper reports `state: STOP`. Important gates
include:

- canonical checkout dirty or missing the reviewed merge SHA;
- active gateway turns or development/kanban workers;
- malformed or unsafe Honcho/config/registry paths;
- incomplete selectable-host or observer/target receipts;
- archive destination on another device;
- vault source absent, already a symlink, changed since preflight, or recreated;
- unsupported vault filesystem objects;
- legacy Gmail collector/renewal service still active;
- gateway not healthy after its one controlled restart;
- QMD skills refresh failure after targeted fallback;
- archive metadata/hash/fsync failure;
- restore-time concurrent drift.

Never substitute `rm -rf`, cross-device copy/delete, an unverified tarball, or a
manual config overwrite.

## 1. Independent review and merge

Before touching the host:

1. Obtain independent review of the removal PR and this helper.
2. Merge the PR normally.
3. Fast-forward the clean canonical checkout to the verified merge SHA.
4. Record the merge SHA in the change ticket.
5. Confirm no development worker, active agent turn, or gateway migration is in
   flight.

## 2. Preflight (safe to run repeatedly)

```bash
python scripts/archive_legacy_vault.py preflight
```

Review the structural JSON summary. It reports counts and hashes, not file
content or names. Confirm:

- every enabled/selectable Honcho host is included, including profile-derived
  hosts such as `hermes_uiux`;
- expected observer/target scope count is non-zero for enabled workspaces;
- planned skill removals cover dedicated retired packages and only
  retired-sourced `json-canvas`/`defuddle` copies;
- archive source/destination are correct and share a device;
- `qmd_index` is `skills` and `pid_docs_touched` is false.

Preflight does not create a receipt directory or archive root.

## 3. Apply (post-merge only)

Use the exact reviewed merge SHA:

```bash
python scripts/archive_legacy_vault.py apply \
  --expected-merge-sha <MERGE_SHA> \
  --confirm RETIRE-OBSIDIAN-POST-MERGE
```

The transaction:

1. Re-runs preflight and merge/idle gates.
2. Creates a restricted durable receipt.
3. Materializes a Honcho host allowlist when a legacy flat config could select
   unknown hosts.
4. Inventories every selected Honcho workspace/peer observer-target pair,
   removes retired facts from cards/conclusions, records context/search status,
   and preserves messages and sessions.
5. Stops/disables `gmail-intake-pubsub.service` and
   `gmail-intake-watch-renew.timer`, stops a running renewal service, verifies
   no collector unit remains active, and neutralizes legacy `invoke-agent`
   branches without redirecting them to future intake.
6. Atomically moves retired skill directories into the receipt and reconciles
   `.hub/lock.json`, `.bundled_manifest`, `.usage.json`, taps/index caches, and
   prompt snapshots for both default and profile homes.
7. Removes current retired-system memory paragraphs and only `OBSIDIAN_*`
   environment assignments, preserving file mode/ownership atomically.
8. Regenerates/refreshes the QMD `skills` catalog/index. If the established
   refresh service fails, it uses a targeted `--index skills` rebuild/embed
   fallback. It does not invoke or modify `pid-docs`.
9. Rechecks idle state, restarts the gateway exactly once, and verifies it is
   active.
10. Only after the complete all-host receipt, atomically renames
    `$HERMES_HOME/obsidian-vault` on the same filesystem to:

    ```text
    ~/archives/hermes-retired-note-system/obsidian-vault-<UTC>/vault
    ```

11. Writes and fsyncs `MANIFEST.json`, `SHA256SUMS`, `FILE_METADATA.tsv`, and
    `VERIFICATION_RECEIPT.json`. The manifest includes files, directories,
    symlinks, SHA-256 data, modes, uid/gid, mtimes, counts, and tree hash.
12. Runs verification and marks the receipt `verified`.

Any exception attempts receipt-driven rollback. A rollback failure is itself
recorded and is a hard operator STOP.

## 4. Verify

Use the receipt ID printed by apply:

```bash
python scripts/archive_legacy_vault.py verify --receipt-id <RECEIPT_ID>
```

Verification proves:

- the original vault path is absent and is not a symlink;
- every original archive payload entry still matches type, SHA-256, size,
  mode, uid/gid, and mtime;
- all four archive receipt files exist and bind to the payload tree hash;
- Gmail collector/renewal units are inactive;
- the gateway is active;
- a controlled structural interaction does not recreate the vault;
- `pid-docs` was not touched.

## 5. Restore

Restore only when the receipt remains authoritative:

```bash
python scripts/archive_legacy_vault.py restore --receipt-id <RECEIPT_ID>
```

Restore first compares every receipt-owned post-apply fingerprint. If a person,
service, or concurrent process changed a target, it stops rather than overwrite.
When safe, it:

- atomically moves the verified vault back and removes only the four generated
  archive receipt files;
- restores file modes, ownership, mtimes, registries, caches, prompt snapshot,
  current memory/env files, and invoke-agent source from backups;
- restores moved skill directories;
- restores Honcho cards/conclusions where the receipt permits and fails closed
  on card drift;
- restores prior Gmail unit enabled/active state;
- restarts the gateway if apply had restarted it.

Messages, sessions, credentials, Gmail intake state, and vault content are never
deleted by the transaction.

## Acceptance evidence

The ticket must contain:

- reviewed merge SHA and canonical HEAD;
- preflight summary;
- receipt ID/path and `verified` state;
- selected Honcho host/scope counts and mutation counts (not content);
- Gmail unit before/after states;
- skill-registry/QMD/gateway verification;
- archive payload counts/tree hash and all four receipt paths;
- explicit confirmation that source absence survived controlled interaction;
- either “restore not required” or the restore receipt/result.

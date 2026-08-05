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
| `verify` | receipt only | Re-verify services, the authenticated archive/journal chain, and source absence. |
| `restore` | yes | Restore only receipt-owned changes; stop on concurrent drift rather than overwrite. |

The durable receipt root defaults to:

```text
$HERMES_HOME/retired-note-cutover-receipts/
```

It is created with mode `0700`; receipt, journal, and backup files use `0600`.
Every apply-side mutation has a hash-chained intent record fsynced before the
mutation and a completion record fsynced afterward. Restore has a separate
append-only `RESTORE.jsonl` journal.

## STOP gates

Stop without improvising when the helper reports `state: STOP`. Important gates
include:

- canonical checkout dirty or missing the reviewed merge SHA;
- missing/malformed gateway runtime status or PID evidence, active gateway
  turns, or matching gateway-owned direct-child workers;
- malformed or unsafe Honcho/config/registry paths;
- incomplete selectable-host or observer/target receipts;
- archive destination on another device;
- vault source absent, already a symlink, changed since preflight, or recreated;
- unsupported vault filesystem objects;
- legacy Gmail collector/renewal service still active;
- gateway not healthy after its one controlled restart;
- QMD skills refresh failure after targeted fallback;
- archive layout, metadata, control-artifact, journal-chain, hash, or fsync
  failure;
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

- every explicitly configured enabled/selectable Honcho host is included;
- disabled aliases remain disabled, legacy dot/underscore aliases are not
  rewritten, and unknown profile hosts are not materialized;
- expected observer/target scope count is non-zero for enabled workspaces;
- planned skill removals cover dedicated retired packages and only
  retired-sourced `json-canvas`/`defuddle` copies;
- archive source/destination are correct and share a device;
- `qmd_index` is `skills` and `pid_docs_touched` is false.

Preflight does not create a receipt directory or archive root.

## 3. Apply (post-merge only)

Use the exact reviewed full 40- or 64-hex-character merge SHA:

```bash
python scripts/archive_legacy_vault.py apply \
  --expected-merge-sha <MERGE_SHA> \
  --confirm RETIRE-OBSIDIAN-POST-MERGE
```

The transaction:

1. Re-runs preflight and merge/idle gates.
2. Creates a restricted durable receipt.
3. Uses only the configured Honcho host membership. It does not add discovered
   profile aliases, shadow dot/underscore aliases, or re-enable disabled hosts.
4. Inventories every selected Honcho workspace/peer observer-target pair,
   removes retired facts from cards, and adds receipt-owned corrective
   conclusions that reference the matched historical conclusion IDs. Original
   conclusions, messages, and sessions are preserved because the SDK cannot
   recreate original server IDs, timestamps, or reasoning metadata.
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
9. Only after the complete all-host receipt, atomically renames
    `$HERMES_HOME/obsidian-vault` on the same filesystem to:

    ```text
    ~/archives/hermes-retired-note-system/obsidian-vault-<UTC>/vault
    ```

10. Writes and fsyncs `MANIFEST.json`, `SHA256SUMS`, `FILE_METADATA.tsv`, and
    `VERIFICATION_RECEIPT.json` **beside** `vault/`, never inside the payload.
    The manifest includes files, directories, symlinks, SHA-256 data, modes,
    uid/gid, mtimes, counts, source path, and tree hash. Payload files with any
    of those same names remain ordinary vault content and are not interpreted
    as controls.
11. Writes `.drain_request.json` with the current gateway instantiation epoch,
    waits up to 120 seconds for `active_agents == 0` and no matching direct
    gateway child, performs one supervised
    `systemctl --user restart hermes-gateway.service`, verifies the service is
    active and did not recreate the retired source path, then clears the drain
    marker.
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
- all four outer archive artifacts exist, match one another, and bind to the
  exact `archive_outer_receipt_write` intent in the transaction journal;
- Gmail collector/renewal units are inactive;
- the gateway is active;
- apply's post-restart source-absence check was recorded;
- `pid-docs` was not touched.

## 5. Restore

Restore only when the receipt remains authoritative:

```bash
python scripts/archive_legacy_vault.py restore --receipt-id <RECEIPT_ID>
```

Restore validates the apply journal, classifies each owned target as exact
pre-state or exact transaction post-state, and checks Honcho card drift before
performing any rollback write. An intent without a completion record is still
recoverable: exact pre-state is a no-op, exact post-state is restored, and any
third state is a hard STOP. When safe, it:

- atomically moves the authenticated vault back and removes only the four outer
  generated archive artifacts; same-named files inside the payload are retained;
- restores file modes, ownership, mtimes, registries, caches, prompt snapshot,
  current memory/env files, and invoke-agent source from backups;
- restores moved skill directories;
- restores exact Honcho card snapshots and removes receipt-owned corrective
  conclusions by ID (or exact receipt-owned content when a post-create crash
  prevented the ID from being journaled); original conclusions are untouched;
- restores the exact prior enabled/active state of Gmail, QMD-refresh, and
  gateway units;
- restores a pre-existing drain marker byte-for-byte, or removes the
  transaction-created marker;
- re-runs only journaled targeted QMD refresh commands as compensating derived
  state after local skill/config restoration.

Messages, sessions, credentials, Gmail intake state, historical Honcho
conclusions, and vault content are never deleted by the transaction.

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

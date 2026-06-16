# Finite No-Agent Cron Migration

This runbook records the safe migration path for legacy finite no-agent cron
executors to the explicit terminal-success contract.

## Command Center Kanban Corruption Executor

Active-path evidence from the default Hermes profile:

- Job ID: `27cf5683d99f`
- Name: `Command Center Kanban corruption executor`
- Live job record: `$HERMES_HOME/cron/jobs.json`
- Live script wrapper: `$HERMES_HOME/scripts/command_center_kanban_corruption_executor.sh`
- Live implementation: `$HERMES_HOME/scripts/command_center_kanban_corruption_executor.py`
- Runtime workdir: `/home/droid/hermes`
- Script repo status: not repo-backed in this checkout; do not edit it from a
  dev worker ticket.
- Terminal evidence: `$HERMES_HOME/cron/output/27cf5683d99f/2026-06-15_15-17-09.md`
- Terminal marker line: `DONE: all ordered Kanban corruption Command Center items are terminal success.`

The live script already emits a recognized terminal-success marker on the first
successful finite completion, so no script parser broadening or live script edit
is required for this job. The remaining migration is an operator-owned live
cron state update that preserves the existing job ID, name, schedule, repeat
metadata, history, and output directory.

## Operator Migration

After the terminal-success cron contract is deployed to the active runtime,
apply this minimal live-state change to the `27cf5683d99f` object in
`$HERMES_HOME/cron/jobs.json`:

```diff
 {
   "id": "27cf5683d99f",
   "name": "Command Center Kanban corruption executor",
   "script": "command_center_kanban_corruption_executor.sh",
   "no_agent": true,
+  "disable_on_terminal_success": true,
   "schedule": {
     "kind": "interval",
     "minutes": 20,
     "display": "every 20m"
   }
 }
```

Do not delete historical output files under
`$HERMES_HOME/cron/output/27cf5683d99f/`. Do not recreate the job with a new ID.

If the job is already complete and no longer emits `DONE:` because its state file
has `done_notified`, the operator can either leave the field in place for future
finite executors or perform an explicit reviewed pause. If the script is expected
to emit `DONE:` again, the next successful tick with this opt-in field will pause
the job automatically and record `last_terminal_output_path`.

## Safe Verification

Use a temporary `HERMES_HOME` and a minimal copied script shape before touching
live state:

```bash
tmp_home=$(mktemp -d /tmp/hermes-finite-cron.XXXXXX)
mkdir -p "$tmp_home/home/scripts"
cat > "$tmp_home/home/scripts/command_center_kanban_corruption_executor.sh" <<'SCRIPT'
#!/usr/bin/env bash
printf '%s\n' 'DONE: all ordered Kanban corruption Command Center items are terminal success.'
SCRIPT
chmod +x "$tmp_home/home/scripts/command_center_kanban_corruption_executor.sh"
HERMES_HOME="$tmp_home/home" python - <<'PY'
from cron.jobs import create_job, get_job, save_jobs
from cron.scheduler import tick
from datetime import datetime, timezone

job = create_job(
    prompt='Script-only sequential executor for migration smoke.',
    name='Command Center Kanban corruption executor',
    schedule='every 20m',
    script='command_center_kanban_corruption_executor.sh',
    no_agent=True,
    deliver='local',
    disable_on_terminal_success=True,
)
job['id'] = '27cf5683d99f'
job['repeat']['times'] = None
job['next_run_at'] = datetime.now(timezone.utc).isoformat()
save_jobs([job])
assert tick(verbose=False) == 1
updated = get_job('27cf5683d99f')
assert updated['state'] == 'paused'
assert updated['enabled'] is False
assert updated['paused_reason'] == 'terminal success: DONE marker'
assert updated['last_terminal_output_path']
PY
rm -rf "$tmp_home"
```

After approved live migration and the next intended tick, verify read-only:

```bash
hermes cron list --all
```

Expected rendering for the migrated job includes:

- `27cf5683d99f [paused]`
- `Terminal:  auto-pause on DONE/terminal_success marker`
- `Paused:    terminal success: DONE marker`
- `Terminal output: <path under $HERMES_HOME/cron/output/27cf5683d99f/>`

## Rollback

If automatic pausing is not desired, remove `disable_on_terminal_success` from
the live job record or set it to `false`. If the job was paused unintentionally,
run:

```bash
hermes cron resume 27cf5683d99f
```

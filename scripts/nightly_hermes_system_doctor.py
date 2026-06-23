#!/usr/bin/env python3
"""Nightly Hermes system doctor for Sligo Labs.

Repo-managed cron entrypoint. Healthy normal execution prints nothing;
``--status`` prints a compact OK/failure report. The script intentionally avoids
printing secrets and treats optional unconfigured providers as non-blocking.

Default-profile deploy/update path:
``HERMES_HOME=/home/droid/.hermes HERMES_REPO=/home/droid/hermes /home/droid/hermes/.venv/bin/python /home/droid/hermes/scripts/nightly_hermes_system_doctor.py --install-live``

The install path backs up the current live script under
``$HERMES_HOME/scripts/archive/`` before replacing
``$HERMES_HOME/scripts/nightly_hermes_system_doctor.py``. Rollback is a simple
restore of the selected archive file to that live path.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib import request

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_cli.codex_auth_incidents import (
    classify_codex_auth_incident,
    collect_codex_auth_evidence,
    render_codex_auth_incident_summary,
)

REPO = Path(os.environ.get("HERMES_REPO", "/home/droid/hermes"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/droid/.hermes"))
HERMES = REPO / ".venv" / "bin" / "hermes"
PYTHON = Path(os.environ.get("HERMES_PYTHON", str(REPO / ".venv" / "bin" / "python")))
STATE = HERMES_HOME / "state" / "nightly-hermes-system-doctor.json"
LIVE_SCRIPT = HERMES_HOME / "scripts" / "nightly_hermes_system_doctor.py"
REPO_SCRIPT = REPO / "scripts" / "nightly_hermes_system_doctor.py"
CRON_JOBS = HERMES_HOME / "cron" / "jobs.json"
HONCHO_WATCHDOG = HERMES_HOME / "skills" / "devops" / "honcho-health-watchdog" / "scripts" / "honcho_daily_health_watchdog.py"
ENTRYPOINT_SOURCE_LABEL = "repo-managed-nightly-hermes-system-doctor-v1"
LIVE_INSTALL_COMMAND = (
    "HERMES_HOME=/home/droid/.hermes HERMES_REPO=/home/droid/hermes "
    "/home/droid/hermes/.venv/bin/python "
    "/home/droid/hermes/scripts/nightly_hermes_system_doctor.py --install-live"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
HONCHO_INFERENCE_DISABLED_RE = re.compile(
    r"RuntimeError:\s*inference disabled by HONCHO_WATCHDOG_NO_INFERENCE=1"
)


def coding_worker_backend() -> str:
    code = "from agent.opencode_worker import load_coding_worker_backend; print(load_coding_worker_backend())"
    r = run([str(PYTHON if PYTHON.exists() else sys.executable), "-c", code], timeout=60, cwd=REPO)
    return (r["output"] or "").strip() if r["exit"] == 0 else "unknown"


def clean(text: str, limit: int = 4000) -> str:
    text = ANSI_RE.sub("", text or "")
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", text)
    if len(text) > limit:
        return text[:limit] + "…<truncated>"
    return text


def run(cmd: list[str], *, timeout: int = 120, cwd: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    merged_env = os.environ.copy()
    merged_env.setdefault("HERMES_HOME", str(HERMES_HOME))
    if env:
        merged_env.update(env)
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd or REPO),
            env=merged_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return {"cmd": " ".join(cmd), "exit": p.returncode, "output": clean(p.stdout)}
    except subprocess.TimeoutExpired as exc:
        return {"cmd": " ".join(cmd), "exit": 124, "output": clean((exc.stdout or "") + "\n<TIMEOUT>")}
    except Exception as exc:  # noqa: BLE001 - watchdog should report, not crash silently
        return {"cmd": " ".join(cmd), "exit": 125, "output": f"<exception> {type(exc).__name__}: {exc}"}


def sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def script_provenance() -> dict[str, Any]:
    live_sha = sha256_file(LIVE_SCRIPT)
    source_sha = sha256_file(REPO_SCRIPT)
    running_path = Path(__file__).resolve()
    provenance = {
        "script_entrypoint_source": ENTRYPOINT_SOURCE_LABEL,
        "script_running_path": str(running_path),
        "script_live_path": str(LIVE_SCRIPT),
        "script_source_path": str(REPO_SCRIPT),
        "script_live_install_command": LIVE_INSTALL_COMMAND,
        "script_live_sha256": live_sha,
        "script_source_sha256": source_sha,
        "script_live_exists": LIVE_SCRIPT.exists(),
        "script_source_exists": REPO_SCRIPT.exists(),
        "script_matches_source": bool(live_sha and source_sha and live_sha == source_sha),
    }
    provenance.update(cron_entrypoint_provenance())
    provenance["script_pickup_ready"] = bool(
        provenance["script_matches_source"] and provenance.get("cron_invokes_live_script")
    )
    if provenance["script_pickup_ready"]:
        provenance["script_pickup_required"] = "none; cron invokes the live script that matches the repo source"
    elif not provenance.get("cron_invokes_live_script"):
        provenance["script_pickup_required"] = "update the Nightly Hermes system doctor cron script entrypoint to the live script path"
    else:
        provenance["script_pickup_required"] = f"run `{LIVE_INSTALL_COMMAND}` to copy the repo script to the live cron path"
    return provenance


def cron_entrypoint_provenance() -> dict[str, Any]:
    """Return secret-safe evidence for the cron job that invokes this doctor."""
    facts: dict[str, Any] = {
        "cron_jobs_path": str(CRON_JOBS),
        "cron_jobs_exists": CRON_JOBS.exists(),
        "cron_entrypoint_resolution": "cron.scheduler._run_job_script resolves relative job script names under $HERMES_HOME/scripts",
        "cron_job_id": None,
        "cron_job_name": None,
        "cron_job_script": None,
        "cron_job_script_resolved_path": None,
        "cron_job_schedule": None,
        "cron_job_enabled": None,
        "cron_job_state": None,
        "cron_job_no_agent": None,
        "cron_invokes_live_script": False,
    }
    if not CRON_JOBS.exists():
        return facts
    try:
        payload = json.loads(CRON_JOBS.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - provenance should not break the doctor
        facts["cron_jobs_read_error"] = f"{type(exc).__name__}: {exc}"
        return facts

    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        facts["cron_jobs_read_error"] = "jobs.json does not contain a jobs list"
        return facts

    script_name = REPO_SCRIPT.name
    candidates = [
        item
        for item in jobs
        if isinstance(item, dict) and Path(str(item.get("script") or "")).name == script_name
    ]
    if not candidates:
        return facts
    job = next((item for item in candidates if item.get("name") == "Nightly Hermes system doctor"), candidates[0])
    script_value = str(job.get("script") or "")
    raw_script = Path(script_value).expanduser()
    resolved_script = raw_script.resolve() if raw_script.is_absolute() else (HERMES_HOME / "scripts" / raw_script).resolve()

    facts.update(
        {
            "cron_job_id": job.get("id"),
            "cron_job_name": job.get("name"),
            "cron_job_script": script_value,
            "cron_job_script_resolved_path": str(resolved_script),
            "cron_job_schedule": job.get("schedule_display") or (job.get("schedule") or {}).get("display"),
            "cron_job_enabled": job.get("enabled"),
            "cron_job_state": job.get("state"),
            "cron_job_no_agent": job.get("no_agent"),
            "cron_invokes_live_script": resolved_script == LIVE_SCRIPT.resolve(),
        }
    )
    return facts


def install_live(*, dry_run: bool = False) -> int:
    if not REPO_SCRIPT.exists():
        print(f"ERROR source script missing: {REPO_SCRIPT}", file=sys.stderr)
        return 1

    source_sha = sha256_file(REPO_SCRIPT)
    live_sha = sha256_file(LIVE_SCRIPT)
    archive_dir = HERMES_HOME / "scripts" / "archive"
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = archive_dir / f"nightly_hermes_system_doctor.py.{stamp}.bak"

    print(f"source={REPO_SCRIPT}")
    print(f"destination={LIVE_SCRIPT}")
    print(f"archive={backup_path}")
    print(f"source_sha256={source_sha}")
    print(f"current_live_sha256={live_sha}")
    print(f"matches_source={bool(source_sha and live_sha and source_sha == live_sha)}")

    if dry_run:
        print("dry_run=true")
        return 0

    archive_dir.mkdir(parents=True, exist_ok=True)
    LIVE_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    if LIVE_SCRIPT.exists():
        shutil.copy2(LIVE_SCRIPT, backup_path)
    shutil.copy2(REPO_SCRIPT, LIVE_SCRIPT)
    LIVE_SCRIPT.chmod(LIVE_SCRIPT.stat().st_mode | 0o111)
    installed_sha = sha256_file(LIVE_SCRIPT)
    print(f"installed_live_sha256={installed_sha}")
    print(f"installed_matches_source={bool(source_sha and installed_sha == source_sha)}")
    if LIVE_SCRIPT.exists():
        print(f"rollback=cp {backup_path} {LIVE_SCRIPT}")
    return 0 if source_sha and installed_sha == source_sha else 1


def add_issue(issues: list[dict[str, str]], severity: str, name: str, detail: str) -> None:
    issues.append({"severity": severity, "name": name, "detail": clean(detail, 1800)})


def honcho_watchdog_repair_inference_disabled(output: str) -> bool:
    """Return True when the Honcho watchdog intentionally skipped repair inference."""
    return bool(HONCHO_INFERENCE_DISABLED_RE.search(output or ""))


def honcho_watchdog_reported_anomaly_with_repair_skipped(exit_code: Any, output: str) -> bool:
    """Return True for deterministic anomaly reports with skipped repair inference."""
    return (
        exit_code == 0
        and output.startswith("Honcho health alert: deterministic watchdog found unexpected output")
        and "--- Raw watchdog facts ---" in output
        and honcho_watchdog_repair_inference_disabled(output)
    )


def extract_compression_routes(stdout: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    route_results: list[dict[str, Any]] = []
    text = stdout or ""
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        routes = payload.get("routes")
        if isinstance(routes, list):
            route_results = [item for item in routes if isinstance(item, dict)]
    return route_results


def record_compression_route_results(
    routes: dict[str, Any],
    issues: list[dict[str, str]],
    facts: dict[str, Any],
) -> list[dict[str, Any]]:
    output = str(routes.get("output") or "")
    facts["compression_routes_exit"] = routes.get("exit")
    facts["compression_routes_output"] = output
    route_results = extract_compression_routes(output)
    facts["compression_route_results"] = route_results
    if not route_results:
        add_issue(
            issues,
            "critical",
            "compression configured provider routes missing",
            output or "No auxiliary.compression primary or fallback routes were discovered.",
        )
        return route_results
    for item in route_results:
        if item.get("exit") == 0:
            continue
        label = item.get("label") or "unknown"
        provider = item.get("provider") or "unknown-provider"
        model = item.get("model") or "unknown-model"
        add_issue(
            issues,
            "critical",
            f"compression configured provider route failed: {label} {provider}/{model}",
            json.dumps(item, sort_keys=True),
        )
    return route_results


def check_compression_routes(
    routes_smoke: dict[str, Any],
    issues: list[dict[str, str]],
    facts: dict[str, Any],
) -> list[dict[str, Any]]:
    """Doctor route-smoke path for configured compression providers."""
    return record_compression_route_results(routes_smoke, issues, facts)


def check_hermes_doctor(issues: list[dict[str, str]], facts: dict[str, Any]) -> None:
    r = run([str(HERMES), "doctor"], timeout=300)
    facts["hermes_doctor_exit"] = r["exit"]
    facts["hermes_doctor_excerpt"] = r["output"][-1800:]
    out = r["output"]
    if r["exit"] != 0:
        add_issue(issues, "critical", "hermes doctor exited nonzero", out[-1800:])
    fatal_lines = []
    for line in out.splitlines():
        stripped = line.strip()
        # Keep known local non-blockers quiet: venv entrypoint is present and optional provider/tool gaps are expected.
        if "~/.local/bin/hermes not found" in stripped:
            continue
        if "Run 'hermes setup' to configure missing API keys" in stripped:
            continue
        if stripped.startswith("✗"):
            fatal_lines.append(stripped)
        if re.search(r"(?i)auth failed|expired|token_invalidated|security advisories.*active", stripped):
            fatal_lines.append(stripped)
    if fatal_lines:
        add_issue(issues, "critical", "hermes doctor reported blocking lines", "\n".join(fatal_lines))


def check_auth_list(issues: list[dict[str, str]], facts: dict[str, Any]) -> None:
    r = run([str(HERMES), "auth", "list"], timeout=120)
    facts["hermes_auth_list_exit"] = r["exit"]
    facts["hermes_auth_list"] = r["output"]
    if r["exit"] != 0:
        add_issue(issues, "critical", "hermes auth list failed", r["output"])
    bad = [line.strip() for line in r["output"].splitlines() if re.search(r"(?i)auth failed|expired|exhausted|invalid|401|403", line)]
    if bad:
        add_issue(issues, "critical", "hermes credential pool has unhealthy credentials", "\n".join(bad))


def check_codex_auth_incidents(issues: list[dict[str, str]], facts: dict[str, Any]) -> None:
    """Correlate recent OpenAI Codex auth failures into one route incident."""
    evidence = collect_codex_auth_evidence(HERMES_HOME)
    facts["codex_auth_evidence_count"] = len(evidence)
    incident = classify_codex_auth_incident(evidence, auth_status_text=str(facts.get("hermes_auth_list") or ""))
    if not incident:
        facts["codex_auth_incident"] = None
        return
    facts["codex_auth_incident"] = incident.to_dict()
    add_issue(
        issues,
        "critical" if incident.current_state == "current_auth_failure" else "warning",
        "openai-codex credential invalidation incident detected",
        render_codex_auth_incident_summary(incident),
    )


def python_smoke(code: str, timeout: int = 180) -> dict[str, Any]:
    python = PYTHON if PYTHON.exists() else Path(sys.executable)
    return run([str(python), "-c", code], timeout=timeout, cwd=REPO)


def check_main_inference(issues: list[dict[str, str]], facts: dict[str, Any]) -> None:
    code = r'''
from agent.auxiliary_client import call_llm
resp = call_llm(
    provider="openai-codex",
    model="gpt-5.5",
    messages=[{"role":"user","content":"Reply with exactly: HERMES_MAIN_SMOKE_OK"}],
    max_tokens=12,
    timeout=90,
)
text = (resp.choices[0].message.content or "").strip()
print(text)
'''
    r = python_smoke(code, timeout=150)
    facts["main_inference_exit"] = r["exit"]
    facts["main_inference_output"] = r["output"]
    if r["exit"] != 0 or "HERMES_MAIN_SMOKE_OK" not in r["output"]:
        add_issue(issues, "critical", "main openai-codex inference smoke failed", r["output"])


def check_compression_inference(issues: list[dict[str, str]], facts: dict[str, Any]) -> None:
    # Two checks are intentionally separate:
    # 1. The configured compression task can still produce a summary (fallback may help).
    # 2. Every explicitly configured compression route is healthy on its own.
    # A marker-only task smoke can hide stale primary or fallback credentials
    # because another configured route returned the marker.
    code = r'''
import json
from agent.auxiliary_client import call_llm
resp = call_llm(
    task="compression",
    messages=[{"role":"user","content":"Reply with exactly: HERMES_COMPRESSION_SMOKE_OK"}],
    max_tokens=16,
    timeout=120,
)
text = (resp.choices[0].message.content or "").strip()
print(json.dumps({"text": text, "response_model": getattr(resp, "model", None)}, sort_keys=True))
'''
    r = python_smoke(code, timeout=180)
    facts["compression_inference_exit"] = r["exit"]
    facts["compression_inference_output"] = r["output"]
    if r["exit"] != 0 or "HERMES_COMPRESSION_SMOKE_OK" not in r["output"]:
        add_issue(issues, "critical", "compression auxiliary inference smoke failed", r["output"])

    routes_code = r'''
import json
import re
import sys
from agent.auxiliary_client import call_llm

try:
    from hermes_cli.config import load_config_readonly
except ImportError:
    from hermes_cli.config import load_config as load_config_readonly

SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^\s,;}]+")

def scrub(value):
    if value is None:
        return None
    text = SECRET_RE.sub(r"\1=<redacted>", str(value))
    return text[-1200:]

def route_from(label, item, default_timeout=None):
    if not isinstance(item, dict):
        return None
    route = {
        "label": label,
        "provider": item.get("provider"),
        "model": item.get("model"),
        "base_url": item.get("base_url"),
        "api_key": item.get("api_key"),
        "extra_body": item.get("extra_body"),
        "timeout": item.get("timeout", default_timeout),
    }
    if not route["provider"] and not route["model"] and not route["base_url"]:
        return None
    return route

cfg = load_config_readonly()
compression = ((cfg.get("auxiliary") or {}).get("compression") or {})
routes = []
primary = route_from("primary", compression, compression.get("timeout"))
if primary:
    routes.append(primary)
for index, entry in enumerate(compression.get("fallback_chain") or []):
    route = route_from(f"fallback_chain[{index}]", entry, compression.get("timeout"))
    if route:
        routes.append(route)

results = []
for index, route in enumerate(routes):
    marker = f"HERMES_COMPRESSION_ROUTE_OK_{index}"
    result = {
        "label": route["label"],
        "provider": route.get("provider"),
        "model": route.get("model"),
        "base_url": scrub(route.get("base_url")),
        "exit": 1,
        "output": "",
    }
    try:
        resp = call_llm(
            provider=route.get("provider"),
            model=route.get("model"),
            base_url=route.get("base_url"),
            api_key=route.get("api_key"),
            messages=[{"role":"user","content":f"Reply with exactly: {marker}"}],
            max_tokens=16,
            timeout=route.get("timeout") or 120,
            extra_body=route.get("extra_body"),
        )
        text = (resp.choices[0].message.content or "").strip()
        result["output"] = scrub(text)
        result["response_model"] = scrub(getattr(resp, "model", None))
        result["exit"] = 0 if marker in text else 1
    except Exception as exc:  # noqa: BLE001 - report route-specific smoke failures
        result["error"] = scrub(f"{type(exc).__name__}: {exc}")
    results.append(result)

print(json.dumps({"routes": results}, sort_keys=True))
if not routes or any(item.get("exit") != 0 for item in results):
    sys.exit(1)
'''
    routes = python_smoke(routes_code, timeout=360)
    record_compression_route_results(routes, issues, facts)


def post_json(url: str, body: dict[str, Any], timeout: int = 90) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer healthcheck"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed local URL
        return resp.status, resp.read(4000).decode("utf-8", errors="replace")


def check_honcho(issues: list[dict[str, str]], facts: dict[str, Any]) -> None:
    if HONCHO_WATCHDOG.exists():
        r = run(
            [str(PYTHON if PYTHON.exists() else Path(sys.executable)), str(HONCHO_WATCHDOG), "--status"],
            timeout=300,
            cwd=HERMES_HOME,
            env={"HONCHO_WATCHDOG_NO_INFERENCE": "1"},
        )
        exit_code = r["exit"]
        output = r["output"]
        facts["honcho_watchdog_exit"] = exit_code
        facts["honcho_watchdog_output"] = output
        if exit_code == 0 and output.startswith("OK Honcho watchdog"):
            pass
        elif honcho_watchdog_reported_anomaly_with_repair_skipped(exit_code, output):
            add_issue(
                issues,
                "critical",
                "honcho deterministic watchdog reported anomalies; repair inference skipped",
                output,
            )
        else:
            add_issue(issues, "critical", "honcho deterministic watchdog failed", output)
    else:
        add_issue(issues, "warning", "honcho watchdog script missing", str(HONCHO_WATCHDOG))

    for model, marker in [("gpt-5.3-codex-spark", "HONCHO_SPARK_SMOKE_OK"), ("gpt-5.4-mini", "HONCHO_MINI_SMOKE_OK")]:
        try:
            status, text = post_json(
                "http://127.0.0.1:8645/v1/chat/completions",
                {
                    "model": model,
                    "messages": [{"role": "user", "content": f"Reply with exactly: {marker}"}],
                    "max_tokens": 16,
                    "temperature": 0,
                },
                timeout=90,
            )
            facts[f"honcho_proxy_{model}_status"] = status
            facts[f"honcho_proxy_{model}_excerpt"] = clean(text, 1200)
            if status != 200 or marker not in text:
                add_issue(issues, "critical", f"honcho proxy inference smoke failed for {model}", text)
        except Exception as exc:  # noqa: BLE001
            facts[f"honcho_proxy_{model}_error"] = f"{type(exc).__name__}: {exc}"
            add_issue(issues, "critical", f"honcho proxy inference smoke failed for {model}", f"{type(exc).__name__}: {exc}")


def record_honcho_watchdog_result(
    result: dict[str, Any],
    issues: list[dict[str, str]],
    facts: dict[str, Any],
) -> None:
    """Record the deterministic Honcho watchdog status result."""
    output = str(result.get("output") or "")
    exit_code = result.get("exit")
    facts["honcho_watchdog_exit"] = exit_code
    facts["honcho_watchdog_output"] = output

    if exit_code == 0 and output.startswith("OK Honcho watchdog"):
        return

    if honcho_watchdog_reported_anomaly_with_repair_skipped(exit_code, output):
        add_issue(
            issues,
            "critical",
            "honcho deterministic watchdog reported anomalies; repair inference skipped",
            output,
        )
        return

    add_issue(issues, "critical", "honcho deterministic watchdog failed", output)


def check_honcho_watchdog_status(
    watchdog_status: dict[str, Any],
    issues: list[dict[str, str]],
    facts: dict[str, Any],
) -> None:
    """Doctor status path for Honcho deterministic watchdog output."""
    record_honcho_watchdog_result(watchdog_status, issues, facts)


def check_coding_worker(issues: list[dict[str, str]], facts: dict[str, Any]) -> None:
    backend = coding_worker_backend()
    facts["coding_worker_backend"] = backend
    if backend == "opencode":
        code = r'''
import json
import sys
from agent.opencode_worker import check_opencode_binary

ok, detail = check_opencode_binary()
print(json.dumps({'phase': 'binary', 'ok': ok, 'detail': detail}, sort_keys=True))
if not ok:
    sys.exit(1)
'''
        smoke = python_smoke(code, timeout=60)
        facts["coding_worker_smoke_exit"] = smoke["exit"]
        facts["coding_worker_smoke_output"] = smoke["output"]
        if smoke["exit"] != 0:
            add_issue(issues, "critical", "OpenCode coding-worker binary check failed", smoke["output"])
        return

    if backend != "codex":
        add_issue(issues, "critical", "coding worker backend is not supported", backend)
        return

    code = r'''
import json
import sys
from agent.codex_worker_auth import create_codex_worker_home
from agent.transports.codex_app_server import check_codex_binary
from agent.transports.codex_app_server_session import CodexAppServerSession

ok, detail = check_codex_binary()
print(json.dumps({'phase': 'binary', 'ok': ok, 'detail': detail}, sort_keys=True))
if not ok:
    sys.exit(1)

marker = 'CODEX_CODING_WORKER_OK'
with create_codex_worker_home(prefix='doctor-codex-worker-') as lease:
    with CodexAppServerSession(
        cwd='/tmp',
        codex_home=str(lease.path),
        extra_args=['-c', 'model_reasoning_effort="medium"'],
        scope_kind='coding-worker-smoke',
        scope_purpose='Nightly Hermes Codex coding-worker smoke',
    ) as session:
        result = session.run_turn(
            user_input=f'Smoke test only. Return exactly {marker} and nothing else.',
            turn_timeout=240,
        )
print(json.dumps({
    'phase': 'turn',
    'error': result.error,
    'final_text': (result.final_text or '').strip(),
    'interrupted': result.interrupted,
    'thread_id': result.thread_id,
    'tool_iterations': result.tool_iterations,
    'turn_id': result.turn_id,
}, sort_keys=True))
if result.error or result.interrupted or marker not in (result.final_text or ''):
    sys.exit(1)
'''
    smoke = python_smoke(code, timeout=540)
    facts["coding_worker_smoke_exit"] = smoke["exit"]
    facts["coding_worker_smoke_output"] = smoke["output"]
    if smoke["exit"] != 0 or "CODEX_CODING_WORKER_OK" not in smoke["output"]:
        add_issue(issues, "critical", "Codex coding-worker inference smoke failed", smoke["output"])


def _compact_issue_detail(detail: str, *, max_lines: int = 8, max_chars: int = 1200) -> str:
    """Return a Discord-readable, secret-sanitized detail block."""
    text = clean(detail, max_chars)
    lines: list[str] = []
    refs = sorted(set(re.findall(r"err_[A-Za-z0-9]+", text)))
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Worker CLIs can emit full JSON event envelopes; keep the useful
        # human line/ref and drop the bulky envelope from Discord alerts.
        if line.startswith("{") and line.endswith("}"):
            continue
        if len(line) > 220:
            line = line[:217].rstrip() + "..."
        lines.append(line)
        if len(lines) >= max_lines:
            break
    if refs and not any(ref in " ".join(lines) for ref in refs):
        lines.append("refs=" + ", ".join(refs))
    if not lines:
        lines = ["See state file for captured sanitized output."]
    return "\n".join(lines)


def _print_issue_report(issues: list[dict[str, str]]) -> None:
    critical = sum(1 for item in issues if item.get("severity") == "critical")
    warnings = sum(1 for item in issues if item.get("severity") == "warning")
    status = "BLOCKED" if critical else "REVIEW REQUIRED"
    print(f"**{status} — Nightly Hermes system doctor found {len(issues)} issue(s)**")
    print()
    print("### Findings")
    for item in issues:
        severity = item.get("severity", "issue")
        name = item.get("name", "unnamed issue")
        print(f"- **{severity} — {name}**")
        detail = _compact_issue_detail(item.get("detail", ""))
        if detail:
            print("  ```text")
            print("\n".join(f"  {line}" for line in detail.splitlines()))
            print("  ```")
    print()
    print("### Evidence")
    print(f"- Critical: `{critical}`; warnings: `{warnings}`")
    print(f"- State: `{STATE}`")
    print()
    print("### Next")
    print("- Fix or re-auth the failing component, then run `nightly_hermes_system_doctor.py --status` to verify the full check set.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true", help="print OK output even when healthy")
    parser.add_argument(
        "--skip-coding-worker-smoke",
        "--skip-opencode-smoke",
        dest="skip_coding_worker_smoke",
        action="store_true",
        help="skip coding-worker smoke",
    )
    parser.add_argument("--install-live", action="store_true", help="install this repo-managed script to $HERMES_HOME/scripts")
    parser.add_argument("--dry-run", action="store_true", help="show install-live actions without changing files")
    args = parser.parse_args()

    if args.install_live:
        return install_live(dry_run=args.dry_run)

    issues: list[dict[str, str]] = []
    facts: dict[str, Any] = {
        "checked_at": dt.datetime.now(dt.UTC).isoformat(),
        "repo": str(REPO),
        "hermes_home": str(HERMES_HOME),
        **script_provenance(),
    }

    check_hermes_doctor(issues, facts)
    check_auth_list(issues, facts)
    check_codex_auth_incidents(issues, facts)
    check_main_inference(issues, facts)
    check_compression_inference(issues, facts)
    check_honcho(issues, facts)
    if not args.skip_coding_worker_smoke:
        check_coding_worker(issues, facts)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"issues": issues, "facts": facts}, indent=2, sort_keys=True) + "\n")

    if issues:
        _print_issue_report(issues)
        # In no_agent cron mode, non-empty stdout is the alert. Exit 0 so the
        # scheduler does not misclassify a detected system problem as a broken
        # watchdog script.
        return 0

    if args.status:
        checked = "hermes doctor/auth/main inference/compression/honcho"
        if not args.skip_coding_worker_smoke:
            checked += "/codex-worker"
        print(
            f"OK Nightly Hermes system doctor: {checked} checks passed; "
            f"checked_at={facts['checked_at']}; "
            f"source={facts['script_entrypoint_source']}; "
            f"live_sha256={facts['script_live_sha256'] or 'missing'}; "
            f"source_sha256={facts['script_source_sha256'] or 'missing'}; "
            f"matches_source={facts['script_matches_source']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
import socket
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
QMD_EXPECTED_TARGETS = [
    {"name": "qmd-8181", "host": "127.0.0.1", "port": 8181},
    {"name": "qmd-8182", "host": "127.0.0.1", "port": 8182},
]
QMD_ALLOWED_STATUSES = {"healthy", "process_missing", "port_closed", "wrong_bind", "unknown"}
QMD_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def coding_worker_backend() -> str:
    code = "from agent.opencode_worker import load_coding_worker_backend; print(load_coding_worker_backend())"
    r = run([str(PYTHON if PYTHON.exists() else sys.executable), "-c", code], timeout=60, cwd=REPO)
    return (r["output"] or "").strip() if r["exit"] == 0 else "unknown"


def clean(text: str, limit: int = 4000) -> str:
    text = ANSI_RE.sub("", text or "")
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;}\]\)\"']+", r"\1=<redacted>", text)
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


def collect_qmd_process_commands() -> list[str]:
    """Return local QMD-ish process commands without exposing environment values."""
    try:
        p = subprocess.run(
            ["ps", "-ww", "-eo", "pid=,args="],
            cwd="/",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:  # noqa: BLE001 - readiness evidence is best-effort
        return []
    if p.returncode != 0:
        return []
    commands: list[str] = []
    current_pid = str(os.getpid())
    for line in p.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(maxsplit=1)
        if len(parts) != 2 or parts[0] == current_pid:
            continue
        command = parts[1]
        lower = command.lower()
        if "qmd" in lower and "nightly_hermes_system_doctor" not in lower:
            commands.append(clean(command, 2000))
    return commands[:25]


def qmd_tcp_ready(host: str, port: int, timeout: float = 0.5) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def qmd_loopback_ready(
    host: str,
    port: int,
    connector: Any = qmd_tcp_ready,
) -> tuple[bool, str, str | None, dict[str, str]]:
    candidates = list(dict.fromkeys([host, *QMD_LOOPBACK_HOSTS]))
    errors: dict[str, str] = {}
    for candidate in candidates:
        ready, error = connector(candidate, port)
        if ready:
            return True, candidate, None, errors
        errors[candidate] = clean(error or "closed", 200)
    return False, candidates[0], errors.get(candidates[0], "closed"), errors


def _qmd_command_matches_target(command: str, port: int) -> bool:
    lower = command.lower()
    return "qmd" in lower and re.search(rf"(?<!\d){port}(?!\d)", command) is not None


def _qmd_command_has_nonlocal_bind(command: str) -> bool:
    lower = command.lower()
    loopback_hosts = ("127.0.0.1", "localhost", "::1")
    return ("--host" in lower or "--bind" in lower) and not any(item in lower for item in loopback_hosts)


def classify_qmd_target(
    target: dict[str, Any],
    process_commands: list[str],
    connector: Any = qmd_tcp_ready,
) -> dict[str, Any]:
    host = str(target["host"])
    port = int(target["port"])
    matching_commands = [cmd for cmd in process_commands if _qmd_command_matches_target(cmd, port)]
    any_qmd_process = any("qmd" in cmd.lower() for cmd in process_commands)
    port_ready, reachable_host, port_error, loopback_errors = qmd_loopback_ready(host, port, connector)

    if matching_commands and port_ready:
        status = "healthy"
    elif not matching_commands:
        status = "process_missing"
    elif _qmd_command_has_nonlocal_bind("\n".join(matching_commands)):
        status = "wrong_bind"
    elif port_error:
        status = "port_closed"
    else:
        status = "unknown"

    result = {
        "name": str(target["name"]),
        "host": host,
        "port": port,
        "status": status,
        "process_present": bool(matching_commands),
        "qmd_process_seen": any_qmd_process,
        "port_ready": port_ready,
        "reachable_host": reachable_host if port_ready else "",
        "port_error": clean(port_error or "", 300),
        "loopback_errors": loopback_errors,
        "process_excerpt": clean(matching_commands[0], 300) if matching_commands else "",
    }
    if result["status"] not in QMD_ALLOWED_STATUSES:
        result["status"] = "unknown"
    return result


def check_qmd_health(
    issues: list[dict[str, str]],
    facts: dict[str, Any],
    *,
    process_collector: Any = collect_qmd_process_commands,
    connector: Any = qmd_tcp_ready,
) -> list[dict[str, Any]]:
    process_commands = process_collector()
    results = [classify_qmd_target(target, process_commands, connector) for target in QMD_EXPECTED_TARGETS]
    facts["qmd_expected_target_source"] = "nightly doctor defaults: local ports 8181 and 8182"
    facts["qmd_health"] = results
    for item in results:
        if item["status"] == "healthy":
            continue
        detail = (
            f"{item['name']} expected {item['host']}:{item['port']} status={item['status']} "
            f"process_present={item['process_present']} port_ready={item['port_ready']}. "
            "Inspect QMD/Honcho service logs and the Honcho operator runbook; do not infer QMD health from process presence alone."
        )
        if item.get("port_error"):
            detail += f" port_error={item['port_error']}"
        if item.get("process_excerpt"):
            detail += f" process_excerpt={item['process_excerpt']}"
        add_issue(issues, "critical", f"QMD service readiness failed: {item['name']} {item['status']}", detail)
    return results


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


def sanitize_compression_route_item(item: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, str):
            sanitized[key] = clean(value, 1200)
        elif isinstance(value, dict):
            sanitized[key] = sanitize_compression_route_item(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_compression_route_item(v) if isinstance(v, dict) else clean(v, 1200) if isinstance(v, str) else v
                for v in value
            ]
        else:
            sanitized[key] = value
    return sanitized


def compression_route_failure_class(item: dict[str, Any]) -> str:
    credential_status = item.get("credential_status")
    if credential_status == "missing":
        return "missing_credential"
    if credential_status == "exhausted":
        return "credential_rate_limited_or_quarantined"
    if credential_status == "dead":
        return "credential_terminal_auth_failure"
    if credential_status in {"unavailable", "unknown_provider"}:
        return "credential_status_unavailable"
    return "route_failed"


def compression_route_next_action(item: dict[str, Any]) -> str:
    failure_class = item.get("failure_class") or compression_route_failure_class(item)
    if failure_class == "missing_credential":
        return f"Add or configure credentials for {item.get('provider') or 'the provider'}."
    if failure_class == "credential_rate_limited_or_quarantined":
        reset_at = item.get("credential_last_error_reset_at")
        if reset_at:
            return f"Wait for credential cooldown to expire at {reset_at}, then rerun the doctor."
        seconds = item.get("credential_cooldown_seconds_remaining")
        if seconds is not None:
            return f"Wait about {seconds} seconds for credential cooldown to expire, then rerun the doctor."
        return "Wait for credential cooldown/quarantine to expire, then rerun the doctor."
    if failure_class == "credential_terminal_auth_failure":
        return f"Re-authenticate or refresh credentials for {item.get('provider') or 'the provider'}."
    return "Inspect sanitized route error; if this route should remain primary, repair it before relying on fallback."


def annotate_compression_route_results(route_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fallback_results = [item for item in route_results if str(item.get("label") or "").startswith("fallback_chain[")]
    fallback_success = any(item.get("exit") == 0 for item in fallback_results)
    fallback_failed = bool(fallback_results) and not fallback_success
    fallback_status = "healthy" if fallback_success else "failed" if fallback_failed else "not_configured"
    annotated: list[dict[str, Any]] = []
    for item in route_results:
        annotated_item = dict(item)
        annotated_item.setdefault("fallback_status", fallback_status)
        annotated_item.setdefault("fallback_chain_healthy", fallback_success)
        if annotated_item.get("exit") == 0:
            annotated_item.setdefault("failure_class", "healthy")
            annotated_item.setdefault("impact", "primary route healthy" if annotated_item.get("label") == "primary" else "fallback route healthy")
            annotated.append(annotated_item)
            continue
        failure_class = compression_route_failure_class(annotated_item)
        annotated_item["failure_class"] = failure_class
        if fallback_success:
            annotated_item["impact"] = "configured primary route degraded; fallback chain is healthy"
        else:
            annotated_item["impact"] = "compression route unavailable; fallback chain is not healthy"
        annotated_item["next_action"] = compression_route_next_action(annotated_item)
        annotated.append(annotated_item)
    return annotated


def record_compression_route_results(
    routes: dict[str, Any],
    issues: list[dict[str, str]],
    facts: dict[str, Any],
) -> list[dict[str, Any]]:
    output = str(routes.get("output") or "")
    facts["compression_routes_exit"] = routes.get("exit")
    facts["compression_routes_output"] = output
    route_results = annotate_compression_route_results([
        sanitize_compression_route_item(item) for item in extract_compression_routes(output)
    ])
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


def configured_inference_providers(config: dict[str, Any]) -> set[str]:
    """Return providers that the live main/auxiliary routes explicitly require."""
    providers: set[str] = set()

    def add_route(route: Any) -> None:
        if not isinstance(route, dict):
            return
        provider = str(route.get("provider") or "").strip().lower()
        if provider and provider != "auto":
            providers.add(provider)
        for fallback in route.get("fallback_chain") or []:
            add_route(fallback)

    add_route(config.get("model"))
    for route in (config.get("auxiliary") or {}).values():
        add_route(route)
    fallback_model = config.get("fallback_model")
    if isinstance(fallback_model, list):
        for route in fallback_model:
            add_route(route)
    else:
        add_route(fallback_model)
    return providers


def load_configured_inference_providers() -> set[str]:
    try:
        from hermes_cli.config import load_config_readonly
    except ImportError:
        from hermes_cli.config import load_config as load_config_readonly

    return configured_inference_providers(load_config_readonly())


def check_auth_list(
    issues: list[dict[str, str]],
    facts: dict[str, Any],
    expected_providers: set[str] | None = None,
) -> None:
    r = run([str(HERMES), "auth", "list"], timeout=120)
    facts["hermes_auth_list_exit"] = r["exit"]
    facts["hermes_auth_list"] = r["output"]
    facts["configured_inference_providers"] = sorted(expected_providers or [])
    if r["exit"] != 0:
        add_issue(issues, "critical", "hermes auth list failed", r["output"])
    bad = []
    current_provider = ""
    for line in r["output"].splitlines():
        header = re.match(r"^([^\s(]+)\s+\(\d+\s+credentials?\):\s*$", line.strip())
        if header:
            current_provider = header.group(1).lower()
            continue
        if not re.search(r"(?i)auth failed|expired|exhausted|invalid|401|403", line):
            continue
        if expected_providers is None or current_provider in expected_providers:
            bad.append(line.strip())
    if bad:
        add_issue(issues, "critical", "configured provider credential pool has unhealthy credentials", "\n".join(bad))


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


def check_xai_plugin_imports(issues: list[dict[str, str]], facts: dict[str, Any]) -> None:
    """Verify xAI optional plugin imports without resolving credentials or calling xAI."""
    code = r'''
import importlib
import json

modules = [
    "tools.xai_http",
    "plugins.image_gen.xai",
    "plugins.video_gen.xai",
    "plugins.model-providers.xai",
]
result = {}
for name in modules:
    mod = importlib.import_module(name)
    result[name] = getattr(mod, "__file__", None)

from tools import xai_http

helper = getattr(xai_http, "build_xai_storage_options", None)
result["has_build_xai_storage_options"] = callable(helper)
result["helper_module_file"] = getattr(xai_http, "__file__", None)
print(json.dumps(result, sort_keys=True))
'''
    r = python_smoke(code, timeout=60)
    facts["xai_plugin_imports_exit"] = r["exit"]
    facts["xai_plugin_imports_output"] = r["output"]
    if r["exit"] != 0 or '"has_build_xai_storage_options": true' not in r["output"]:
        add_issue(issues, "critical", "xAI optional plugin import smoke failed", r["output"])


def check_main_inference(issues: list[dict[str, str]], facts: dict[str, Any]) -> None:
    code = r'''
import json
from agent.auxiliary_client import call_llm
try:
    from hermes_cli.config import load_config_readonly
except ImportError:
    from hermes_cli.config import load_config as load_config_readonly

config = load_config_readonly()
main = config.get("model") or {}
provider = str(main.get("provider") or "").strip()
model = str(main.get("default") or main.get("name") or main.get("model") or "").strip()
api_mode = str(main.get("api_mode") or "").strip() or None
if not provider or not model:
    raise RuntimeError("model.provider and model.default/name must be configured")
resp = call_llm(
    provider=provider,
    model=model,
    api_mode=api_mode,
    messages=[{"role":"user","content":"Reply with exactly: HERMES_MAIN_SMOKE_OK"}],
    max_tokens=12,
    timeout=90,
    strict_provider=True,
)
text = (resp.choices[0].message.content or "").strip()
print(json.dumps({
    "provider": provider,
    "model": model,
    "response_model": getattr(resp, "model", None),
    "text": text,
}, sort_keys=True))
'''
    r = python_smoke(code, timeout=150)
    facts["main_inference_exit"] = r["exit"]
    facts["main_inference_output"] = r["output"]
    if r["exit"] != 0 or "HERMES_MAIN_SMOKE_OK" not in r["output"]:
        add_issue(issues, "critical", "configured main inference smoke failed", r["output"])


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
    from agent.credential_pool import describe_provider_credential_availability
except Exception:  # noqa: BLE001 - keep route smoke usable if helper import fails
    describe_provider_credential_availability = None

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

def credential_diagnostics(provider):
    if describe_provider_credential_availability is None:
        return {"credential_status": "unavailable"}
    diag = describe_provider_credential_availability(provider)
    first_unavailable = (diag.get("unavailable_entries") or [{}])[0]
    result = {
        "credential_status": diag.get("credential_status"),
        "credential_has_credentials": diag.get("has_credentials"),
        "credential_has_available": diag.get("has_available"),
    }
    for key in ("last_error_code", "last_error_reason", "last_error_message", "last_error_reset_at", "cooldown_seconds_remaining"):
        if first_unavailable.get(key) is not None:
            result[f"credential_{key}"] = first_unavailable.get(key)
    return result

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
    result.update(credential_diagnostics(route.get("provider")))
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
    check_qmd_health(issues, facts)

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
    expected_providers = load_configured_inference_providers()
    check_auth_list(issues, facts, expected_providers)
    if "openai-codex" in expected_providers:
        check_codex_auth_incidents(issues, facts)
    else:
        facts["codex_auth_evidence_count"] = 0
        facts["codex_auth_incident"] = None
        facts["codex_auth_check"] = "skipped; openai-codex is not a configured inference route"
    check_xai_plugin_imports(issues, facts)
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
        checked = "hermes doctor/auth/main inference/compression/honcho/qmd"
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

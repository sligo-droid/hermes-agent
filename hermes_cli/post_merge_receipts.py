"""Bounded exact-SHA post-merge receipt collection.

Collectors are deterministic and storage-neutral. They return one gathered
receipt update; caller-owned persistence remains atomic and happens only after
all independent collectors finish.
"""

from __future__ import annotations

import copy
import datetime as dt
import inspect
import json
import multiprocessing
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from agent.runtime_spans import RuntimeSpanRecorder
from hermes_cli.github_remote import github_cli_env


_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_RECEIPT_NAMES = ("canonical_sync", "ci", "deployment", "production_qa", "restart")
_TERMINAL_STATUSES = frozenset({"passed", "failed", "blocked", "not_configured"})
_MAX_WORKERS = 5

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
CanonicalSync = Callable[..., Any]


@dataclass
class PostMergeControl:
    """Cooperative collector deadline shared with mutation-capable workers."""

    deadline: float
    _cancel_event: Any = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._cancel_event.set()

    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def mutation_allowed(self) -> bool:
        """Return false immediately once collection no longer owns mutations."""

        return not self.cancelled() and not self.expired()


class PostMergeAdapter(Protocol):
    """Trusted repository adapter that independently observes an exact SHA."""

    def __call__(
        self,
        *,
        target_sha: str,
        repository: str,
        workspace_path: str,
        canonical_path: str,
        timeout_s: float,
        control: PostMergeControl,
    ) -> Mapping[str, Any]: ...


_DEPLOYMENT_ADAPTERS: dict[str, PostMergeAdapter] = {}
_PRODUCTION_QA_ADAPTERS: dict[str, PostMergeAdapter] = {}
_RESTART_ADAPTERS: dict[str, PostMergeAdapter] = {}
_ADAPTER_DISCOVERY_LOCK = threading.Lock()
_ADAPTER_DISCOVERY_ATTEMPTED = False


def _discover_registered_adapters() -> None:
    """Load trusted local plugins before resolving repository adapter names."""

    global _ADAPTER_DISCOVERY_ATTEMPTED
    with _ADAPTER_DISCOVERY_LOCK:
        if _ADAPTER_DISCOVERY_ATTEMPTED:
            return
        try:
            from hermes_cli.plugins import discover_plugins

            discover_plugins()
        except Exception:
            # Required adapters still fail closed as missing. Discovery failures
            # are transient: leave the completion marker clear so a later caller
            # can retry while the lock keeps concurrent callers serialized.
            return
        _ADAPTER_DISCOVERY_ATTEMPTED = True


def _register(registry: dict[str, PostMergeAdapter], name: str, adapter: PostMergeAdapter) -> None:
    key = re.sub(r"[^a-z0-9_.-]", "", str(name or "").strip().lower())[:80]
    if not key or not callable(adapter):
        raise ValueError("post-merge adapter requires a bounded name and callable")
    registry[key] = adapter


def register_deployment_adapter(name: str, adapter: PostMergeAdapter) -> None:
    _register(_DEPLOYMENT_ADAPTERS, name, adapter)


def register_production_qa_adapter(name: str, adapter: PostMergeAdapter) -> None:
    _register(_PRODUCTION_QA_ADAPTERS, name, adapter)


def register_restart_adapter(name: str, adapter: PostMergeAdapter) -> None:
    _register(_RESTART_ADAPTERS, name, adapter)


def _default_run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int | float = 60,
    github: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=github_cli_env() if github else None,
    )


def _default_sync(
    canonical_path: str,
    branch: str,
    merge_sha: str,
    *,
    control: PostMergeControl | None = None,
) -> Any:
    from hermes_cli.canonical_checkout_sync import sync_protected_canonical_checkout

    return sync_protected_canonical_checkout(
        canonical_path,
        branch,
        merge_sha,
        control=control,
    )


def gateway_restart_adapter(
    *,
    target_sha: str,
    repository: str,
    workspace_path: str,
    canonical_path: str,
    timeout_s: float,
    control: PostMergeControl | None = None,
    run: CommandRunner | None = None,
    read_status: Callable[[], Mapping[str, Any] | None] | None = None,
    signal_process: Callable[[int, int], Any] | None = None,
    get_running_pid: Callable[[], int | None] | None = None,
    get_process_start_time: Callable[[int], int | None] | None = None,
    now_utc: Callable[[], dt.datetime] | None = None,
    runtime_max_age_s: float = 900.0,
    prior_receipt: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Two-phase self-restart adapter bound to trusted live runtime identity."""

    target = _safe_sha(target_sha)
    if not target:
        return {"status": "blocked", "diagnostic_code": "restart_target_invalid"}
    try:
        budget = max(0.1, min(float(timeout_s), 60.0))
    except (TypeError, ValueError):
        budget = 30.0
    local_deadline = time.monotonic() + budget
    operation_control = control or PostMergeControl(deadline=local_deadline)
    root_text = str(canonical_path or workspace_path or "").strip()
    root = Path(root_text).expanduser().resolve(strict=False) if root_text else Path()
    if not root_text or not root.is_dir():
        return {"status": "blocked", "diagnostic_code": "restart_source_missing"}

    execute = run or _default_run

    def remaining() -> float:
        return max(0.1, min(local_deadline - time.monotonic(), operation_control.remaining()))

    try:
        head = execute(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            timeout=min(10.0, remaining()),
            github=False,
        )
        dirty = execute(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            timeout=min(10.0, remaining()),
            github=False,
        )
    except Exception:
        return {"status": "blocked", "diagnostic_code": "restart_source_probe_failed"}
    desired_head = _safe_sha(head.stdout if head.returncode == 0 else "")
    if desired_head != target:
        return {"status": "blocked", "diagnostic_code": "restart_source_sha_mismatch"}
    if dirty.returncode != 0 or bool(str(dirty.stdout or "").strip()):
        return {"status": "blocked", "diagnostic_code": "restart_source_dirty"}

    if read_status is None or get_running_pid is None or get_process_start_time is None:
        try:
            from gateway.status import (
                get_process_start_time as trusted_process_start_time,
                get_running_pid as trusted_running_pid,
                read_runtime_status,
            )

            read_status = read_status or read_runtime_status
            get_running_pid = get_running_pid or trusted_running_pid
            get_process_start_time = get_process_start_time or trusted_process_start_time
        except Exception:
            read_status = read_status or (lambda: None)
            get_running_pid = get_running_pid or (lambda: None)
            get_process_start_time = get_process_start_time or (lambda _pid: None)

    try:
        freshness_limit = max(1.0, min(float(runtime_max_age_s), 3600.0))
    except (TypeError, ValueError):
        freshness_limit = 900.0

    def prove_runtime(
        value: Mapping[str, Any] | None,
        *,
        expected_commit: str,
        previous_pid: int | None = None,
        previous_start_time: int | None = None,
        allow_restart_requested: bool = False,
    ) -> tuple[bool, str, int, int]:
        if not isinstance(value, Mapping):
            return False, "restart_runtime_missing", 0, 0
        runtime = dict(value)
        try:
            from hermes_cli.gateway import _gateway_replacement_proof, _parse_runtime_updated_at

            updated_at = _parse_runtime_updated_at(runtime.get("updated_at"))
        except Exception:
            return False, "restart_runtime_stale", 0, 0
        current_time = now_utc() if now_utc is not None else dt.datetime.now(dt.timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=dt.timezone.utc)
        else:
            current_time = current_time.astimezone(dt.timezone.utc)
        age = (current_time - updated_at).total_seconds() if updated_at is not None else None
        if age is None or age < 0 or age > freshness_limit:
            return False, "restart_runtime_stale", 0, 0
        try:
            runtime_pid = int(runtime.get("pid") or 0)
            runtime_start = int(runtime.get("start_time"))
        except (TypeError, ValueError):
            return False, "restart_runtime_process_identity_missing", 0, 0
        manager_pid = get_running_pid()
        if manager_pid != runtime_pid:
            return False, "restart_runtime_pid_mismatch", runtime_pid, runtime_start
        actual_start = get_process_start_time(runtime_pid)
        if actual_start is None or actual_start != runtime_start:
            return False, "restart_runtime_start_time_mismatch", runtime_pid, runtime_start
        proof_state = dict(runtime)
        if allow_restart_requested:
            proof_state["restart_requested"] = False
        proven, proof_code = _gateway_replacement_proof(
            proof_state,
            manager_pid=manager_pid,
            previous_pid=previous_pid,
            previous_start_time=previous_start_time,
            actual_start_time=actual_start,
            expected_source_commit=expected_commit,
        )
        if not proven:
            aliases = {
                "gateway_not_running": "restart_runtime_not_running",
                "runtime_identity_inconsistent": "restart_runtime_identity_invalid",
                "runtime_pid_mismatch": "restart_runtime_pid_mismatch",
                "runtime_process_identity_missing": "restart_runtime_process_identity_missing",
                "runtime_start_time_mismatch": "restart_runtime_start_time_mismatch",
            }
            return False, aliases.get(proof_code, f"restart_{proof_code}"), runtime_pid, runtime_start
        return True, "passed", runtime_pid, runtime_start

    prior = prior_receipt if isinstance(prior_receipt, Mapping) else {}
    try:
        baseline_pid = int(prior.get("baseline_pid"))
        baseline_start = int(prior.get("baseline_start_time"))
        if baseline_pid <= 0 or baseline_start < 0:
            raise ValueError
    except (TypeError, ValueError):
        baseline_pid = None
        baseline_start = None

    runtime = read_status() or {}
    observed = _safe_sha(runtime.get("source_commit")) if isinstance(runtime, Mapping) else ""
    if not observed:
        return {"status": "blocked", "diagnostic_code": "restart_source_identity_invalid"}
    restart_requested = runtime.get("restart_requested") is True if isinstance(runtime, Mapping) else False

    if baseline_pid is not None and baseline_start is not None:
        proven, proof_code, _pid, _start = prove_runtime(
            runtime,
            expected_commit=target,
            previous_pid=baseline_pid,
            previous_start_time=baseline_start,
        )
        if proven:
            return {
                "status": "passed",
                "observed_sha": target,
                "baseline_pid": baseline_pid,
                "baseline_start_time": baseline_start,
            }
        # Once a signal has been issued, unchanged identity, startup readiness,
        # and source propagation are retryable observations. Never signal an
        # unproven or replacement process a second time.
        return {
            "status": "pending",
            "diagnostic_code": "gateway_restart_replacement_not_observed",
            "baseline_pid": baseline_pid,
            "baseline_start_time": baseline_start,
        }

    proven, proof_code, pid, start_time = prove_runtime(
        runtime,
        expected_commit=observed,
        allow_restart_requested=restart_requested,
    )
    if not proven:
        return {"status": "blocked", "diagnostic_code": proof_code}
    if restart_requested:
        return {"status": "pending", "diagnostic_code": "gateway_restart_in_progress"}

    # Re-read and re-prove the exact live (pid, start_time) immediately before
    # signalling so a stale status file or PID reuse can never redirect SIGUSR1.
    latest_runtime = read_status() or {}
    latest_observed = (
        _safe_sha(latest_runtime.get("source_commit"))
        if isinstance(latest_runtime, Mapping)
        else ""
    )
    if not latest_observed:
        return {"status": "blocked", "diagnostic_code": "restart_source_identity_invalid"}
    latest_proven, latest_code, latest_pid, latest_start = prove_runtime(
        latest_runtime,
        expected_commit=latest_observed,
    )
    if not latest_proven:
        return {"status": "blocked", "diagnostic_code": latest_code}
    if (latest_pid, latest_start) != (pid, start_time):
        return {"status": "blocked", "diagnostic_code": "restart_runtime_identity_changed"}
    if not operation_control.mutation_allowed():
        return {"status": "blocked", "diagnostic_code": "collector_cancelled"}
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return {"status": "blocked", "diagnostic_code": "gateway_restart_signal_unavailable"}
    send_signal = signal_process or os.kill
    try:
        send_signal(latest_pid, sigusr1)
    except Exception:
        return {"status": "blocked", "diagnostic_code": "gateway_restart_request_failed"}
    return {
        "status": "pending",
        "diagnostic_code": "gateway_restart_requested",
        "baseline_pid": latest_pid,
        "baseline_start_time": latest_start,
    }


def _safe_sha(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _SHA_RE.fullmatch(text) else ""


register_restart_adapter("gateway-self", gateway_restart_adapter)


def _safe_code(value: Any, default: str) -> str:
    code = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(value or default).strip())[:80]
    return code or default


def _receipt(
    status: str,
    *,
    now: float,
    target_sha: str = "",
    diagnostic_code: str = "",
    baseline_pid: int | None = None,
    baseline_start_time: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "checked_at": now}
    if target_sha:
        result["observed_sha"] = target_sha
    if diagnostic_code:
        result["diagnostic_code"] = _safe_code(diagnostic_code, status)
    if baseline_pid is not None and baseline_pid > 0:
        result["baseline_pid"] = baseline_pid
    if baseline_start_time is not None and baseline_start_time >= 0:
        result["baseline_start_time"] = baseline_start_time
    return result


def initialize_post_merge_receipts(
    state: Mapping[str, Any],
    *,
    target_sha: str,
) -> dict[str, Any]:
    """Initialize all exact-SHA receipt slots together before collection."""

    target = _safe_sha(target_sha)
    if not target:
        raise ValueError("post-merge target must be an exact full SHA")
    policy = state.get("policy") if isinstance(state.get("policy"), Mapping) else {}
    requirements = (
        policy.get("post_merge_requirements")
        if isinstance(policy.get("post_merge_requirements"), Mapping)
        else {}
    )
    workspace = state.get("workspace") if isinstance(state.get("workspace"), Mapping) else {}
    initialized: dict[str, Any] = {"target_sha": target}
    for name in _RECEIPT_NAMES:
        configured = requirements.get(name) is True
        if name == "canonical_sync" and str(workspace.get("canonical_path") or "").strip():
            configured = True
        initialized[name] = {"status": "pending" if configured else "not_configured"}
    return initialized


def _sort_key(item: Mapping[str, Any], index: int) -> tuple[str, int, int, int]:
    timestamp = str(item.get("updatedAt") or item.get("createdAt") or "")
    values: list[int] = []
    for name in ("runAttempt", "runNumber", "databaseId"):
        try:
            values.append(int(item.get(name) or 0))
        except (TypeError, ValueError):
            values.append(0)
    return (timestamp, values[0], values[1], values[2] or index)


def _collect_ci(
    *,
    target_sha: str,
    repository: str,
    workspace_path: str,
    run: CommandRunner,
    control: PostMergeControl,
    now: float,
) -> dict[str, Any]:
    root = Path(workspace_path).expanduser().resolve(strict=False)
    if control.expired():
        return _receipt("blocked", now=now, diagnostic_code="collector_timeout")
    result = run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repository,
            "--workflow",
            "tests.yml",
            "--event",
            "push",
            "--commit",
            target_sha,
            "--limit",
            "50",
            "--json",
            "databaseId,headSha,event,status,conclusion,workflowName,createdAt,updatedAt",
        ],
        cwd=root,
        timeout=max(0.1, min(60.0, control.remaining())),
        github=True,
    )
    if result.returncode != 0:
        return _receipt("blocked", now=now, diagnostic_code="post_merge_ci_query_failed")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return _receipt("blocked", now=now, diagnostic_code="post_merge_ci_invalid_json")
    candidates: list[tuple[tuple[str, int, int, int], Mapping[str, Any]]] = []
    if isinstance(payload, list):
        for index, raw in enumerate(payload[:100]):
            if not isinstance(raw, Mapping):
                continue
            if _safe_sha(raw.get("headSha")) != target_sha:
                continue
            if str(raw.get("event") or "").strip().lower() != "push":
                continue
            if str(raw.get("workflowName") or "").strip() != "Basic Tests":
                continue
            candidates.append((_sort_key(raw, index), raw))
    if not candidates:
        return _receipt("pending", now=now, diagnostic_code="post_merge_ci_not_found")
    latest = max(candidates, key=lambda item: item[0])[1]
    status = str(latest.get("status") or "").strip().upper()
    conclusion = str(latest.get("conclusion") or "").strip().upper()
    if status != "COMPLETED":
        return _receipt("pending", now=now, diagnostic_code="post_merge_ci_running")
    if conclusion != "SUCCESS":
        return _receipt("failed", now=now, target_sha=target_sha, diagnostic_code="post_merge_ci_failed")
    run_id = str(latest.get("databaseId") or "").strip()
    if not run_id.isdigit():
        return _receipt("blocked", now=now, diagnostic_code="post_merge_ci_run_id_missing")
    if control.expired():
        return _receipt("blocked", now=now, diagnostic_code="collector_timeout")
    jobs_result = run(
        ["gh", "run", "view", run_id, "--repo", repository, "--json", "jobs"],
        cwd=root,
        timeout=max(0.1, min(60.0, control.remaining())),
        github=True,
    )
    if jobs_result.returncode != 0:
        return _receipt("blocked", now=now, diagnostic_code="post_merge_ci_jobs_query_failed")
    try:
        jobs_payload = json.loads(jobs_result.stdout or "{}")
    except json.JSONDecodeError:
        return _receipt("blocked", now=now, diagnostic_code="post_merge_ci_jobs_invalid_json")
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, Mapping) else None
    basic_jobs = [
        item
        for item in (jobs if isinstance(jobs, list) else [])
        if isinstance(item, Mapping) and str(item.get("name") or "").strip() == "basic"
    ]
    if not basic_jobs:
        return _receipt("pending", now=now, diagnostic_code="post_merge_basic_job_missing")
    job = basic_jobs[-1]
    job_status = str(job.get("status") or "").strip().upper()
    job_conclusion = str(job.get("conclusion") or "").strip().upper()
    if job_status != "COMPLETED":
        return _receipt("pending", now=now, diagnostic_code="post_merge_basic_job_running")
    if job_conclusion != "SUCCESS":
        return _receipt("failed", now=now, target_sha=target_sha, diagnostic_code="post_merge_basic_job_failed")
    return _receipt("passed", now=now, target_sha=target_sha)


def _collect_canonical(
    *,
    target_sha: str,
    canonical_path: str,
    branch: str,
    required: bool,
    enforce: bool,
    sync_canonical: CanonicalSync,
    control: PostMergeControl,
    now: float,
) -> dict[str, Any]:
    if not canonical_path:
        status = "failed" if required and enforce else "not_configured"
        return _receipt(status, now=now, diagnostic_code="canonical_path_missing" if status == "failed" else "")
    if not control.mutation_allowed():
        return _receipt("blocked", now=now, diagnostic_code="collector_cancelled")
    try:
        parameters = inspect.signature(sync_canonical).parameters
        accepts_control = "control" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if accepts_control:
            result = sync_canonical(
                canonical_path,
                branch,
                target_sha,
                control=control,
            )
        else:
            result = sync_canonical(canonical_path, branch, target_sha)
        data = result.as_dict() if hasattr(result, "as_dict") else dict(result) if isinstance(result, Mapping) else {}
    except Exception:
        return _receipt("failed", now=now, diagnostic_code="canonical_sync_failed")
    sync_state = str(data.get("state") or "").strip().lower()
    if sync_state == "not_applicable":
        return _receipt("not_configured", now=now, diagnostic_code="canonical_not_applicable")
    if not sync_state.startswith("synced"):
        return _receipt("failed", now=now, diagnostic_code="canonical_sync_failed")
    observed_head = _safe_sha(data.get("head"))
    if not observed_head:
        return _receipt("failed", now=now, diagnostic_code="canonical_head_missing")
    if observed_head != target_sha:
        return _receipt("failed", now=now, diagnostic_code="canonical_head_mismatch")
    observed_merge = _safe_sha(data.get("merge_commit"))
    if observed_merge != target_sha:
        return _receipt("failed", now=now, diagnostic_code="canonical_merge_target_mismatch")
    return _receipt("passed", now=now, target_sha=observed_head)


def _adapter_receipt(
    *,
    registry: Mapping[str, PostMergeAdapter],
    adapter_name: str,
    required: bool,
    enforce: bool,
    target_sha: str,
    repository: str,
    workspace_path: str,
    canonical_path: str,
    timeout_s: float,
    control: PostMergeControl,
    now: float,
    prior_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    adapter = registry.get(adapter_name.lower()) if adapter_name else None
    if adapter is None:
        status = "failed" if required and enforce else "not_configured"
        code = "required_adapter_missing" if status == "failed" else ""
        return _receipt(status, now=now, diagnostic_code=code)
    if not control.mutation_allowed():
        return _receipt("blocked", now=now, diagnostic_code="collector_cancelled")
    try:
        adapter_kwargs: dict[str, Any] = {
            "target_sha": target_sha,
            "repository": repository,
            "workspace_path": workspace_path,
            "canonical_path": canonical_path,
            "timeout_s": min(timeout_s, max(0.1, control.remaining())),
            "control": control,
        }
        parameters = inspect.signature(adapter).parameters
        if "prior_receipt" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            adapter_kwargs["prior_receipt"] = dict(prior_receipt or {})
        raw = adapter(**adapter_kwargs)
    except Exception:
        return _receipt("failed", now=now, diagnostic_code="adapter_failed")
    data = raw if isinstance(raw, Mapping) else {}
    status = str(data.get("status") or "blocked").strip().lower()
    if status not in _TERMINAL_STATUSES and status != "pending":
        status = "blocked"
    observed = _safe_sha(data.get("observed_sha"))
    code = _safe_code(data.get("diagnostic_code"), "adapter_result") if data.get("diagnostic_code") else ""
    if status == "passed" and observed != target_sha:
        return _receipt("failed", now=now, diagnostic_code="observed_sha_mismatch")
    try:
        baseline_pid = int(data.get("baseline_pid"))
        baseline_start = int(data.get("baseline_start_time"))
        if baseline_pid <= 0 or baseline_start < 0:
            raise ValueError
    except (TypeError, ValueError):
        baseline_pid = None
        baseline_start = None
    return _receipt(
        status,
        now=now,
        target_sha=observed,
        diagnostic_code=code,
        baseline_pid=baseline_pid,
        baseline_start_time=baseline_start,
    )


def _repository_config(config: Mapping[str, Any], repository: str) -> Mapping[str, Any]:
    repositories = config.get("repositories") if isinstance(config.get("repositories"), Mapping) else {}
    entry = repositories.get(repository) if isinstance(repositories, Mapping) else None
    return entry if isinstance(entry, Mapping) else {}


def _isolated_collector_entry(
    collector: Callable[[PostMergeControl], dict[str, Any]],
    control: PostMergeControl,
    connection: Any,
) -> None:
    """Run one collector in a process group the parent can terminate atomically."""

    try:
        if hasattr(os, "setsid"):
            os.setsid()
        connection.send(("result", collector(control)))
    except BaseException:
        try:
            connection.send(("error", None))
        except BaseException:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _terminate_isolated_collector(process: Any, *, deadline: float) -> None:
    """Kill and reap a timed-out collector and descendants before returning."""

    if not process.is_alive():
        process.join(timeout=0)
        return
    try:
        if hasattr(os, "killpg") and process.pid:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except Exception:
            pass
    process.join(timeout=max(0.0, deadline - time.monotonic()))
    if process.is_alive():
        # Do not let a timeout receipt escape while mutation-capable code lives.
        try:
            process.kill()
        except Exception:
            pass
        process.join()


def collect_post_merge_receipts(
    state: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    run: CommandRunner | None = None,
    sync_canonical: CanonicalSync | None = None,
    now: float | None = None,
    max_workers: int = _MAX_WORKERS,
    read_only: bool = False,
    mutation_allowed: Callable[[], bool] | None = None,
    span_recorder: RuntimeSpanRecorder | None = None,
    span_parent_id: str = "",
    span_attempt_id: str = "",
) -> dict[str, Any]:
    """Collect independent receipts concurrently and return one gathered update."""

    _discover_registered_adapters()
    snapshot = copy.deepcopy(dict(state))
    post_merge = snapshot.get("post_merge") if isinstance(snapshot.get("post_merge"), Mapping) else {}
    target_sha = _safe_sha(post_merge.get("target_sha"))
    if not target_sha:
        raise ValueError("persisted post-merge target SHA is required before collection")
    workspace = snapshot.get("workspace") if isinstance(snapshot.get("workspace"), Mapping) else {}
    policy = snapshot.get("policy") if isinstance(snapshot.get("policy"), Mapping) else {}
    requirements = (
        policy.get("post_merge_requirements")
        if isinstance(policy.get("post_merge_requirements"), Mapping)
        else {}
    )
    repository = str(workspace.get("repository") or "").strip()
    workspace_path = str(workspace.get("path") or "").strip()
    canonical_path = str(workspace.get("canonical_path") or "").strip()
    branch = str(workspace.get("base_branch") or "main").strip() or "main"
    enforce = str(snapshot.get("mode") or "off").strip().lower() == "enforce"
    collected_at = float(time.time() if now is None else now)
    cfg = config if isinstance(config, Mapping) else {}
    repo_cfg = _repository_config(cfg, repository)
    try:
        collector_timeout_s = max(0.1, min(float(cfg.get("collector_timeout_s") or 120.0), 300.0))
    except (TypeError, ValueError):
        collector_timeout_s = 120.0
    try:
        adapter_timeout_s = max(0.1, min(float(cfg.get("adapter_timeout_s") or 60.0), collector_timeout_s))
    except (TypeError, ValueError):
        adapter_timeout_s = min(60.0, collector_timeout_s)
    runner = run or _default_run
    synchronizer = sync_canonical or _default_sync
    ownership_allows_mutation = mutation_allowed or (lambda: True)

    gathered = copy.deepcopy(dict(post_merge))
    gathered["target_sha"] = target_sha
    jobs: dict[str, Callable[[PostMergeControl], dict[str, Any]]] = {}
    isolated_jobs: set[str] = set()

    def exact_pass(name: str) -> bool:
        prior = post_merge.get(name) if isinstance(post_merge.get(name), Mapping) else {}
        return (
            str(prior.get("status") or "").strip().lower() == "passed"
            and _safe_sha(prior.get("observed_sha")) == target_sha
        )

    canonical_configured = requirements.get("canonical_sync") is True or bool(canonical_path)
    canonical_already_proven = canonical_configured and exact_pass("canonical_sync")
    if canonical_configured and read_only:
        gathered["canonical_sync"] = _receipt(
            "not_configured",
            now=collected_at,
            diagnostic_code="shadow_not_executed",
        )
    elif canonical_configured and not canonical_already_proven:
        jobs["canonical_sync"] = lambda control: _collect_canonical(
            target_sha=target_sha,
            canonical_path=canonical_path,
            branch=branch,
            required=requirements.get("canonical_sync") is True,
            enforce=enforce,
            sync_canonical=synchronizer,
            control=control,
            now=collected_at,
        )
    if requirements.get("ci") is True and not exact_pass("ci"):
        jobs["ci"] = lambda control: _collect_ci(
            target_sha=target_sha,
            repository=repository,
            workspace_path=workspace_path,
            run=runner,
            control=control,
            now=collected_at,
        )
    adapter_specs = (
        ("deployment", _DEPLOYMENT_ADAPTERS, "deployment_adapter"),
        ("production_qa", _PRODUCTION_QA_ADAPTERS, "production_qa_adapter"),
        ("restart", _RESTART_ADAPTERS, "restart_adapter"),
    )
    for receipt_name, registry, config_key in adapter_specs:
        adapter_name = str(repo_cfg.get(config_key) or "").strip().lower()
        required = requirements.get(receipt_name) is True
        if not (required or adapter_name):
            continue
        if read_only:
            gathered[receipt_name] = _receipt(
                "not_configured",
                now=collected_at,
                diagnostic_code="shadow_not_executed",
            )
            continue
        if exact_pass(receipt_name):
            continue
        prior_receipt = (
            dict(post_merge.get(receipt_name))
            if isinstance(post_merge.get(receipt_name), Mapping)
            else {}
        )
        isolated_jobs.add(receipt_name)
        jobs[receipt_name] = lambda control, registry=registry, adapter_name=adapter_name, required=required, prior_receipt=prior_receipt: _adapter_receipt(
            registry=registry,
            adapter_name=adapter_name,
            required=required,
            enforce=enforce,
            target_sha=target_sha,
            repository=repository,
            workspace_path=workspace_path,
            canonical_path=canonical_path,
            timeout_s=adapter_timeout_s,
            control=control,
            now=collected_at,
            prior_receipt=prior_receipt,
        )

    if jobs:
        try:
            workers = max(1, min(_MAX_WORKERS, int(max_workers), len(jobs)))
        except (TypeError, ValueError):
            workers = min(_MAX_WORKERS, len(jobs))
        hard_deadline = time.monotonic() + collector_timeout_s
        cleanup_budget = min(0.1, max(0.02, collector_timeout_s * 0.2))
        execution_deadline = hard_deadline - cleanup_budget
        try:
            process_context = multiprocessing.get_context("fork")
        except ValueError:
            process_context = None
        event_factory = process_context.Event if process_context is not None else threading.Event
        controls = {
            name: PostMergeControl(
                deadline=execution_deadline,
                _cancel_event=event_factory(),
            )
            for name in jobs
        }
        span_handles: dict[str, Any] = {}
        span_finished: set[str] = set()
        phase_by_name = {
            "canonical_sync": "canonical_sync",
            "ci": "ci",
            "deployment": "deployment",
            "production_qa": "production_qa",
            "restart": "restart",
        }

        def start_span(name: str) -> None:
            if span_recorder is None or name in span_handles:
                return
            span_handles[name] = span_recorder.start(
                f"post_merge_{name}",
                phase=phase_by_name.get(name, "closeout"),
                parent_id=span_parent_id,
                attempt_id=span_attempt_id,
                concurrency_id=f"post-merge-{target_sha[:12]}",
                metadata={"collector": name, "repository": repository},
            )

        def finish_span_once(name: str, status: str) -> None:
            if span_recorder is None or name not in span_handles or name in span_finished:
                return
            span_finished.add(name)
            span_recorder.finish(span_handles[name], status=status)

        def finish_receipt(name: str, receipt: Mapping[str, Any]) -> None:
            gathered[name] = dict(receipt)
            receipt_status = str(receipt.get("status") or "blocked").strip().lower()
            span_status = {
                "passed": "ok",
                "not_configured": "ok",
                "pending": "uncertain",
                "failed": "error",
                "blocked": "blocked",
            }.get(receipt_status, "uncertain")
            finish_span_once(name, span_status)

        pending = [name for name in jobs if name != "restart"]
        restart_waiting = "restart" in jobs
        if restart_waiting and (not canonical_configured or canonical_already_proven):
            pending.append("restart")
            restart_waiting = False
        running: dict[str, tuple[str, Any, Any]] = {}

        def start_one(name: str) -> None:
            start_span(name)
            if name not in isolated_jobs:
                slot: dict[str, Any] = {}

                def run_native() -> None:
                    try:
                        slot["payload"] = ("result", jobs[name](controls[name]))
                    except BaseException:
                        slot["payload"] = ("error", None)

                thread = threading.Thread(
                    target=run_native,
                    name=f"post-merge-{name}",
                    daemon=True,
                )
                thread.start()
                running[name] = ("thread", thread, slot)
                return
            if process_context is None:
                finish_receipt(
                    name,
                    _receipt("blocked", now=collected_at, diagnostic_code="collector_isolation_unavailable"),
                )
                return
            receive, send = process_context.Pipe(duplex=False)
            process = process_context.Process(
                target=_isolated_collector_entry,
                args=(jobs[name], controls[name], send),
                name=f"post-merge-{name}",
            )
            process.start()
            send.close()
            running[name] = ("process", process, receive)

        while (pending or running or restart_waiting) and time.monotonic() < execution_deadline:
            if not ownership_allows_mutation():
                for control in controls.values():
                    control.cancel()
                pending.clear()
                restart_waiting = False
                break
            while pending and len(running) < workers:
                if not ownership_allows_mutation():
                    for control in controls.values():
                        control.cancel()
                    pending.clear()
                    restart_waiting = False
                    break
                start_one(pending.pop(0))
            completed_names: list[str] = []
            for name, (kind, worker, channel) in list(running.items()):
                payload: tuple[str, Any] | None = None
                if kind == "thread":
                    if worker.is_alive():
                        continue
                    payload = channel.get("payload", ("error", None))
                    worker.join(timeout=0)
                else:
                    if channel.poll(0):
                        try:
                            payload = channel.recv()
                        except (EOFError, OSError):
                            payload = ("error", None)
                    elif not worker.is_alive():
                        payload = ("error", None)
                    if payload is None:
                        continue
                    worker.join(timeout=max(0.0, hard_deadline - time.monotonic()))
                    if worker.is_alive():
                        _terminate_isolated_collector(worker, deadline=hard_deadline)
                    channel.close()
                completed_names.append(name)
                if payload[0] == "result" and isinstance(payload[1], Mapping):
                    finish_receipt(name, payload[1])
                else:
                    finish_receipt(
                        name,
                        _receipt("blocked", now=collected_at, diagnostic_code="collector_failed"),
                    )
            for name in completed_names:
                running.pop(name, None)
                if name == "canonical_sync" and restart_waiting:
                    canonical_receipt = gathered.get("canonical_sync")
                    canonical_status = (
                        str(canonical_receipt.get("status") or "").strip().lower()
                        if isinstance(canonical_receipt, Mapping)
                        else ""
                    )
                    canonical_ready = (
                        canonical_status == "passed"
                        and _safe_sha(canonical_receipt.get("observed_sha")) == target_sha
                    ) or (
                        canonical_status == "not_configured"
                        and canonical_receipt.get("diagnostic_code") == "canonical_not_applicable"
                    )
                    if canonical_ready:
                        pending.append("restart")
                    else:
                        start_span("restart")
                        finish_receipt(
                            "restart",
                            _receipt(
                                "pending",
                                now=collected_at,
                                diagnostic_code="canonical_sync_not_ready",
                            ),
                        )
                    restart_waiting = False
            if not completed_names:
                time.sleep(min(0.005, max(0.0, execution_deadline - time.monotonic())))

        for name, (kind, worker, channel) in list(running.items()):
            controls[name].cancel()
            if kind == "process":
                _terminate_isolated_collector(worker, deadline=hard_deadline)
                channel.close()
            finish_span_once(name, "timeout")
            gathered[name] = _receipt(
                "blocked",
                now=collected_at,
                diagnostic_code="collector_timeout",
            )
        for name in pending:
            start_span(name)
            finish_span_once(name, "timeout")
            gathered[name] = _receipt(
                "blocked",
                now=collected_at,
                diagnostic_code="collector_timeout",
            )
        if restart_waiting:
            start_span("restart")
            finish_receipt(
                "restart",
                _receipt("pending", now=collected_at, diagnostic_code="canonical_sync_not_ready"),
            )
    for name in _RECEIPT_NAMES:
        if name not in gathered:
            gathered[name] = {"status": "not_configured"}
    return gathered


__all__ = [
    "PostMergeAdapter",
    "PostMergeControl",
    "collect_post_merge_receipts",
    "gateway_restart_adapter",
    "initialize_post_merge_receipts",
    "register_deployment_adapter",
    "register_production_qa_adapter",
    "register_restart_adapter",
]

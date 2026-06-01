"""OpenCode non-PTY coding worker backend."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


BACKEND_CODEX = "codex"
BACKEND_OPENCODE = "opencode"
_VALID_BACKENDS = {BACKEND_CODEX, BACKEND_OPENCODE}
_VALID_REASONING_LEVELS = {"minimal", "low", "medium", "high", "xhigh", "max"}
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 90.0


@dataclass
class OpenCodeRunResult:
    final_text: str = ""
    error: Optional[str] = None
    interrupted: bool = False
    timed_out: bool = False
    should_retire: bool = False
    tool_iterations: int = 0
    turn_id: Optional[str] = None
    thread_id: Optional[str] = None
    backend: str = BACKEND_OPENCODE
    agents: list[str] = field(default_factory=list)
    plan_text: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    exit_code: Optional[int] = None
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    run_profile: dict[str, Any] = field(default_factory=dict)


@dataclass
class _OpenCodeProcessResult:
    returncode: Optional[int]
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    startup_timed_out: bool = False
    duration_seconds: float = 0.0


def normalize_coding_worker_backend(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _VALID_BACKENDS else BACKEND_CODEX


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def load_coding_worker_backend(
    config: Optional[dict[str, Any]] = None,
    *,
    worker_config: Optional[dict[str, Any]] = None,
) -> str:
    """Resolve coding worker backend.

    Precedence:
      1. HERMES_CODING_WORKER_BACKEND
      2. kanban.discord_worker.backend (passed as worker_config)
      3. coding_worker.backend
      4. codex
    """
    raw_env = os.getenv("HERMES_CODING_WORKER_BACKEND")
    if raw_env:
        return normalize_coding_worker_backend(raw_env)

    if worker_config and worker_config.get("backend"):
        return normalize_coding_worker_backend(worker_config.get("backend"))

    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}
    if isinstance(cfg, dict):
        coding_cfg = cfg.get("coding_worker") if isinstance(cfg.get("coding_worker"), dict) else {}
        if coding_cfg.get("backend"):
            return normalize_coding_worker_backend(coding_cfg.get("backend"))
    return BACKEND_CODEX


def load_coding_worker_pass_config(
    config: Optional[dict[str, Any]] = None,
    *,
    worker_config: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}

    coding_cfg: dict[str, Any] = {}
    if isinstance(cfg, dict) and isinstance(cfg.get("coding_worker"), dict):
        coding_cfg.update(cfg["coding_worker"])
    if worker_config:
        for key in (
            "simple_build_reasoning_level",
            "complex_plan_reasoning_level",
            "complex_build_reasoning_level",
        ):
            if key in worker_config:
                coding_cfg[key] = worker_config[key]

    return {
        "simple_build_reasoning_level": _normalize_reasoning_level(
            coding_cfg.get("simple_build_reasoning_level") or "xhigh"
        ),
        "complex_plan_reasoning_level": _normalize_reasoning_level(
            coding_cfg.get("complex_plan_reasoning_level") or "xhigh"
        ),
        "complex_build_reasoning_level": _normalize_reasoning_level(
            coding_cfg.get("complex_build_reasoning_level") or "medium"
        ),
    }


def load_opencode_config(
    config: Optional[dict[str, Any]] = None,
    *,
    worker_config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}

    opencode_cfg: dict[str, Any] = {}
    if isinstance(cfg, dict):
        coding_cfg = cfg.get("coding_worker") if isinstance(cfg.get("coding_worker"), dict) else {}
        if isinstance(coding_cfg.get("opencode"), dict):
            opencode_cfg.update(coding_cfg["opencode"])

    if worker_config and isinstance(worker_config.get("opencode"), dict):
        opencode_cfg.update(worker_config["opencode"])

    pass_cfg = load_coding_worker_pass_config(cfg, worker_config=worker_config)
    return {
        "binary": str(opencode_cfg.get("binary") or "opencode"),
        "model": str(opencode_cfg.get("model") or "").strip(),
        "plan_agent": str(opencode_cfg.get("plan_agent") or "plan").strip() or "plan",
        "build_agent": str(opencode_cfg.get("build_agent") or "build").strip() or "build",
        "simple_build_reasoning_level": pass_cfg["simple_build_reasoning_level"],
        "complex_plan_reasoning_level": pass_cfg["complex_plan_reasoning_level"],
        "complex_build_reasoning_level": pass_cfg["complex_build_reasoning_level"],
        "dangerously_skip_permissions": bool(opencode_cfg.get("dangerously_skip_permissions", False)),
        "startup_timeout_seconds": _non_negative_float(
            os.getenv("HERMES_OPENCODE_STARTUP_TIMEOUT_SECONDS")
            or opencode_cfg.get("startup_timeout_seconds"),
            _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        ),
    }


def check_opencode_binary(config: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
    binary = load_opencode_config(config).get("binary") or "opencode"
    if os.path.isabs(str(binary)):
        path = Path(str(binary))
        if path.is_file() and os.access(path, os.X_OK):
            return True, str(path)
        return False, f"OpenCode binary is not executable: {path}"
    resolved = shutil.which(str(binary))
    if not resolved:
        return False, f"OpenCode CLI not found in PATH: {binary}"
    return True, resolved


def opencode_credentials_look_configured(config: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
    ok, resolved = check_opencode_binary(config)
    if not ok:
        return False, resolved
    try:
        proc = subprocess.run(
            [resolved, "providers", "list"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return False, f"OpenCode credentials check failed: {exc}"
    output = "\n".join(
        part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()
    )
    if proc.returncode != 0:
        return False, output or f"OpenCode credentials check exited {proc.returncode}"
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).strip()
    if not plain or re.search(r"\b(?:0|no) credentials\b", plain, flags=re.IGNORECASE):
        return False, "OpenCode is installed but has no configured credentials."
    return True, plain


def looks_complex_or_risky(task: str, context: str = "") -> bool:
    lower = f"{task}\n{context}".lower()
    if not lower.strip():
        return False
    explicit_plan = (
        "plan first",
        "first plan",
        "planning pass",
        "two phase",
        "two-phase",
        "design before",
    )
    if any(phrase in lower for phrase in explicit_plan):
        return True

    simple_signals = (
        "typo",
        "comment",
        "formatting",
        "small docs",
        "documentation",
        "readme",
        "changelog",
        "one-line",
        "one line",
        "trivial",
        "mechanical",
    )
    if any(_contains_signal(lower, signal) for signal in simple_signals):
        return False

    risky_signals = (
        "security",
        "auth",
        "permission",
        "sandbox",
        "secret",
        "credential",
        "payment",
        "wallet",
        "signing",
        "race",
        "deadlock",
        "concurrency",
        "data loss",
        "migration",
        "schema migration",
        "breaking change",
        "architecture",
        "design review",
        "audit",
        "incident",
        "production",
        "unsafe",
        "dangerous",
        "rewrite",
        "upgrade",
        "rebase",
        "merge conflict",
        "flaky",
        "intermittent",
        "root cause",
        "state machine",
        "async",
        "cache",
        "performance",
    )
    return any(_contains_signal(lower, signal) for signal in risky_signals)


def run_opencode_task(
    prompt: str,
    workspace: str,
    *,
    timeout: float,
    context_for_classification: str = "",
    force_plan: Optional[bool] = None,
    title: str = "",
    config: Optional[dict[str, Any]] = None,
    worker_config: Optional[dict[str, Any]] = None,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
) -> OpenCodeRunResult:
    cfg = load_opencode_config(config, worker_config=worker_config)
    needs_plan = (
        bool(force_plan)
        if force_plan is not None
        else looks_complex_or_risky(prompt, context_for_classification)
    )
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    agents: list[str] = []
    run_profile = _task_run_profile(cfg, needs_plan)

    def _capture(event: dict[str, Any]) -> None:
        events.append(event)
        if on_event is not None:
            on_event(event)

    plan_text = ""
    if needs_plan:
        agents.append(cfg["plan_agent"])
        plan = _run_opencode_once(
            prompt=_plan_prompt(prompt),
            workspace=workspace,
            timeout=max(30.0, timeout),
            cfg=cfg,
            agent=cfg["plan_agent"],
            reasoning_level=cfg["complex_plan_reasoning_level"],
            title=title,
            on_event=_capture,
        )
        if plan.error:
            plan.backend = BACKEND_OPENCODE
            plan.agents = agents
            plan.events = events
            plan.plan_text = plan.final_text
            plan.duration_seconds = round(time.monotonic() - started, 2)
            plan.run_profile = run_profile
            return plan
        plan_text = plan.final_text.strip()

    agents.append(cfg["build_agent"])
    build_prompt = prompt
    if plan_text:
        build_prompt = (
            f"{prompt.rstrip()}\n\n"
            "OpenCode plan to follow:\n"
            f"{plan_text}\n"
        )
    build = _run_opencode_once(
        prompt=build_prompt,
        workspace=workspace,
        timeout=max(30.0, timeout),
        cfg=cfg,
        agent=cfg["build_agent"],
        reasoning_level=(
            cfg["complex_build_reasoning_level"]
            if needs_plan
            else cfg["simple_build_reasoning_level"]
        ),
        title=title,
        on_event=_capture,
    )
    build.backend = BACKEND_OPENCODE
    build.agents = agents
    build.plan_text = plan_text
    build.events = events
    build.run_profile = run_profile
    build.tool_iterations = len(events)
    build.timed_out = bool(build.timed_out)
    if build.error is None and not build.final_text.strip():
        build.error = "OpenCode completed without producing final text."
    build.exit_code = build.exit_code
    if build.thread_id is None:
        build.thread_id = _last_session_id(events)
    build.turn_id = build.thread_id
    build.duration_seconds = round(time.monotonic() - started, 2)
    if build.stderr:
        build.stderr = build.stderr.strip()
    build.stdout = build.stdout.strip()
    return build


def run_opencode_single_pass(
    prompt: str,
    workspace: str,
    *,
    timeout: float,
    agent: str,
    reasoning_level: str,
    title: str = "",
    config: Optional[dict[str, Any]] = None,
    worker_config: Optional[dict[str, Any]] = None,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
) -> OpenCodeRunResult:
    cfg = load_opencode_config(config, worker_config=worker_config)
    selected_agent = str(agent or cfg["build_agent"]).strip() or cfg["build_agent"]
    selected_reasoning = _normalize_reasoning_level(reasoning_level)
    started = time.monotonic()
    events: list[dict[str, Any]] = []

    def _capture(event: dict[str, Any]) -> None:
        events.append(event)
        if on_event is not None:
            on_event(event)

    result = _run_opencode_once(
        prompt=prompt,
        workspace=workspace,
        timeout=max(30.0, timeout),
        cfg=cfg,
        agent=selected_agent,
        reasoning_level=selected_reasoning,
        title=title,
        on_event=_capture,
    )
    result.backend = BACKEND_OPENCODE
    result.agents = [selected_agent]
    result.run_profile = {
        "kind": "single_pass",
        "label": f"1-pass {selected_agent}",
        "pass_count": 1,
        "plan_used": False,
        "passes": [
            {
                "name": selected_agent,
                "agent": selected_agent,
                "reasoning": selected_reasoning,
            }
        ],
    }
    if events:
        result.events = events
    result.tool_iterations = len(result.events)
    result.duration_seconds = round(time.monotonic() - started, 2)
    if result.error is None and not result.final_text.strip():
        result.error = "OpenCode completed without producing final text."
    if result.thread_id is None:
        result.thread_id = _last_session_id(result.events)
    result.turn_id = result.thread_id
    result.stdout = result.stdout.strip()
    result.stderr = result.stderr.strip()
    return result


def _task_run_profile(cfg: dict[str, Any], needs_plan: bool) -> dict[str, Any]:
    if needs_plan:
        return {
            "kind": "two_pass_plan_build",
            "label": "2-pass plan+build",
            "pass_count": 2,
            "plan_used": True,
            "passes": [
                {
                    "name": "plan",
                    "agent": cfg["plan_agent"],
                    "reasoning": cfg["complex_plan_reasoning_level"],
                },
                {
                    "name": "build",
                    "agent": cfg["build_agent"],
                    "reasoning": cfg["complex_build_reasoning_level"],
                },
            ],
        }
    return {
        "kind": "one_pass_simple_build",
        "label": "1-pass simple build",
        "pass_count": 1,
        "plan_used": False,
        "passes": [
            {
                "name": "build",
                "agent": cfg["build_agent"],
                "reasoning": cfg["simple_build_reasoning_level"],
            }
        ],
    }


def _run_opencode_once(
    *,
    prompt: str,
    workspace: str,
    timeout: float,
    cfg: dict[str, Any],
    agent: str,
    reasoning_level: str,
    title: str,
    on_event: Callable[[dict[str, Any]], None],
) -> OpenCodeRunResult:
    ok, binary_or_error = check_opencode_binary({"coding_worker": {"opencode": cfg}})
    if not ok:
        return OpenCodeRunResult(error=binary_or_error)

    workdir_path = Path(workspace).expanduser().resolve()
    workdir = str(workdir_path)
    brief_path = _write_brief(prompt, workspace=workdir_path)
    cmd = [
        binary_or_error,
        "run",
        "--pure",
        "Read the attached Hermes worker brief and follow it exactly.",
        "--format",
        "json",
        "--agent",
        agent,
        "--dir",
        workdir,
    ]
    if cfg.get("model"):
        cmd.extend(["--model", str(cfg["model"])])
    if reasoning_level:
        cmd.extend(["--variant", reasoning_level])
    if title:
        cmd.extend(["--title", title])
    if cfg.get("dangerously_skip_permissions"):
        cmd.append("--dangerously-skip-permissions")
    cmd.extend(["--file", str(brief_path)])

    try:
        configured_startup_timeout = float(
            cfg.get("startup_timeout_seconds")
            if cfg.get("startup_timeout_seconds") is not None
            else _DEFAULT_STARTUP_TIMEOUT_SECONDS
        )
        startup_timeout = (
            0.0
            if configured_startup_timeout <= 0
            else min(max(10.0, configured_startup_timeout), timeout)
        )
        proc = _run_opencode_process(
            cmd,
            timeout=timeout,
            startup_timeout=startup_timeout,
            workdir=workdir,
        )
    except Exception as exc:
        return OpenCodeRunResult(error=f"OpenCode {agent} run failed to start: {exc}")
    finally:
        try:
            brief_path.unlink()
        except OSError:
            pass

    result = _parse_opencode_output(proc.stdout, proc.stderr, on_event=on_event)
    result.exit_code = proc.returncode
    result.stdout = proc.stdout or ""
    result.stderr = proc.stderr or ""
    if proc.timed_out:
        result.timed_out = True
        result.should_retire = True
        if result.error is None:
            if proc.startup_timed_out and not result.events:
                result.error = (
                    f"OpenCode {agent} produced no JSON events for "
                    f"{proc.duration_seconds:g}s during startup and was killed "
                    f"before the full {timeout:g}s turn timeout. This usually "
                    "means OpenCode is stuck bootstrapping the repository "
                    "(snapshot/file watcher setup) before reaching the model."
                )
            else:
                result.error = f"OpenCode {agent} run timed out after {timeout:g}s."
    if proc.returncode == 0 and result.error is None and not result.final_text.strip():
        result.final_text = _load_final_text_from_export(
            binary_or_error,
            result.thread_id or _last_session_id(result.events),
        )
    if proc.returncode != 0 and result.error is None:
        result.error = _classify_opencode_error(
            result.stdout,
            result.stderr,
            f"OpenCode {agent} exited with code {proc.returncode}.",
        )
    if result.error is not None:
        result.error = _classify_opencode_error(result.error, result.stdout, result.stderr)
    result.thread_id = result.thread_id or _last_session_id(result.events)
    result.turn_id = result.thread_id
    result.tool_iterations = len(result.events)
    return result


def _run_opencode_process(
    cmd: list[str],
    *,
    workdir: str,
    timeout: float,
    startup_timeout: float,
) -> _OpenCodeProcessResult:
    """Run OpenCode while watching for no-output startup stalls.

    ``opencode run --format json`` emits JSONL on stdout only after the run
    reaches the session/model path. In large repos it can hang during
    bootstrap before any JSON is emitted, which used to burn the whole worker
    timeout and report zero tool iterations. Kill that case separately so the
    caller gets a concrete infrastructure failure.
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        raise

    line_queue: queue.Queue[tuple[str, Optional[str]]] = queue.Queue()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _reader(name: str, stream: Any) -> None:
        try:
            if stream is not None:
                for line in iter(stream.readline, ""):
                    if line == "":
                        break
                    line_queue.put((name, line))
        finally:
            line_queue.put((name, None))

    threads = [
        threading.Thread(target=_reader, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=_reader, args=("stderr", proc.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    closed_streams: set[str] = set()
    timed_out = False
    startup_timed_out = False

    def _terminate() -> None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    while True:
        now = time.monotonic()
        elapsed = now - started
        if proc.poll() is None:
            if elapsed >= timeout:
                timed_out = True
                _terminate()
            elif startup_timeout > 0 and not stdout_lines and elapsed >= startup_timeout:
                timed_out = True
                startup_timed_out = True
                _terminate()

        try:
            name, line = line_queue.get(timeout=0.1)
        except queue.Empty:
            if proc.poll() is not None and len(closed_streams) >= 2:
                break
            continue

        if line is None:
            closed_streams.add(name)
        elif name == "stdout":
            stdout_lines.append(line)
        elif name == "stderr":
            stderr_lines.append(line)

        if proc.poll() is not None and len(closed_streams) >= 2:
            break

    for thread in threads:
        thread.join(timeout=1)

    return _OpenCodeProcessResult(
        returncode=proc.returncode,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        timed_out=timed_out,
        startup_timed_out=startup_timed_out,
        duration_seconds=round(time.monotonic() - started, 2),
    )


def _parse_opencode_output(
    stdout: str,
    stderr: str,
    *,
    on_event: Callable[[dict[str, Any]], None],
) -> OpenCodeRunResult:
    events: list[dict[str, Any]] = []
    texts: list[str] = []
    raw_text_lines: list[str] = []
    error: Optional[str] = None
    session_id: Optional[str] = None

    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            raw_text_lines.append(stripped)
            continue
        if not isinstance(event, dict):
            raw_text_lines.append(stripped)
            continue
        events.append(event)
        on_event(event)
        session_id = session_id or _event_session_id(event)
        if str(event.get("type") or "").lower() == "error":
            error = _event_error_text(event) or "OpenCode reported an error."
            continue
        text = _event_text(event)
        if text:
            texts.append(text)

    final_text = "\n".join(texts).strip()
    if not final_text and raw_text_lines:
        final_text = "\n".join(raw_text_lines).strip()
    if not final_text and stderr and error is None:
        final_text = stderr.strip()

    return OpenCodeRunResult(
        final_text=final_text,
        error=error,
        events=events,
        thread_id=session_id,
        turn_id=session_id,
    )


def _load_final_text_from_export(binary: str, session_id: Optional[str]) -> str:
    """Recover assistant text from ``opencode export`` when JSONL output is sparse."""
    if not session_id:
        return ""
    try:
        proc = subprocess.run(
            [binary, "export", session_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return _parse_opencode_export_text(proc.stdout)


def _parse_opencode_export_text(stdout: str) -> str:
    text = (stdout or "").strip()
    if not text:
        return ""
    json_start = text.find("{")
    if json_start > 0:
        text = text[json_start:]
    try:
        exported = json.loads(text)
    except json.JSONDecodeError:
        return ""
    messages = exported.get("messages") if isinstance(exported, dict) else None
    if not isinstance(messages, list):
        return ""

    assistant_texts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        info_raw = message.get("info")
        info = info_raw if isinstance(info_raw, dict) else {}
        if str(info.get("role") or message.get("role") or "").lower() != "assistant":
            continue
        part_texts: list[str] = []
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if str(part.get("type") or "").lower() != "text":
                continue
            if part.get("synthetic") is True:
                continue
            content = str(part.get("text") or "").strip()
            if content:
                part_texts.append(content)
        if part_texts:
            assistant_texts.append("\n".join(part_texts).strip())
    return "\n".join(filter(None, assistant_texts)).strip()


def _write_brief(prompt: str, *, workspace: Optional[Path] = None) -> Path:
    root = (
        workspace / ".hermes-opencode"
        if workspace is not None
        else Path(tempfile.gettempdir()) / "opencode"
    )
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="hermes-worker-",
        suffix=".md",
        dir=str(root),
        delete=False,
    ) as handle:
        handle.write(prompt)
        handle.write("\n")
        return Path(handle.name)


def _plan_prompt(prompt: str) -> str:
    return (
        "Create a concise implementation plan for the attached Hermes worker "
        "brief. Do not edit repository files. Focus on the minimum safe changes, "
        "key files to inspect, and verification steps. Return plain text.\n\n"
        f"Worker brief:\n{prompt}"
    )


def _normalize_reasoning_level(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _VALID_REASONING_LEVELS else ""


def _contains_signal(text: str, signal: str) -> bool:
    if " " in signal or "-" in signal:
        return signal in text
    return bool(re.search(rf"\b{re.escape(signal)}\b", text))


def _event_session_id(event: dict[str, Any]) -> Optional[str]:
    for key in ("sessionID", "sessionId", "session_id"):
        value = event.get(key)
        if value:
            return str(value)
    return None


def _last_session_id(events: list[dict[str, Any]]) -> Optional[str]:
    session_id = None
    for event in events:
        session_id = _event_session_id(event) or session_id
    return session_id


def _event_error_text(event: dict[str, Any]) -> str:
    err = event.get("error")
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        parts = []
        for key in ("message", "code", "name"):
            value = err.get(key)
            if value:
                parts.append(str(value))
        data = err.get("data")
        if isinstance(data, dict):
            message = data.get("message")
            if message:
                parts.append(str(message))
        return ": ".join(parts)
    return ""


def _event_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (_event_text(item) for item in value))).strip()
    if not isinstance(value, dict):
        return ""

    if str(value.get("type") or "").lower() == "error":
        return ""
    for key in ("text", "message", "content", "output", "result", "final", "part"):
        if key in value:
            text = _event_text(value.get(key))
            if text:
                return text
    data = value.get("data")
    if isinstance(data, dict):
        text = _event_text(data)
        if text:
            return text
    return ""


def _classify_opencode_error(*parts: str) -> str:
    text = "\n".join(part for part in parts if part).strip()
    lower = text.lower()
    details = _shorten_opencode_error_details(text)
    if any(
        needle in lower
        for needle in (
            "contextoverflowerror",
            "context_length_exceeded",
            "input exceeds context window",
            "exceeds the context window",
            "context window of this model",
        )
    ):
        return (
            "OpenCode context window exceeded. Reduce the worker prompt or "
            f"retry with a larger-context model. Details: {details}"
        )
    if any(
        needle in lower
        for needle in (
            "token_invalidated",
            "authentication token has been invalidated",
            "authentication failed",
            "not authenticated",
            "unauthorized",
            "401",
            "signing in again",
            "please login",
            "please log in",
            "invalid api key",
            "invalid_api_key",
        )
    ):
        return (
            "OpenCode authentication failed. Run `opencode auth login` "
            f"or configure a valid OpenCode provider, then retry. Details: {details}"
        )
    return text or "OpenCode worker failed."


def _shorten_opencode_error_details(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    marker = "\n... [truncated]"
    if limit <= len(marker):
        return marker[:limit]
    return text[: limit - len(marker)] + marker

"""Turn-scoped preview/browser-QA readiness circuit."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from agent.redact import redact_sensitive_text


MAX_PREVIEW_EVENTS = 6
_APPROVED_SCRIPTS = (
    "qa:auth",
    "qa:admin-viewer",
    "test:e2e",
    "e2e",
    "test:browser",
    "browser:qa",
)
_DETERMINISTIC = frozenset({
    "vite_fs_allow",
    "missing_environment",
    "application_bootstrap",
    "hmr_origin_mismatch",
})
_SUMMARIES = {
    "vite_fs_allow": "Vite refused files outside the configured serving allow-list.",
    "missing_environment": "The preview bootstrap is missing required environment configuration.",
    "application_bootstrap": "The local route reached an application bootstrap error.",
    "hmr_origin_mismatch": (
        "The local preview reloaded through an external HMR origin before browser QA could complete."
    ),
    "transient_browser_network": (
        "The browser could not reach the target because of a transient "
        "network/navigation failure."
    ),
}


@dataclass(frozen=True)
class PreviewFailure:
    failure_class: str
    deterministic: bool
    summary: str


@dataclass(frozen=True)
class PreviewBlockDecision:
    code: str
    message: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _Launcher:
    script: str
    command: str


@dataclass
class _Attempt:
    root: str
    strategy: str
    strategy_hash: str
    approved: bool
    origin: str = ""
    session_id: str = ""
    status: str = "running"
    failure_class: str = ""


def classify_preview_failure(result: Any) -> PreviewFailure | None:
    """Distinguish deterministic readiness failures from transient navigation."""

    text = _result_text(result).lower()
    failure_class = ""
    deterministic = True
    if any(
        marker in text
        for marker in (
            "outside of vite serving allow list",
            "outside of vite's serving allow list",
            "server.fs.allow",
            "not in the fs allow list",
        )
    ):
        failure_class = "vite_fs_allow"
    elif (
        "supabaseurl is required" in text
        or "missing supabase configuration" in text
        or re.search(r"missing required [^\n]{0,160}environment variable", text)
        or re.search(r"required [a-z0-9_:-]{2,80} environment variable", text)
    ):
        failure_class = "missing_environment"
    elif (
        re.search(r'"title"\s*:\s*"internal error"', text)
        or 'heading "internal error"' in text
        or "<title>internal error</title>" in text
    ):
        failure_class = "application_bootstrap"
    elif (
        "vite server connection lost" in text
        and ("cloudflareaccess.com" in text or "external hmr" in text)
    ):
        failure_class = "hmr_origin_mismatch"
    elif any(
        marker in text
        for marker in (
            "err_connection_refused",
            "connection refused",
            "econnrefused",
            "err_name_not_resolved",
            "name or service not known",
            "temporary failure in name resolution",
            "navigation timeout",
            "timed out navigating",
            "net::err_connection_reset",
            "econnreset",
        )
    ):
        failure_class = "transient_browser_network"
        deterministic = False
    if not failure_class:
        return None
    return PreviewFailure(failure_class, deterministic, _SUMMARIES[failure_class])


def sanitize_preview_event(value: Any) -> dict[str, Any] | None:
    """Allow-list and bound one runtime/ledger event."""

    if not isinstance(value, Mapping):
        return None
    status = _token(value.get("status"), 32)
    if status not in {"blocked", "failed", "ready", "unavailable"}:
        return None
    event: dict[str, Any] = {
        "status": status,
        "deterministic": bool(value.get("deterministic")),
    }
    for key, limit in (("failure_class", 48), ("strategy", 48)):
        cleaned = _token(value.get(key), limit)
        if cleaned:
            event[key] = cleaned
    strategy_hash = _safe_hash(value.get("strategy_hash"))
    if strategy_hash:
        event["strategy_hash"] = strategy_hash
    summary = _safe_summary(value.get("summary"))
    if summary:
        event["summary"] = summary
    launcher = _safe_launcher(value.get("recommended_launcher"))
    if launcher:
        event["recommended_launcher"] = launcher
    cleanup = value.get("cleanup")
    if isinstance(cleanup, Mapping) and _token(cleanup.get("status"), 32):
        event["cleanup"] = {"status": _token(cleanup.get("status"), 32)}
        session_ref = _safe_hash(cleanup.get("session_ref"))
        if session_ref:
            event["cleanup"]["session_ref"] = session_ref
    budget = value.get("budget")
    if isinstance(budget, Mapping):
        event["budget"] = {
            key: _bounded_int(budget.get(key), 8)
            for key in ("local_used", "local_max", "native_used", "native_max")
        }
    return event


def summarize_preview_events(value: Any) -> dict[str, Any]:
    events = []
    if isinstance(value, (list, tuple)):
        events = [
            event
            for event in (sanitize_preview_event(raw) for raw in value[-12:])
            if event is not None
        ][-MAX_PREVIEW_EVENTS:]
    latest = events[-1] if events else {}
    return {
        "schema_version": 1,
        "status": latest.get("status", "not_observed"),
        "latest_failure_class": latest.get("failure_class", ""),
        "deterministic_failures": sum(
            event.get("status") == "failed" and event.get("deterministic") is True
            for event in events
        ),
        "blocked_attempts": sum(event.get("status") == "blocked" for event in events),
        "cleanup_count": sum(
            isinstance(event.get("cleanup"), dict) for event in events
        ),
        "events": events,
    }


def record_preview_event(stats: Any, event: Any) -> None:
    safe = sanitize_preview_event(event)
    if not isinstance(stats, dict) or safe is None:
        return
    events = stats.setdefault("preview_readiness_events", [])
    if not isinstance(events, list):
        events = stats["preview_readiness_events"] = []
    events.append(safe)
    del events[:-MAX_PREVIEW_EVENTS]


class PreviewReadinessController:
    """Bound failed preview capability without halting unrelated closeout work."""

    def __init__(self, *, max_local_required: int = 2, max_native: int = 1):
        self.max_local_required = max(1, int(max_local_required))
        self.max_native = max(1, int(max_native))
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        self.attempts: list[_Attempt] = []
        self.sessions: dict[str, _Attempt] = {}
        self.failed_origins: set[str] = set()
        self.last_browser_origin = ""
        self.build_recoveries: set[str] = set()
        self.launcher_cache: dict[str, tuple[_Launcher, ...]] = {}

    def before_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        *,
        session_cwd: str,
        visual_required: bool,
    ) -> PreviewBlockDecision | None:
        args = args if isinstance(args, Mapping) else {}
        workdir = _workdir(args, session_cwd)
        root = _hash(str(workdir))
        launchers = self._launchers(workdir)
        recommendation = launchers[0].command if launchers else ""

        if tool_name == "terminal":
            classified = _classify_command(
                str(args.get("command") or ""),
                bool(args.get("background")),
                launchers,
            )
            if classified is None:
                return None
            kind, strategy, approved = classified
            failure = self._latest_failure(root)
            if kind == "build":
                if failure and (not visual_required or root in self.build_recoveries):
                    return self._block(
                        "preview_build_recovery_exhausted",
                        root,
                        strategy,
                        failure,
                        visual_required,
                        recommendation,
                    )
                return None
            prior = self._strategy_attempt(root, strategy)
            if prior and (
                prior.status == "running"
                or (prior.status == "failed" and prior.failure_class in _DETERMINISTIC)
            ):
                return self._block(
                    "preview_launcher_already_running"
                    if prior.status == "running"
                    else "preview_equivalent_retry_blocked",
                    root,
                    strategy,
                    failure,
                    visual_required,
                    recommendation,
                )
            if not failure:
                return None
            maximum = (
                self.max_native
                if approved
                else (self.max_local_required if visual_required else 1)
            )
            if self._used(root, approved=approved) < maximum:
                return None
            return self._block(
                "preview_native_budget_exhausted"
                if approved
                else "preview_local_budget_exhausted",
                root,
                strategy,
                failure,
                visual_required,
                recommendation,
            )

        if tool_name == "process":
            action = str(args.get("action") or "").lower()
            if action in {"kill", "list", "write", "submit", "close"}:
                return None
            attempt = self.sessions.get(str(args.get("session_id") or ""))
            if attempt and attempt.status == "failed":
                return self._block_for_attempt(
                    "preview_failed_process_observation_blocked",
                    attempt,
                    visual_required,
                    recommendation,
                )
            return None

        if tool_name.startswith("browser_"):
            origin = (
                _local_origin(args.get("url"))
                if tool_name == "browser_navigate"
                else self.last_browser_origin
            )
            if origin and origin in self.failed_origins:
                attempt = self._origin_attempt(origin) or _Attempt(
                    root,
                    "local_browser",
                    _hash(origin),
                    False,
                    origin=origin,
                    status="failed",
                    failure_class="application_bootstrap",
                )
                return self._block_for_attempt(
                    "preview_failed_browser_target_blocked",
                    attempt,
                    visual_required,
                    recommendation,
                )
        return None

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: Any,
        *,
        session_cwd: str,
        visual_required: bool,
    ) -> tuple[Any, dict[str, Any] | None]:
        args = args if isinstance(args, Mapping) else {}
        workdir = _workdir(args, session_cwd)
        root = _hash(str(workdir))
        launchers = self._launchers(workdir)
        recommendation = launchers[0].command if launchers else ""

        if tool_name == "terminal":
            command = str(args.get("command") or "")
            classified = _classify_command(
                command, bool(args.get("background")), launchers
            )
            if classified is None:
                return result, None
            kind, strategy, approved = classified
            if kind == "build":
                if self._latest_failure(root):
                    self.build_recoveries.add(root)
                return result, None
            data = _result_dict(result)
            attempt = _Attempt(
                root,
                strategy,
                _hash(f"{root}:{strategy}"),
                approved,
                origin=_command_origin(command) if args.get("background") else "",
                session_id=str(data.get("session_id") or ""),
            )
            self.attempts.append(attempt)
            if attempt.session_id:
                self.sessions[attempt.session_id] = attempt
            if attempt.origin:
                self.failed_origins.discard(attempt.origin)
            failure = classify_preview_failure(result)
            if failure:
                return self._observe_failure(
                    result,
                    attempt,
                    failure,
                    visual_required,
                    recommendation,
                    cleanup=bool(attempt.session_id and failure.deterministic),
                )
            if approved and data.get("exit_code") == 0 and not args.get("background"):
                attempt.status = "ready"
                event = self._event(
                    "ready",
                    attempt,
                    None,
                    "The repository-native QA launcher completed successfully. "
                    "This is readiness/verification evidence, not a visual-QA receipt.",
                    visual_required,
                    recommendation,
                )
                return _attach(result, event), event
            return result, None

        if tool_name == "process":
            attempt = self.sessions.get(str(args.get("session_id") or ""))
            failure = classify_preview_failure(result)
            if not attempt or not failure:
                return result, None
            return self._observe_failure(
                result,
                attempt,
                failure,
                visual_required,
                recommendation,
                cleanup=(
                    failure.deterministic
                    and str(args.get("action") or "").lower() != "kill"
                ),
            )

        if tool_name.startswith("browser_"):
            origin = (
                _local_origin(args.get("url"))
                if tool_name == "browser_navigate"
                else self.last_browser_origin
            )
            if tool_name == "browser_navigate" and origin:
                self.last_browser_origin = origin
            failure = classify_preview_failure(result)
            if not origin or not failure:
                return result, None
            attempt = self._origin_attempt(origin)
            if attempt is None:
                attempt = _Attempt(
                    root,
                    "local_browser",
                    _hash(origin),
                    False,
                    origin=origin,
                )
                self.attempts.append(attempt)
            return self._observe_failure(
                result,
                attempt,
                failure,
                visual_required,
                recommendation,
                cleanup=bool(attempt.session_id and failure.deterministic),
            )
        return result, None

    def _observe_failure(
        self,
        result: Any,
        attempt: _Attempt,
        failure: PreviewFailure,
        visual_required: bool,
        recommendation: str,
        *,
        cleanup: bool,
    ) -> tuple[Any, dict[str, Any]]:
        cleanup_event = None
        if cleanup:
            cleanup_event, cleanup_failure = _cleanup(attempt.session_id)
            if (
                failure.failure_class == "application_bootstrap"
                and cleanup_failure is not None
                and cleanup_failure.deterministic
            ):
                failure = cleanup_failure
        attempt.status = "failed" if failure.deterministic else "unavailable"
        attempt.failure_class = failure.failure_class
        if failure.deterministic and attempt.origin:
            self.failed_origins.add(attempt.origin)
        summary = failure.summary
        if failure.deterministic:
            summary += (
                " Required visual QA remains pending; this failed preview cannot satisfy it."
                if visual_required
                else " Visual QA is not required for this turn, so reuse this evidence "
                "and continue verification/closeout."
            )
        event = self._event(
            "failed" if failure.deterministic else "unavailable",
            attempt,
            failure,
            summary,
            visual_required,
            recommendation,
            cleanup=cleanup_event,
        )
        return _attach(result, event), event

    def _block_for_attempt(
        self,
        code: str,
        attempt: _Attempt,
        visual_required: bool,
        recommendation: str,
    ) -> PreviewBlockDecision:
        return self._block(
            code,
            attempt.root,
            attempt.strategy,
            PreviewFailure(
                attempt.failure_class or "application_bootstrap",
                True,
                _SUMMARIES.get(
                    attempt.failure_class,
                    "The preview already failed deterministically.",
                ),
            ),
            visual_required,
            recommendation,
        )

    def _block(
        self,
        code: str,
        root: str,
        strategy: str,
        failure: PreviewFailure | None,
        visual_required: bool,
        recommendation: str,
    ) -> PreviewBlockDecision:
        message = (
            "Preview recovery is bounded and this launcher path is exhausted. "
            + (
                "Required visual QA remains pending; use one materially different "
                "repository-native launcher if recommended, otherwise report the blocker."
                if visual_required
                else "Visual QA is not required for this turn; do not keep rebuilding, "
                "relaunching, navigating, or polling this failed local preview. "
                "Continue verification and closeout."
            )
        )
        if recommendation:
            message += (
                f" Repo-native launcher still available once: `{recommendation}`."
            )
        attempt = _Attempt(root, strategy, _hash(f"{root}:{strategy}"), False)
        event = self._event(
            "blocked",
            attempt,
            failure,
            message,
            visual_required,
            recommendation,
        )
        return PreviewBlockDecision(code, message, event)

    def _event(
        self,
        status: str,
        attempt: _Attempt,
        failure: PreviewFailure | None,
        summary: str,
        visual_required: bool,
        recommendation: str,
        *,
        cleanup: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        event = {
            "status": status,
            "deterministic": bool(failure and failure.deterministic),
            "failure_class": failure.failure_class if failure else "",
            "strategy": attempt.strategy,
            "strategy_hash": attempt.strategy_hash,
            "summary": summary,
            "recommended_launcher": recommendation,
            "budget": {
                "local_used": self._used(attempt.root, approved=False),
                "local_max": self.max_local_required if visual_required else 1,
                "native_used": self._used(attempt.root, approved=True),
                "native_max": self.max_native,
            },
        }
        if cleanup:
            event["cleanup"] = cleanup
        return sanitize_preview_event(event) or {
            "status": status,
            "deterministic": bool(failure and failure.deterministic),
        }

    def _latest_failure(self, root: str) -> PreviewFailure | None:
        for attempt in reversed(self.attempts):
            if (
                attempt.root == root
                and attempt.status == "failed"
                and attempt.failure_class in _DETERMINISTIC
            ):
                return PreviewFailure(
                    attempt.failure_class,
                    True,
                    _SUMMARIES[attempt.failure_class],
                )
        return None

    def _strategy_attempt(self, root: str, strategy: str) -> _Attempt | None:
        return next(
            (
                attempt
                for attempt in reversed(self.attempts)
                if attempt.root == root and attempt.strategy == strategy
            ),
            None,
        )

    def _origin_attempt(self, origin: str) -> _Attempt | None:
        return next(
            (
                attempt
                for attempt in reversed(self.attempts)
                if attempt.origin == origin
            ),
            None,
        )

    def _used(self, root: str, *, approved: bool) -> int:
        return len({
            attempt.strategy_hash
            for attempt in self.attempts
            if attempt.root == root
            and attempt.approved is approved
            and attempt.status != "unavailable"
            and attempt.strategy != "local_browser"
        })

    def _launchers(self, workdir: Path) -> tuple[_Launcher, ...]:
        key = str(workdir)
        if key not in self.launcher_cache:
            self.launcher_cache[key] = _discover_launchers(workdir)
        return self.launcher_cache[key]


def preview_block_result(decision: PreviewBlockDecision) -> str:
    return json.dumps(
        {
            "error": decision.message,
            "preview_readiness": {"code": decision.code, **decision.evidence},
        },
        ensure_ascii=False,
    )


def _classify_command(
    command: str, background: bool, launchers: tuple[_Launcher, ...]
) -> tuple[str, str, bool] | None:
    tokens = _tokens(command)
    token_set = set(tokens)
    for launcher in launchers:
        if launcher.script in token_set:
            return "native", f"repo_native_{launcher.script.replace(':', '_')}", True
    if background and token_set & {
        "pnpm",
        "npm",
        "yarn",
        "bun",
        "vite",
        "next",
        "webpack",
    }:
        if "hermes_cli.worker_frontend_smoke" in command.lower():
            return "local", "managed_frontend_smoke", False
        family = next(
            (
                name
                for name in ("preview", "dev", "serve", "start")
                if name in token_set
            ),
            "",
        )
        if family:
            wrappers = [token for token in tokens if token.startswith("qa:")]
            suffix = f"_{wrappers[0].replace(':', '_')}" if wrappers else ""
            return "local", f"local_{family}{suffix}", False
    if (
        not background
        and "build" in token_set
        and token_set & {"pnpm", "npm", "yarn", "bun", "vite"}
    ):
        return "build", "frontend_build_recovery", False
    return None


def _discover_launchers(workdir: Path) -> tuple[_Launcher, ...]:
    packages = [workdir / "package.json"]
    try:
        packages.extend(
            child / "package.json"
            for child in sorted(workdir.iterdir(), key=lambda path: path.name)[:24]
            if child.is_dir()
            and not child.name.startswith(".")
            and child.name != "node_modules"
        )
    except OSError:
        pass
    launchers = []
    for package in packages:
        try:
            if not package.is_file() or package.stat().st_size > 262_144:
                continue
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        scripts = data.get("scripts") if isinstance(data, dict) else None
        script = next(
            (
                name
                for name in _APPROVED_SCRIPTS
                if isinstance(scripts, dict) and name in scripts
            ),
            "",
        )
        if not script:
            continue
        relative = os.path.relpath(package.parent, workdir)
        manager = _package_manager(workdir, package.parent, data)
        if manager == "pnpm":
            command = (
                f"pnpm {script}"
                if relative == "."
                else f"pnpm --dir {shlex.quote(relative)} {script}"
            )
        elif manager == "yarn":
            command = (
                f"yarn {script}"
                if relative == "."
                else f"yarn --cwd {shlex.quote(relative)} {script}"
            )
        else:
            command = (
                f"npm run {script}"
                if relative == "."
                else f"npm --prefix {shlex.quote(relative)} run {script}"
            )
        launchers.append(_Launcher(script, command))
    return tuple(launchers[:4])


def _package_manager(root: Path, package_dir: Path, data: Mapping[str, Any]) -> str:
    configured = str(data.get("packageManager") or "").split("@", 1)[0].lower()
    if configured in {"pnpm", "yarn", "npm"}:
        return configured
    for base in (package_dir, root):
        if (base / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (base / "yarn.lock").exists():
            return "yarn"
    return "npm"


def _cleanup(session_id: str) -> tuple[dict[str, str], PreviewFailure | None]:
    failure = None
    try:
        from tools.process_registry import process_registry

        result = process_registry.kill_process(
            session_id,
            source="preview_readiness",
            consume_output=False,
            suppress_completion=True,
        )
        status = _token(
            result.get("status") if isinstance(result, dict) else "error", 32
        )
        failure = classify_preview_failure(result)
    except Exception:
        status = "cleanup_error"
    return (
        {"status": status or "cleanup_error", "session_ref": _hash(session_id)},
        failure,
    )


def _attach(result: Any, event: dict[str, Any]) -> Any:
    safe = sanitize_preview_event(event)
    if safe is None:
        return result
    data = _result_dict(result)
    if data:
        return json.dumps({**data, "preview_readiness": safe}, ensure_ascii=False)
    if isinstance(result, str):
        return f"{result}\n\n[Preview readiness evidence: {json.dumps(safe, ensure_ascii=False)}]"
    return result


def _result_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    if not isinstance(result, str):
        return {}
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result[:64_000]
    if isinstance(result, Mapping):
        return "\n".join(
            str(result.get(key) or "")
            for key in (
                "output",
                "output_preview",
                "error",
                "message",
                "title",
                "snapshot",
            )
        )[:64_000]
    return str(result or "")[:64_000]


def _tokens(command: str) -> list[str]:
    try:
        return [token.lower() for token in shlex.split(command)[:256]]
    except ValueError:
        return re.findall(r"[A-Za-z0-9_.:@/-]+", command.lower())[:256]


def _command_origin(command: str) -> str:
    tokens = _tokens(command)
    host, port = "127.0.0.1", "5173"
    for index, token in enumerate(tokens):
        if token == "--host" and index + 1 < len(tokens):
            host = tokens[index + 1]
        elif token.startswith("--host="):
            host = token.split("=", 1)[1]
        elif token == "--port" and index + 1 < len(tokens):
            port = tokens[index + 1]
        elif token.startswith("--port="):
            port = token.split("=", 1)[1]
    if host in {"0.0.0.0", "::", "[::]", "localhost"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _local_origin(value: Any) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        host, port = parsed.hostname or "", parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not host or not _local_host(host):
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host.lower()}:{port or default_port}"


def _local_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or "." not in host:
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return host.endswith((".local", ".internal", ".lan", ".home"))


def _workdir(args: Mapping[str, Any], session_cwd: str) -> Path:
    return (
        Path(str(args.get("workdir") or session_cwd or os.getcwd()))
        .expanduser()
        .resolve(strict=False)
    )


def _safe_summary(value: Any) -> str:
    text = redact_sensitive_text(
        " ".join(str(value or "").split()),
        force=True,
        redact_url_credentials=True,
    )
    text = re.sub(
        r"(?<![\w:/.-])(?:~[\w.-]*(?:/[^\s\"'<>),;{}\[\]]*)?|"
        r"/(?:home|Users|tmp|var|etc|opt|private|workspace|workspaces|mnt|srv|repo|root)"
        r"(?:/[^\s\"'<>),;{}\[\]]*)?|[A-Za-z]:\\[^\s\"'<>),;{}\[\]]+)",
        "[path]",
        text,
    )
    return text[:240]


def _safe_launcher(value: Any) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > 240
        or any(ord(char) < 32 for char in text)
        or re.search(r"(?i)(password|secret|token|api[_-]?key)\s*=", text)
    ):
        return ""
    return text


def _token(value: Any, limit: int) -> str:
    return "".join(
        char
        for char in str(value or "").strip().lower()
        if char.isalnum() or char in {"_", "-", "."}
    )[:limit]


def _safe_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[:16] if re.fullmatch(r"[0-9a-f]{8,64}", text) else ""


def _bounded_int(value: Any, maximum: int) -> int:
    try:
        return min(maximum, max(0, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]


__all__ = [
    "MAX_PREVIEW_EVENTS",
    "PreviewBlockDecision",
    "PreviewFailure",
    "PreviewReadinessController",
    "classify_preview_failure",
    "preview_block_result",
    "record_preview_event",
    "sanitize_preview_event",
    "summarize_preview_events",
]

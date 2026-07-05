"""Lightweight verification evidence classification and claim gating."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


_DB_LOCK = threading.Lock()
_MAX_OUTPUT_SUMMARY_CHARS = 2000
_MAX_EVIDENCE_AGE_DAYS = 30
_MAX_EVENTS_PER_SESSION_ROOT = 100
_MAX_TOTAL_UNREFERENCED_EVENTS = 10_000
_AD_HOC_SCRIPT_NAME_PREFIXES = ("hermes-verify-", "hermes-ad-hoc-")
_VERIFY_SCHEMA_VERSION = 1
_SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;)\s*")


@dataclass(frozen=True)
class VerificationEvidence:
    command: str
    canonical_command: str
    kind: str
    scope: str
    status: str
    exit_code: int
    cwd: str
    root: str
    session_id: str
    output_summary: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retention_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=_MAX_EVIDENCE_AGE_DAYS)).isoformat()


def _db_path() -> Path:
    return get_hermes_home() / "verification_evidence.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            session_id TEXT NOT NULL,
            cwd TEXT NOT NULL,
            root TEXT NOT NULL,
            command TEXT NOT NULL,
            canonical_command TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            output_summary TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_state (
            session_id TEXT NOT NULL,
            root TEXT NOT NULL,
            last_event_id INTEGER,
            last_edit_at TEXT,
            changed_paths_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (session_id, root)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_verification_events_session_root
        ON verification_events(session_id, root, id DESC)
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(_VERIFY_SCHEMA_VERSION),),
    )
    conn.commit()


_VERIFY_COMMAND_RE = re.compile(
    r"\b(pytest|vitest|playwright|chromium|browser|smoke|check|status|ci|deploy|deployed|"
    r"production|prod|preview|modal|health|run_tests\.sh)\b|"
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:[^\s;&|]*[-:]?)?(?:test|tests|verify|verification)\b|"
    r"\b(?:cargo|go)\s+test\b|"
    r"(?:^|[\s;&|])(?:\./)?(?:scripts/)?(?:test|tests|run_tests)\.sh\b|"
    r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+\S*(?:verify|verification)\b",
    re.IGNORECASE,
)
_PROTECTED_CHECKOUT_GUARDRAIL_RE = re.compile(
    r"\bBLOCKED:\s*refusing to run a non-read-only terminal command from a protected canonical checkout\b",
    re.IGNORECASE,
)
_BROWSER_RE = re.compile(r"\b(browser|playwright|chromium|chrome|modal)\b", re.IGNORECASE)
_PRODUCTION_RE = re.compile(
    r"\b(production|prod|deployed?|live)\b|"
    r"https?://(?!(?:127(?:\.\d{1,3}){3}|localhost|0\.0\.0\.0|\[::1\])(?::|/|$))",
    re.IGNORECASE,
)
_CI_RE = re.compile(r"\b(ci|checks?|status|gh\s+pr\s+checks|test|tests|pytest|vitest)\b", re.IGNORECASE)
_CI_COMMAND_RE = re.compile(
    r"\b(?:ci|checks?|status|gh\s+pr\s+checks|pytest|vitest)\b|"
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:[^\s;&|]*[-:]?)?(?:test|tests|check|verify|verification)\b|"
    r"\b(?:cargo|go)\s+test\b|"
    r"(?:^|[\s;&|])(?:\./)?(?:scripts/)?(?:test|tests|run_tests)\.sh\b|"
    r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+\S*(?:verify|verification)\b",
    re.IGNORECASE,
)
_DEPLOY_RE = re.compile(r"\b(deploy|deployed|deployment)\b", re.IGNORECASE)
_MERGE_RE = re.compile(r"\b(merge|merged|pull|pr)\b", re.IGNORECASE)
_SUCCESS_RE = re.compile(r"\b(success|passed|pass|ok|complete|completed|visible|found|healthy)\b", re.IGNORECASE)
_TIMEOUT_RE = re.compile(r"\b(timed?\s*out|timeout|deadline|expired)\b", re.IGNORECASE)
_SHELL_SEGMENT_RE = re.compile(r"\s*(?:&&|\|\||[;\n])\s*")
_GIT_OPTION_ARGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
_NON_VERIFY_GIT_PATHSPEC_COMMANDS = {"add", "rm", "mv", "restore", "checkout", "reset"}

_CLAIM_WORD_RE = re.compile(r"\b(shipped|verified|visible|checked|confirmed|passed|deployed|merged)\b", re.IGNORECASE)
_NEGATED_CLAIM_RE = re.compile(r"\b(?:not|isn['’]?t|failed|failure|blocked|unverified|not_verified)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|\b(?:and|but)\b)\s*", re.IGNORECASE)

_SURFACE_LABELS = {
    "browser": "browser verification",
    "production": "production verification",
    "production_browser": "production browser verification",
    "ci": "CI verification",
    "deployment": "deployment verification",
    "pr": "PR/merge verification",
    "verification": "verification",
}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _surfaces_for(tool_name: str, check_name: str, detail: str) -> list[str]:
    haystack = f"{tool_name} {check_name} {detail}"
    surfaces: list[str] = []
    if _CI_COMMAND_RE.search(check_name) or re.search(
        r"\b(?:ci|checks?|status\s+checks?|gh\s+pr\s+checks)\b",
        detail,
        flags=re.IGNORECASE,
    ):
        surfaces.append("ci")
    if _MERGE_RE.search(haystack):
        surfaces.append("pr")
    if _DEPLOY_RE.search(haystack):
        surfaces.append("deployment")
    if _BROWSER_RE.search(haystack):
        surfaces.append("browser")
    if _PRODUCTION_RE.search(haystack):
        surfaces.append("production")
    if "browser" in surfaces and "production" in surfaces:
        surfaces.append("production_browser")
    return surfaces or ["verification"]


def _normalized_evidence_surfaces(item: dict[str, Any]) -> list[str]:
    surface = str(item.get("surface") or "").strip()
    if surface != "ci":
        return [surface] if surface else []
    check_name = str(item.get("check_name") or "")
    detail = str(item.get("detail") or "")
    haystack = f"{check_name}\n{detail}"
    if _BROWSER_RE.search(haystack) and not _CI_COMMAND_RE.search(check_name):
        # Older ledgers could mislabel ad-hoc Playwright/Chromium browser probes
        # as CI solely because the script imported "@playwright/test". Do not let
        # that stale label contradict a later independent CI claim. New evidence
        # recording classifies these probes as browser evidence directly.
        return []
    return ["ci"]


def _is_non_verification_git_pathspec_segment(segment: str) -> bool:
    try:
        parts = shlex.split(segment)
    except ValueError:
        parts = segment.split()
    if not parts or parts[0] != "git":
        return False

    index = 1
    while index < len(parts):
        token = parts[index]
        if token in _GIT_OPTION_ARGS:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _GIT_OPTION_ARGS):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        break

    if index >= len(parts):
        return False
    return parts[index] in _NON_VERIFY_GIT_PATHSPEC_COMMANDS


def _split_segment_tokens(command: str) -> list[list[str]]:
    segments: list[list[str]] = []
    for segment in _SHELL_SPLIT_RE.split(command.strip()):
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if tokens:
            segments.append(tokens)
    return segments


def _clean_token(token: str) -> str:
    token = token.strip()
    while token.startswith("./"):
        token = token[2:]
    return token


def _canonical_tokens(canonical: str) -> list[str]:
    try:
        return [_clean_token(t) for t in shlex.split(canonical) if t]
    except ValueError:
        return []


def _strip_command_prefix(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    if remaining and remaining[0] == "env":
        remaining = remaining[1:]
    while remaining and "=" in remaining[0] and not remaining[0].startswith("-"):
        remaining = remaining[1:]
    while remaining and remaining[0] in {"command", "time", "noglob"}:
        remaining = remaining[1:]
    return remaining


def _equivalent_needles(needle: list[str]) -> list[list[str]]:
    candidates = [needle]
    if len(needle) >= 3 and needle[1] == "run":
        package_manager = needle[0]
        script_name = needle[2]
        if package_manager in {"npm", "pnpm", "yarn", "bun"}:
            candidates.append([package_manager, script_name])
    if len(needle) == 1 and "/" in needle[0]:
        candidates.extend([["bash", needle[0]], ["sh", needle[0]]])
    if needle == ["pytest"]:
        candidates.extend(
            [
                ["python", "-m", "pytest"],
                ["python3", "-m", "pytest"],
                ["uv", "run", "pytest"],
                ["poetry", "run", "pytest"],
                ["pipenv", "run", "pytest"],
            ]
        )
    return candidates


def _find_canonical_match(command: str, canonical_commands: list[str]) -> tuple[str, list[str]] | None:
    segments = _split_segment_tokens(command)
    for canonical in canonical_commands:
        needle = _canonical_tokens(canonical)
        if not needle:
            continue
        for tokens in segments:
            candidate_tokens = _strip_command_prefix(tokens)
            for candidate in _equivalent_needles(needle):
                if candidate_tokens[:len(candidate)] == candidate:
                    return canonical, candidate_tokens[len(candidate):]
    return None


def _kind_for_command(canonical: str) -> str:
    lowered = canonical.lower()
    if any(word in lowered for word in ("lint", "eslint", "ruff")):
        return "lint"
    if any(word in lowered for word in ("typecheck", "tsc", "mypy", "pyright", "ty")):
        return "typecheck"
    if "build" in lowered:
        return "build"
    if "fmt" in lowered or "format" in lowered:
        return "format"
    if "check" in lowered and "test" not in lowered:
        return "check"
    return "test"


def _looks_like_target(arg: str) -> bool:
    if not arg or arg.startswith("-") or "=" in arg:
        return False
    return (
        "/" in arg
        or "\\" in arg
        or "::" in arg
        or arg.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java"))
        or arg.startswith(("test_", "tests", "spec", "__tests__"))
    )


def _scope_for_args(args: list[str]) -> str:
    return "targeted" if any(_looks_like_target(arg) for arg in args) else "full"


def _is_under_temp_dir(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    try:
        path = Path(token).expanduser()
        if not path.is_absolute():
            return False
        resolved = path.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        return resolved == temp_root or temp_root in resolved.parents
    except Exception:
        return False


def _is_under_root(token: str, root: str | Path | None) -> bool:
    if not root:
        return False
    try:
        path = Path(token).expanduser().resolve()
        root_path = Path(root).expanduser().resolve()
        return path == root_path or root_path in path.parents
    except Exception:
        return False


def _is_temp_script_path(token: str, root: str | Path | None) -> bool:
    try:
        name = Path(token).expanduser().name
    except Exception:
        return False
    return (
        name.startswith(_AD_HOC_SCRIPT_NAME_PREFIXES)
        and _is_under_temp_dir(token)
        and not _is_under_root(token, root)
    )


def _ad_hoc_script_args(tokens: list[str], root: str | Path | None) -> list[str] | None:
    candidate_tokens = _strip_command_prefix(tokens)
    if not candidate_tokens:
        return None
    command = candidate_tokens[0]
    if _is_temp_script_path(command, root):
        return candidate_tokens[1:]
    if command in {"python", "python3", "node", "bash", "sh", "ruby", "perl"}:
        for idx, token in enumerate(candidate_tokens[1:], start=1):
            if token == "--":
                continue
            if _is_temp_script_path(token, root):
                return candidate_tokens[idx + 1:]
            if not token.startswith("-"):
                return None
    return None


def _find_ad_hoc_match(command: str, root: str | Path | None) -> list[str] | None:
    for tokens in _split_segment_tokens(command):
        trailing_args = _ad_hoc_script_args(tokens, root)
        if trailing_args is not None:
            return trailing_args
    return None


def _summarize_output(output: str) -> str:
    text = (output or "").strip()
    if len(text) <= _MAX_OUTPUT_SUMMARY_CHARS:
        return text
    head = _MAX_OUTPUT_SUMMARY_CHARS // 3
    tail = _MAX_OUTPUT_SUMMARY_CHARS - head
    return text[:head] + f"\n... [{len(text) - _MAX_OUTPUT_SUMMARY_CHARS} chars omitted] ...\n" + text[-tail:]


def _prune_old_events(conn: sqlite3.Connection, *, session_id: str, root: str) -> None:
    cutoff = _retention_cutoff()
    conn.execute(
        """
        DELETE FROM verification_events
        WHERE session_id = ? AND root = ?
          AND id NOT IN (
              SELECT id FROM verification_events
              WHERE session_id = ? AND root = ?
              ORDER BY id DESC LIMIT ?
          )
        """,
        (session_id, root, session_id, root, _MAX_EVENTS_PER_SESSION_ROOT),
    )
    conn.execute(
        """
        DELETE FROM verification_events
        WHERE created_at < ?
          AND id NOT IN (
              SELECT last_event_id FROM verification_state
              WHERE last_event_id IS NOT NULL
          )
        """,
        (cutoff,),
    )


def classify_verification_command(
    command: str,
    *,
    cwd: str | Path | None = None,
    session_id: str | None = None,
    exit_code: int = 0,
    output: str = "",
) -> VerificationEvidence | None:
    if not command or not isinstance(command, str):
        return None
    try:
        from agent.coding_context import project_facts_for

        facts = project_facts_for(cwd)
    except Exception:
        facts = None
    if not facts:
        return None

    verify_commands = list(facts.get("verifyCommands") or [])
    match = _find_canonical_match(command, verify_commands)
    is_ad_hoc = False
    if match is None and not verify_commands:
        ad_hoc_args = _find_ad_hoc_match(command, facts.get("root"))
        if ad_hoc_args is not None:
            match = ("ad-hoc verification script", ad_hoc_args)
            is_ad_hoc = True
    if match is None:
        return None

    canonical, trailing_args = match
    return VerificationEvidence(
        command=command,
        canonical_command=canonical,
        kind="ad_hoc" if is_ad_hoc else _kind_for_command(canonical),
        scope="targeted" if is_ad_hoc else _scope_for_args(trailing_args),
        status="passed" if int(exit_code) == 0 else "failed",
        exit_code=int(exit_code),
        cwd=str(Path(cwd or ".").resolve()),
        root=str(facts.get("root") or Path(cwd or ".").resolve()),
        session_id=str(session_id or "default"),
        output_summary=_summarize_output(output),
    )


def _terminal_command_looks_like_verification(command: str) -> bool:
    for segment in _SHELL_SEGMENT_RE.split(command):
        segment = segment.strip()
        if not segment or _is_non_verification_git_pathspec_segment(segment):
            continue
        if _VERIFY_COMMAND_RE.search(segment):
            return True
    return False


def classify_tool_verification_evidence(
    tool_name: str,
    tool_args: dict[str, Any] | None,
    result: Any,
    is_error: bool,
    *,
    order: int | None = None,
) -> list[dict[str, Any]]:
    """Return normalized verification evidence emitted by an explicit check.

    The classifier is intentionally conservative: terminal failures are only
    captured when the command itself looks like a verification/status/smoke
    attempt, while browser tool failures are inherently browser evidence.
    """
    name = str(tool_name or "")
    args = tool_args if isinstance(tool_args, dict) else {}
    data = _json_object(result)
    result_text = _text(data.get("output") or data.get("error") or result)
    check_name = _text(args.get("command") or args.get("url") or args.get("route") or name, limit=160)

    if name == "terminal" and _PROTECTED_CHECKOUT_GUARDRAIL_RE.search(result_text):
        return []

    if name == "terminal":
        if not _terminal_command_looks_like_verification(check_name):
            return []
    elif not name.startswith("browser") and name not in {"webfetch", "web_search"}:
        return []

    timed_out = bool(_TIMEOUT_RE.search(f"{check_name}\n{result_text}"))
    status = "timeout" if timed_out else ("failure" if is_error else "success")
    if not is_error and name != "terminal" and data:
        if data.get("success") is False or data.get("ok") is False:
            status = "timeout" if timed_out else "failure"
        elif data.get("success") is True or data.get("ok") is True:
            status = "success"
    if status == "success" and result_text and _TIMEOUT_RE.search(result_text):
        status = "timeout"

    surfaces = _surfaces_for(name, check_name, result_text)
    return [
        {
            "schema_version": 1,
            "surface": surface,
            "check_name": check_name or name,
            "status": status,
            "order": int(order or 0),
            "detail": result_text[:240],
        }
        for surface in surfaces
    ]


def latest_evidence_by_surface(evidence: Any) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not isinstance(evidence, list):
        return latest
    for item in evidence:
        if not isinstance(item, dict):
            continue
        surfaces = _normalized_evidence_surfaces(item)
        if not surfaces:
            continue
        order = int(item.get("order") or 0)
        for surface in surfaces:
            current = latest.get(surface)
            if current is None or order >= int(current.get("order") or 0):
                normalized_item = dict(item)
                normalized_item["surface"] = surface
                latest[surface] = normalized_item
    return latest


def evidence_from_runtime_breakdown(runtime_breakdown: Any) -> list[dict[str, Any]]:
    if not isinstance(runtime_breakdown, dict):
        return []
    evidence = runtime_breakdown.get("verification_evidence")
    if isinstance(evidence, list):
        return [item for item in evidence if isinstance(item, dict)]
    return []


def record_terminal_result(
    *,
    command: str,
    cwd: str | Path | None,
    session_id: str | None,
    exit_code: int,
    output: str = "",
) -> dict[str, Any] | None:
    evidence = classify_verification_command(
        command,
        cwd=cwd,
        session_id=session_id,
        exit_code=exit_code,
        output=output,
    )
    if evidence is None:
        return None

    created_at = _utc_now()
    with _DB_LOCK:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO verification_events(
                    created_at, session_id, cwd, root, command, canonical_command,
                    kind, scope, status, exit_code, output_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    evidence.session_id,
                    evidence.cwd,
                    evidence.root,
                    evidence.command,
                    evidence.canonical_command,
                    evidence.kind,
                    evidence.scope,
                    evidence.status,
                    evidence.exit_code,
                    evidence.output_summary,
                ),
            )
            if cur.lastrowid is None:
                raise RuntimeError("verification event insert did not return an id")
            event_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO verification_state(
                    session_id, root, last_event_id, last_edit_at, changed_paths_json
                ) VALUES (?, ?, ?, NULL, '[]')
                ON CONFLICT(session_id, root) DO UPDATE SET
                    last_event_id = excluded.last_event_id,
                    last_edit_at = NULL,
                    changed_paths_json = '[]'
                """,
                (evidence.session_id, evidence.root, event_id),
            )
            _prune_old_events(conn, session_id=evidence.session_id, root=evidence.root)
            conn.commit()

    return {"id": event_id, **evidence.__dict__, "created_at": created_at}


def mark_workspace_edited(
    *,
    session_id: str | None,
    cwd: str | Path | None,
    paths: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, Any] | None:
    """Mark a workspace's verification evidence stale after a landed edit."""
    try:
        from agent.coding_context import project_facts_for

        facts = project_facts_for(cwd)
    except Exception:
        facts = None
    if not facts:
        return None

    sid = str(session_id or "default")
    root = str(facts.get("root") or Path(cwd or ".").resolve())
    changed_paths = sorted({str(p) for p in (paths or []) if p})
    edited_at = _utc_now()

    with _DB_LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT changed_paths_json FROM verification_state
                WHERE session_id = ? AND root = ?
                """,
                (sid, root),
            ).fetchone()
            existing: set[str] = set()
            if row is not None:
                try:
                    existing = set(json.loads(row["changed_paths_json"] or "[]"))
                except (TypeError, ValueError):
                    existing = set()
            merged = sorted((existing | set(changed_paths)))[-200:]
            conn.execute(
                """
                INSERT INTO verification_state(
                    session_id, root, last_event_id, last_edit_at, changed_paths_json
                ) VALUES (?, ?, NULL, ?, ?)
                ON CONFLICT(session_id, root) DO UPDATE SET
                    last_edit_at = excluded.last_edit_at,
                    changed_paths_json = excluded.changed_paths_json
                """,
                (sid, root, edited_at, json.dumps(merged)),
            )
            conn.commit()

    return {
        "session_id": sid,
        "root": root,
        "last_edit_at": edited_at,
        "changed_paths": changed_paths,
    }


def verification_status(
    *,
    session_id: str | None,
    cwd: str | Path | None,
) -> dict[str, Any]:
    try:
        from agent.coding_context import project_facts_for

        facts = project_facts_for(cwd)
    except Exception:
        facts = None
    if not facts:
        return {"status": "not_applicable", "evidence": None}

    sid = str(session_id or "default")
    root = str(facts.get("root") or Path(cwd or ".").resolve())
    with _DB_LOCK:
        with _connect() as conn:
            state = conn.execute(
                """
                SELECT last_event_id, last_edit_at, changed_paths_json
                FROM verification_state
                WHERE session_id = ? AND root = ?
                """,
                (sid, root),
            ).fetchone()
            if state is None:
                return {
                    "status": "unverified",
                    "evidence": None,
                    "root": root,
                    "session_id": sid,
                    "changed_paths": [],
                }
            event = None
            if state["last_event_id"] is not None:
                event = conn.execute(
                    "SELECT * FROM verification_events WHERE id = ?",
                    (state["last_event_id"],),
                ).fetchone()

    changed_paths: list[str] = []
    try:
        changed_paths = json.loads(state["changed_paths_json"] or "[]")
    except (TypeError, ValueError):
        changed_paths = []

    if event is None:
        return {
            "status": "unverified",
            "evidence": None,
            "root": root,
            "session_id": sid,
            "changed_paths": changed_paths,
        }

    evidence = dict(event)
    status = "stale" if state["last_edit_at"] and state["last_edit_at"] > evidence["created_at"] else evidence["status"]
    return {
        "status": status,
        "evidence": evidence,
        "root": root,
        "session_id": sid,
        "changed_paths": changed_paths,
    }


def _surface_claimed(text: str, surface: str) -> bool:
    relevant_text = text
    if surface in {"browser", "production", "production_browser", "deployment"}:
        sentences = [part for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
        relevant = [part for part in sentences if _surface_terms_present(part, surface)]
        if relevant:
            relevant_text = " ".join(relevant)
    claim_match = _CLAIM_WORD_RE.search(relevant_text)
    if not claim_match:
        return False
    prefix = relevant_text[max(0, claim_match.start() - 80) : claim_match.start()]
    if _NEGATED_CLAIM_RE.search(prefix):
        return False
    lowered = relevant_text.lower()
    if surface == "production_browser":
        return bool(_PRODUCTION_RE.search(relevant_text) and _BROWSER_RE.search(relevant_text))
    if surface == "browser":
        return bool(_BROWSER_RE.search(relevant_text) or "modal" in lowered or "visible" in lowered)
    if surface == "production":
        return bool(_PRODUCTION_RE.search(relevant_text))
    if surface == "ci":
        return bool(_CI_RE.search(relevant_text))
    if surface == "deployment":
        return bool(_DEPLOY_RE.search(relevant_text))
    if surface == "pr":
        return bool(_MERGE_RE.search(relevant_text))
    return bool(_CLAIM_WORD_RE.search(relevant_text))


def _surface_terms_present(text: str, surface: str) -> bool:
    lowered = text.lower()
    if surface == "production_browser":
        return bool(_PRODUCTION_RE.search(text) and (_BROWSER_RE.search(text) or "modal" in lowered))
    if surface == "browser":
        return bool(_BROWSER_RE.search(text) or "modal" in lowered or "visible" in lowered)
    if surface == "production":
        return bool(_PRODUCTION_RE.search(text))
    if surface == "deployment":
        return bool(_DEPLOY_RE.search(text))
    return True


def _clause_mentions_blocked_surface(text: str, surface: str) -> bool:
    lowered = text.lower()
    if surface == "production_browser":
        return bool(_PRODUCTION_RE.search(text) or _BROWSER_RE.search(text) or "modal" in lowered or "visible" in lowered)
    return _surface_terms_present(text, surface)


def _surface_downgraded(text: str, surface: str, item: dict[str, Any]) -> bool:
    label = _SURFACE_LABELS.get(surface, surface.replace("_", " "))
    check = str(item.get("check_name") or "").strip()
    downgrade_lines = [line for line in str(text or "").splitlines() if "verification downgrade" in line.lower()]
    for line in downgrade_lines:
        lowered = line.lower()
        if label.lower() not in lowered and not _surface_terms_present(line, surface):
            continue
        if not _NEGATED_CLAIM_RE.search(line):
            continue
        if check and check[:80].lower() not in lowered:
            continue
        return True
    return False


def claim_constraints_for_text(final_text: str, evidence: Any) -> dict[str, Any]:
    latest = latest_evidence_by_surface(evidence)
    blocked = []
    for surface, item in sorted(latest.items()):
        status = str(item.get("status") or "").lower()
        if status not in {"failure", "timeout"}:
            continue
        if _surface_claimed(final_text, surface):
            blocked.append(
                {
                    "surface": surface,
                    "status": status,
                    "check_name": str(item.get("check_name") or "verification"),
                    "detail": str(item.get("detail") or "")[:240],
                }
            )
    return {
        "allowed": not blocked,
        "blocked_surfaces": blocked,
        "latest_by_surface": latest,
    }


def _blocked_surface_clause(item: dict[str, Any]) -> str:
    surface = str(item.get("surface") or "verification")
    label = _SURFACE_LABELS.get(surface, surface.replace("_", " ") + " verification")
    status = str(item.get("status") or "failed").lower()
    check = str(item.get("check_name") or "verification")
    if len(check) > 180:
        check = check[:177].rstrip() + "..."
    return f"{label} is not verified: latest check `{check}` {status}."


def _rewrite_blocked_surface_claims(final_text: str, blocked: list[dict[str, Any]], downgrade: str) -> str:
    sentences = [part for part in _SENTENCE_SPLIT_RE.split(str(final_text or "")) if part.strip()]
    if not sentences:
        return downgrade

    rewritten: list[str] = []
    inserted = False
    for sentence in sentences:
        sentence = sentence.strip()
        blocked_surfaces = [
            str(item.get("surface") or "")
            for item in blocked
            if isinstance(item, dict) and _surface_claimed(sentence, str(item.get("surface") or ""))
        ]
        if not blocked_surfaces:
            rewritten.append(sentence)
            continue

        clauses = [part.strip() for part in _CLAUSE_SPLIT_RE.split(sentence) if part.strip()]
        kept = []
        for clause in clauses:
            if any(_clause_mentions_blocked_surface(clause, surface) for surface in blocked_surfaces):
                continue
            kept.append(clause.rstrip(".!?"))
        if kept:
            rewritten.append(". ".join(kept) + ".")
        if not inserted:
            rewritten.append(downgrade)
            inserted = True

    if not inserted:
        rewritten.append(downgrade)
    return "\n\n".join(part for part in rewritten if part.strip())


def downgrade_final_response_for_evidence(final_text: str, evidence: Any) -> tuple[str, dict[str, Any]]:
    """Downgrade final-answer success claims contradicted by latest evidence.

    This runs after model synthesis and before host delivery. The evidence ledger
    remains the source of truth; this helper only adds user-visible qualifiers so
    a streamed/returned final answer cannot overclaim a failed or timed-out check.
    """
    text = str(final_text or "")
    constraints = claim_constraints_for_text(text, evidence)
    blocked = constraints.get("blocked_surfaces")
    if not text.strip() or not isinstance(blocked, list) or not blocked:
        return text, constraints

    clauses = [_blocked_surface_clause(item) for item in blocked if isinstance(item, dict)]
    seen: set[str] = set()
    unique_clauses = []
    for clause in clauses:
        if clause not in seen:
            seen.add(clause)
            unique_clauses.append(clause)
    if not unique_clauses:
        return text, constraints

    downgrade = "Verification downgrade: " + " ".join(unique_clauses)
    if downgrade.lower() in text.lower():
        return text, constraints
    return _rewrite_blocked_surface_claims(text, blocked, downgrade), constraints


def metadata_has_verified_claim(value: Any) -> bool:
    if isinstance(value, dict):
        return any(metadata_has_verified_claim(v) for v in value.values())
    if isinstance(value, list):
        return any(metadata_has_verified_claim(v) for v in value)
    if isinstance(value, str):
        return bool(re.search(r"\b(verified|shipped)\b", value, flags=re.IGNORECASE))
    return False


def downgrade_verified_metadata(value: Any, blocked_surfaces: list[dict[str, Any]]) -> Any:
    """Return metadata with explicit verified/shipped strings downgraded."""
    if not blocked_surfaces:
        return value
    if isinstance(value, dict):
        updated = {str(k): downgrade_verified_metadata(v, blocked_surfaces) for k, v in value.items()}
        if metadata_has_verified_claim(value):
            updated["verification_guard"] = {
                "status": "not_verified",
                "blocked_surfaces": blocked_surfaces,
            }
        return updated
    if isinstance(value, list):
        return [downgrade_verified_metadata(item, blocked_surfaces) for item in value]
    if isinstance(value, str):
        return re.sub(r"\b(verified|shipped)\b", "not_verified", value, flags=re.IGNORECASE)
    return value

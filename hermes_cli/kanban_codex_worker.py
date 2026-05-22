"""Run one Kanban planner/dev/reviewer task through a coding worker."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from agent.transports.codex_app_server_session import CodexAppServerSession
from hermes_cli import kanban_db
from hermes_cli.discord_worker_boards import (
    ROLE_DEV,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    mark_dispatch_dirty,
    record_codex_worker_event,
    record_codex_worker_result,
)

_OPENCODE_ROLES = {ROLE_PLANNER, ROLE_DEV}


def main() -> int:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    board = os.environ.get("HERMES_KANBAN_BOARD", "").strip() or None
    role = os.environ.get("HERMES_CODEX_WORKER_ROLE", "").strip().lower()
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE", "").strip() or os.getcwd()
    if not task_id or role not in {ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER}:
        return 2

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            return 2
        prompt = _build_prompt(conn, task_id, role)
        result = _run_role_backend(
            prompt,
            workspace,
            role,
            task=task,
            task_id=task_id,
            board=board,
        )
        if result.error:
            raise RuntimeError(result.error)
        payload = _parse_json(result.final_text)
        _apply_role_output(
            conn,
            task_id,
            role,
            payload,
            board=board,
            workspace=workspace,
            expected_run_id=task.current_run_id,
        )
        return 0
    except Exception as exc:
        try:
            kanban_db.block_task(conn, task_id, reason=f"{_backend_label(role)} worker failed: {exc}")
        except Exception:
            pass
        return 1
    finally:
        conn.close()
        if board:
            try:
                mark_dispatch_dirty(board=board, reason=f"{role}-worker-finished")
            except Exception:
                pass


def _build_prompt(conn: Any, task_id: str, role: str) -> str:
    context = kanban_db.build_worker_context(conn, task_id)
    schema = _schema_instructions(role)
    git = _git_summary(os.environ.get("HERMES_KANBAN_WORKSPACE", "") or os.getcwd())
    discord_read = (
        "Read-only Discord access:\n"
        "- You may inspect Discord message context with "
        "`python -m hermes_cli.discord_worker_read fetch-message --channel-id <id> --message-id <id>`.\n"
        "- You may inspect recent thread/channel history with "
        "`python -m hermes_cli.discord_worker_read fetch-messages --channel-id <id> --limit 25`.\n"
        "- These commands are read-only. Do not attempt Discord mutation or admin actions.\n\n"
    )
    return (
        f"You are the Discord Kanban {role} worker.\n"
        "Do not call Hermes tools. Work only from the repository, shell, files, and the read-only Discord helper available in this worker environment.\n"
        "Return exactly one JSON object matching the schema below; do not wrap it in Markdown.\n\n"
        f"{discord_read}"
        f"{schema}\n\n"
        f"Git context:\n{git}\n\n"
        f"Kanban context:\n{context}"
    )


def _schema_instructions(role: str) -> str:
    if role == ROLE_PLANNER:
        return (
            'Schema: {"status":"planned|blocked","summary":"...","acceptance_criteria":["..."],'
            '"tasks":[{"title":"...","body":"...","priority":0,"parents":[]}],"blocker":null} '
            'In each task, "parents" is a list of earlier task indices this task depends on. '
            "Break the job into the fewest coherent dev tickets that can be implemented and verified independently. "
            "Do not create standalone discovery, audit, polish, or verification tickets unless that work is the user's explicit request or it blocks multiple implementation tickets; fold normal inspection and verification into the relevant implementation ticket. "
            "Each dev task body must be a detailed, self-contained implementation brief with these labeled sections: Goal, Scope, Implementation notes, Ticket-specific acceptance criteria, Likely files/subsystems, Dependencies or handoffs, Verification, and Out of scope. "
            "Write acceptance criteria for the specific slice owned by that dev ticket; do not copy the whole board-level list into every task unless that ticket owns the whole outcome. "
            "Include enough surrounding context from the overall request for a fresh dev worker to execute the ticket without guessing, but keep the scope tight to the ticket. "
            "The top-level acceptance_criteria must be one deduplicated canonical board-level list; if criteria already exist in the Kanban context, reuse them instead of paraphrasing or adding near-duplicates. "
            "Treat slash-looking text in the request as user prose unless the Kanban context explicitly says otherwise."
        )
    if role == ROLE_REVIEWER:
        return (
            'Schema: {"status":"approved|changes_requested|blocked","summary":"...","findings":["..."],'
            '"new_tasks":[{"title":"...","body":"...","priority":0}],"criteria_assessment":{}, "blocker":null}'
        )
    return (
        'Schema: {"status":"completed|blocked|checkpoint","summary":"...","changed_files":["..."],'
        '"tests":[{"command":"...","result":"passed|failed|not_run","output":"..."}],"blocker":null,"pr_ready":false}'
    )


def _configured_backend() -> str:
    try:
        from agent.opencode_worker import load_coding_worker_backend

        return load_coding_worker_backend()
    except Exception:
        return "codex"


def _backend_label(role: str) -> str:
    if _role_uses_opencode(role):
        return "OpenCode"
    return "Codex"


def _role_uses_opencode(role: str) -> bool:
    return role in _OPENCODE_ROLES and _configured_backend() == "opencode"


def _run_role_backend(
    prompt: str,
    workspace: str,
    role: str,
    *,
    task: Any,
    task_id: str,
    board: Optional[str],
):
    if _role_uses_opencode(role):
        return _run_opencode(prompt, workspace, role, task=task, task_id=task_id, board=board)
    return _run_codex(prompt, workspace, role, task_id=task_id, board=board)


def _run_codex(
    prompt: str,
    workspace: str,
    role: str,
    *,
    task_id: str,
    board: Optional[str],
):
    extra_args = _role_extra_args(role)

    def on_event(note: dict) -> None:
        try:
            record_codex_worker_event(task_id, board=board, event=note)
        except Exception:
            pass

    session = CodexAppServerSession(
        cwd=workspace,
        codex_home=os.environ.get("CODEX_HOME"),
        extra_args=extra_args,
        env={
            "HERMES_DISABLE_MCP": "1",
            "HERMES_CODEX_WORKER_NETWORK_ACCESS": "1",
        },
        on_event=on_event,
    )
    try:
        result = session.run_turn(prompt, turn_timeout=_role_timeout(role))
        _attach_scheduled_runtime(result, role)
        try:
            record_codex_worker_result(task_id, board=board, result=result)
        except Exception:
            pass
        return result
    finally:
        session.close()
        try:
            from agent.codex_worker_auth import sync_codex_worker_home

            sync_codex_worker_home(
                os.environ.get("CODEX_HOME"),
                os.environ.get("HERMES_CODEX_WORKER_CREDENTIAL_ID"),
            )
        except Exception:
            pass


def _run_opencode(
    prompt: str,
    workspace: str,
    role: str,
    *,
    task: Any,
    task_id: str,
    board: Optional[str],
):
    def on_event(note: dict) -> None:
        try:
            record_codex_worker_event(
                task_id,
                board=board,
                event={
                    "method": f"opencode/{note.get('type') or 'event'}",
                    "params": {"item": note},
                },
            )
        except Exception:
            pass

    from agent.opencode_worker import (
        load_opencode_config,
        run_opencode_single_pass,
        run_opencode_task,
    )

    context = "\n".join(
        str(part or "")
        for part in (
            getattr(task, "title", ""),
            getattr(task, "body", ""),
            prompt,
        )
    )
    if role == ROLE_PLANNER:
        cfg = load_opencode_config()
        result = run_opencode_single_pass(
            prompt,
            workspace,
            timeout=_role_timeout(role),
            agent=cfg["plan_agent"],
            reasoning_level=cfg["complex_plan_reasoning_level"],
            title=f"kanban {task_id}",
            on_event=on_event,
        )
    else:
        result = run_opencode_task(
            prompt,
            workspace,
            timeout=_role_timeout(role),
            context_for_classification=context,
            title=f"kanban {task_id}",
            on_event=on_event,
        )
    _attach_scheduled_runtime(result, role)
    try:
        record_codex_worker_result(task_id, board=board, result=result)
    except Exception:
        pass
    return result


def _role_extra_args(role: str) -> list[str]:
    effort = _worker_reasoning_effort(role)
    service_tier = _worker_service_tier()
    # Codex accepts arbitrary config overrides through -c. Unknown keys are
    # ignored by older versions, so this remains forward-compatible.
    return [
        "-c", f'model_reasoning_effort="{effort}"',
        "-c", f'service_tier="{service_tier}"',
    ]


def _worker_reasoning_effort(role: str) -> str:
    effort = str(os.environ.get("HERMES_CODEX_WORKER_REASONING") or "").strip().lower()
    if effort in {"minimal", "low", "medium", "high", "xhigh"}:
        return effort
    if role in {ROLE_PLANNER, ROLE_REVIEWER}:
        return "high"
    return "medium"


def _worker_service_tier() -> str:
    service_tier = str(os.environ.get("HERMES_CODEX_WORKER_SERVICE_TIER") or "normal").strip().lower()
    return service_tier if service_tier in {"fast", "normal"} else "normal"


def _attach_scheduled_runtime(result: Any, role: str) -> None:
    service_tier = _worker_service_tier()
    try:
        setattr(result, "service_tier", service_tier)
        setattr(result, "fast_mode", service_tier == "fast")
        if not getattr(result, "run_profile", None):
            name = "build"
            if role == ROLE_PLANNER:
                name = "plan"
            elif role == ROLE_REVIEWER:
                name = "review"
            setattr(
                result,
                "run_profile",
                {
                    "kind": f"one_pass_{name}",
                    "label": f"1-pass {name}",
                    "pass_count": 1,
                    "plan_used": False,
                    "passes": [
                        {
                            "name": name,
                            "agent": role,
                            "reasoning": _worker_reasoning_effort(role),
                        }
                    ],
                },
            )
    except Exception:
        pass


def _role_timeout(role: str) -> float:
    if role == ROLE_DEV:
        return float(os.environ.get("HERMES_CODEX_DEV_TIMEOUT", "3600"))
    return float(os.environ.get("HERMES_CODEX_PLANNER_REVIEWER_TIMEOUT", "1800"))


def _parse_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ValueError("worker did not return JSON")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("worker JSON must be an object")
    return data


def _apply_role_output(
    conn: Any,
    task_id: str,
    role: str,
    payload: dict[str, Any],
    *,
    board: Optional[str],
    workspace: str,
    expected_run_id: Optional[int],
) -> None:
    status = str(payload.get("status") or "").strip().lower()
    summary = str(payload.get("summary") or "").strip()
    if role == ROLE_PLANNER:
        if status == "blocked":
            kanban_db.block_task(
                conn,
                task_id,
                reason=payload.get("blocker") or summary,
                expected_run_id=expected_run_id,
            )
            return
        criteria = _string_list(payload.get("acceptance_criteria"))
        tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
        created = []
        for idx, spec in enumerate(tasks, start=1):
            if not isinstance(spec, dict):
                continue
            title = str(spec.get("title") or "").strip()
            if not title:
                continue
            parent_ids = [
                created[parent_idx]
                for parent_idx in _parent_indices(spec, len(tasks), idx - 1)
                if parent_idx < len(created)
            ]
            created.append(
                kanban_db.create_task(
                    conn,
                    title=title,
                    body=str(spec.get("body") or ""),
                    assignee=ROLE_DEV,
                    parents=parent_ids,
                    created_by=ROLE_PLANNER,
                    workspace_kind="dir",
                    workspace_path=workspace,
                    tenant=board,
                    priority=int(spec.get("priority") or (50 - idx)),
                    max_runtime_seconds=3600,
                )
            )
        _merge_criteria(board, criteria)
        kanban_db.complete_task(
            conn,
            task_id,
            summary=summary or f"Planned {len(created)} task(s).",
            metadata={"created_tasks": created, "acceptance_criteria": criteria, "raw": payload},
            created_cards=created,
            expected_run_id=expected_run_id,
        )
        return

    if role == ROLE_REVIEWER:
        if status == "approved":
            kanban_db.complete_task(
                conn,
                task_id,
                summary=summary or "Reviewer approved.",
                metadata={"raw": payload},
                expected_run_id=expected_run_id,
            )
            _ensure_pr(board, workspace)
            _update_phase(board, "complete", goal_status="done")
            return
        if status == "blocked":
            kanban_db.block_task(
                conn,
                task_id,
                reason=payload.get("blocker") or summary,
                expected_run_id=expected_run_id,
            )
            _update_phase(board, "blocked", goal_status="blocked")
            return
        created = []
        new_tasks = payload.get("new_tasks") if isinstance(payload.get("new_tasks"), list) else []
        for idx, spec in enumerate(new_tasks, start=1):
            if not isinstance(spec, dict) or not str(spec.get("title") or "").strip():
                continue
            created.append(
                kanban_db.create_task(
                    conn,
                    title=str(spec.get("title")).strip(),
                    body=str(spec.get("body") or ""),
                    assignee=ROLE_DEV,
                    created_by=ROLE_REVIEWER,
                    workspace_kind="dir",
                    workspace_path=workspace,
                    tenant=board,
                    priority=int(spec.get("priority") or (90 - idx)),
                    max_runtime_seconds=3600,
                )
            )
        kanban_db.complete_task(
            conn,
            task_id,
            summary=summary or "Reviewer requested changes.",
            metadata={"created_tasks": created, "raw": payload},
            created_cards=created,
            expected_run_id=expected_run_id,
        )
        _update_phase(board, "dev", goal_status="active")
        return

    if status == "blocked":
        kanban_db.block_task(conn, task_id, reason=payload.get("blocker") or summary, expected_run_id=expected_run_id)
        _update_phase(board, "blocked", goal_status="blocked")
        return
    _checkpoint_commit(workspace, task_id, summary)
    kanban_db.complete_task(
        conn,
        task_id,
        summary=summary or "Dev task completed.",
        metadata={
            "changed_files": _string_list(payload.get("changed_files")),
            "tests": payload.get("tests") if isinstance(payload.get("tests"), list) else [],
            "pr_ready": bool(payload.get("pr_ready")),
            "raw": payload,
        },
        expected_run_id=expected_run_id,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _parent_indices(spec: dict[str, Any], task_count: int, current_idx: int) -> list[int]:
    raw = spec.get("parents")
    if raw is None and "depends_on" in spec:
        raw = spec.get("depends_on")
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= task_count or idx >= current_idx or idx in seen:
            continue
        seen.add(idx)
        out.append(idx)
    return out


def _git_summary(workspace: str) -> str:
    root = Path(workspace)
    try:
        status = subprocess.run(["git", "status", "--short"], cwd=root, capture_output=True, text=True, timeout=10)
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        current = (branch.stdout or "").strip() or "unknown"
        short_status = (status.stdout or "").strip() or "(clean)"
        return f"branch: {current}\nstatus:\n{short_status}"
    except Exception as exc:
        return f"git summary unavailable: {exc}"


def _checkpoint_commit(workspace: str, task_id: str, summary: str) -> None:
    try:
        root = Path(workspace)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=10)
        if not (status.stdout or "").strip():
            return
        subprocess.run(["git", "add", "-A"], cwd=root, timeout=60)
        message = f"checkpoint {task_id}: {(summary or 'worker progress')[:80]}"
        subprocess.run(["git", "commit", "-m", message], cwd=root, timeout=120)
    except Exception:
        return


def _github_repo_from_url(url: str) -> Optional[str]:
    raw = str(url or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^git\+", "", raw)
    patterns = (
        r"^https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?(?:[#?].*)?$",
        r"^ssh://git@github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?(?:[#?].*)?$",
        r"^git@github\.com:([^/]+)/([^/#?]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, raw)
        if match:
            owner, repo = match.groups()
            return f"{owner}/{repo}"
    return None


def _resolve_github_repo(worker: dict[str, Any], root: Path) -> Optional[str]:
    context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
    for source in (context, worker):
        for key in ("github_url", "project_github_url", "repo_url", "repository_url"):
            repo = _github_repo_from_url(str(source.get(key) or ""))
            if repo:
                return repo
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if remote.returncode != 0:
        return None
    return _github_repo_from_url(remote.stdout)


def _ensure_pr(board: Optional[str], workspace: str) -> None:
    if not board:
        return
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from utils import atomic_json_write

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    root = Path(workspace)
    branch = str(worker.get("worker_branch") or "").strip()
    base = str(worker.get("base_branch") or "main").strip() or "main"
    repo = _resolve_github_repo(worker, root)
    try:
        missing = []
        if not repo:
            missing.append("GitHub repository")
        if not branch:
            missing.append("worker branch")
        if missing:
            worker["pr_error"] = f"Cannot create PR: missing {', '.join(missing)}"
            raise RuntimeError(worker["pr_error"])
        existing = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                branch,
                "--base",
                base,
                "--state",
                "open",
                "--json",
                "url",
                "--jq",
                ".[0].url",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
        )
        existing_url = (existing.stdout or "").strip()
        if existing.returncode == 0 and existing_url and existing_url != "null":
            worker["pr_url"] = existing_url
        else:
            subprocess.run(["git", "push", "-u", "origin", branch], cwd=root, timeout=300)
            title_source = str(
                worker.get("root_goal")
                or worker.get("initial_request")
                or "implementation"
            )
            title = f"Discord worker: {title_source[:80]}"
            body = (
                f"Board: {worker.get('public_url') or board}\n\n"
                f"Goal:\n{worker.get('root_goal') or worker.get('initial_request') or ''}"
            )
            created = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repo,
                    "--base",
                    base,
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body",
                    body,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if created.returncode == 0 and (created.stdout or "").strip():
                worker["pr_url"] = created.stdout.strip()
            else:
                worker["pr_error"] = (created.stderr or created.stdout or "gh pr create failed").strip()
    except Exception as exc:
        worker.setdefault("pr_error", str(exc))
    metadata[DISCORD_WORKER_META_KEY] = worker
    metadata.pop("db_path", None)
    atomic_json_write(kanban_db.board_metadata_path(board), metadata, indent=2)


def _merge_criteria(board: Optional[str], criteria: list[str]) -> None:
    if not board or not criteria:
        return
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from utils import atomic_json_write

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    existing = list(worker.get("criteria") or [])
    seen = {
        str(item.get("text") if isinstance(item, dict) else item).strip().lower()
        for item in existing
    }
    for text in criteria:
        if text.lower() in seen:
            continue
        existing.append({"text": text, "active": True})
    worker["criteria"] = existing
    metadata[DISCORD_WORKER_META_KEY] = worker
    path = kanban_db.board_metadata_path(board)
    metadata.pop("db_path", None)
    atomic_json_write(path, metadata, indent=2)


def _update_phase(board: Optional[str], phase: str, *, goal_status: str) -> None:
    if not board:
        return
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from utils import atomic_json_write

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    worker["phase"] = phase
    worker["goal_status"] = goal_status
    worker["updated_at"] = int(time.time())
    if goal_status in {"done", "blocked"}:
        worker["terminal_reaction_sync_pending"] = True
        worker["terminal_summary_sync_pending"] = True
    metadata[DISCORD_WORKER_META_KEY] = worker
    metadata.pop("db_path", None)
    atomic_json_write(kanban_db.board_metadata_path(board), metadata, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())

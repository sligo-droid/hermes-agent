"""Run one Kanban planner/dev/reviewer task through Codex app-server."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from agent.transports.codex_app_server_session import CodexAppServerSession
from hermes_cli import kanban_db
from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_PLANNER, ROLE_REVIEWER


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
        result = _run_codex(prompt, workspace, role)
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
            kanban_db.block_task(conn, task_id, reason=f"Codex worker failed: {exc}")
        except Exception:
            pass
        return 1
    finally:
        conn.close()


def _build_prompt(conn: Any, task_id: str, role: str) -> str:
    context = kanban_db.build_worker_context(conn, task_id)
    schema = _schema_instructions(role)
    git = _git_summary(os.environ.get("HERMES_KANBAN_WORKSPACE", "") or os.getcwd())
    return (
        f"You are the Discord Kanban {role} worker. You are running inside a Codex app-server container.\n"
        "Do not call Hermes tools. Work only from the repository, shell, and files available in this container.\n"
        "Return exactly one JSON object matching the schema below; do not wrap it in Markdown.\n\n"
        f"{schema}\n\n"
        f"Git context:\n{git}\n\n"
        f"Kanban context:\n{context}"
    )


def _schema_instructions(role: str) -> str:
    if role == ROLE_PLANNER:
        return (
            'Schema: {"status":"planned|blocked","summary":"...","acceptance_criteria":["..."],'
            '"tasks":[{"title":"...","body":"...","priority":0,"depends_on":[] }],"blocker":null}'
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


def _run_codex(prompt: str, workspace: str, role: str):
    extra_args = _role_extra_args(role)
    session = CodexAppServerSession(
        cwd=workspace,
        codex_home=os.environ.get("CODEX_HOME"),
        extra_args=extra_args,
        env={
            "HERMES_DISABLE_MCP": "1",
            "HERMES_CODEX_WORKER_NETWORK_ACCESS": "1",
        },
    )
    try:
        return session.run_turn(prompt, turn_timeout=_role_timeout(role))
    finally:
        session.close()


def _role_extra_args(role: str) -> list[str]:
    effort = "medium"
    if role in {ROLE_PLANNER, ROLE_REVIEWER}:
        effort = "high"
    # Codex accepts arbitrary config overrides through -c. Unknown keys are
    # ignored by older versions, so this remains forward-compatible.
    return ["-c", f'model_reasoning_effort="{effort}"']


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
            created.append(
                kanban_db.create_task(
                    conn,
                    title=title,
                    body=str(spec.get("body") or ""),
                    assignee=ROLE_DEV,
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


def _ensure_pr(board: Optional[str], workspace: str) -> None:
    if not board:
        return
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from utils import atomic_json_write

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    root = Path(workspace)
    branch = str(worker.get("worker_branch") or "").strip()
    try:
        existing = subprocess.run(
            ["gh", "pr", "view", "--json", "url", "--jq", ".url"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if existing.returncode == 0 and (existing.stdout or "").strip():
            worker["pr_url"] = existing.stdout.strip()
        else:
            if branch:
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
                ["gh", "pr", "create", "--title", title, "--body", body],
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
        worker["pr_error"] = str(exc)
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
    metadata[DISCORD_WORKER_META_KEY] = worker
    metadata.pop("db_path", None)
    atomic_json_write(kanban_db.board_metadata_path(board), metadata, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())

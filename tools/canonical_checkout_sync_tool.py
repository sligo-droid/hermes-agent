"""Scoped post-merge synchronization for protected canonical checkouts.

The terminal tool deliberately refuses mutable commands against canonical
``main`` checkouts.  This tool is the narrow orchestration capability for the
one permitted operation after a verified merge: a clean fast-forward followed
by merge-commit containment verification.  It is intentionally unavailable to
delegated and dispatcher-scoped workers.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from tools.registry import registry, tool_error


_MERGE_COMMIT_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


SYNC_CANONICAL_CHECKOUT_SCHEMA = {
    "name": "sync_canonical_checkout",
    "description": (
        "Fast-forward a clean protected canonical checkout after a verified GitHub PR merge. "
        "Use only after confirming the PR is MERGED and obtaining its exact merge commit SHA. "
        "This never resets, stashes, merges, or creates/pushes a PR."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_path": {
                "type": "string",
                "description": "Absolute path to the protected canonical repository root.",
            },
            "branch": {
                "type": "string",
                "description": "Verified default branch currently checked out by the canonical repository.",
            },
            "merge_commit": {
                "type": "string",
                "description": "Exact merge commit SHA reported by the already-merged pull request.",
            },
        },
        "required": ["project_path", "branch", "merge_commit"],
    },
}


def check_canonical_checkout_sync_requirements() -> bool:
    """Expose the capability to orchestrators, never dispatcher task workers."""
    return not bool(os.environ.get("HERMES_KANBAN_TASK"))


check_canonical_checkout_sync_requirements._hermes_skip_check_cache = True


def _orchestrator_error(parent_agent: Any) -> str | None:
    if parent_agent is None:
        return "sync_canonical_checkout requires trusted Hermes orchestrator dispatch."
    if os.environ.get("HERMES_KANBAN_TASK"):
        return (
            "sync_canonical_checkout is orchestrator-only; dispatcher-scoped workers "
            "must hand off post-merge lifecycle work to Hermes."
        )
    if int(getattr(parent_agent, "_delegate_depth", 0) or 0) > 0:
        return (
            "sync_canonical_checkout is reserved for the parent Hermes orchestrator; "
            "delegated agents must return their handoff instead."
        )
    return None


def _canonical_root(project_path: str, branch: str):
    """Use the shared protected-root/default-branch validation boundary."""
    from hermes_cli.canonical_checkout_sync import resolve_protected_canonical_checkout

    return resolve_protected_canonical_checkout(project_path, branch)


def sync_canonical_checkout_tool(
    *,
    project_path: str,
    branch: str,
    merge_commit: str,
    parent_agent: Any = None,
) -> str:
    """Run the one safe mutable canonical-checkout operation for an orchestrator."""
    denied = _orchestrator_error(parent_agent)
    if denied:
        return tool_error(denied)
    commit = str(merge_commit or "").strip()
    if not _MERGE_COMMIT_RE.fullmatch(commit):
        return tool_error("merge_commit must be an exact 40- or 64-character hexadecimal Git SHA")
    root, validation_error = _canonical_root(project_path, branch)
    if validation_error:
        return tool_error(validation_error)
    assert root is not None

    from hermes_cli.canonical_checkout_sync import sync_canonical_checkout

    result = sync_canonical_checkout(root, str(branch).strip(), commit)
    payload = result.as_dict()
    payload["ok"] = result.state.startswith("synced")
    return json.dumps(payload, ensure_ascii=False)


def _registry_handler(args: dict[str, Any], **kwargs: Any) -> str:
    """Deny raw registry calls; agent runtime passes the trusted parent explicitly."""
    return sync_canonical_checkout_tool(
        project_path=str(args.get("project_path") or ""),
        branch=str(args.get("branch") or ""),
        merge_commit=str(args.get("merge_commit") or ""),
        parent_agent=kwargs.get("parent_agent"),
    )


registry.register(
    name="sync_canonical_checkout",
    toolset="canonical_sync",
    schema=SYNC_CANONICAL_CHECKOUT_SCHEMA,
    handler=_registry_handler,
    check_fn=check_canonical_checkout_sync_requirements,
    emoji="↻",
)

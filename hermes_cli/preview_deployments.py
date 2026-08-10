"""Exact-head preview deployment discovery for pull requests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


CommandRunner = Callable[..., Any]


@dataclass(frozen=True)
class PreviewDeployment:
    """One bounded observation of a Vercel preview deployment."""

    status: str
    observed_sha: str
    url: str = ""
    deployment_id: str = ""
    diagnostic_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": "vercel",
            "status": self.status,
            "observed_sha": self.observed_sha,
        }
        if self.url:
            result["url"] = self.url
        if self.deployment_id:
            result["deployment_id"] = self.deployment_id
        if self.diagnostic_code:
            result["diagnostic_code"] = self.diagnostic_code
        return result


def _is_vercel_preview_url(value: Any) -> bool:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return False
    hostname = str(parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and bool(hostname)
        and (hostname == "vercel.app" or hostname.endswith(".vercel.app"))
        and not parsed.username
        and not parsed.password
    )


def _json_payload(result: Any, *, default: Any) -> Any:
    if int(getattr(result, "returncode", 1) or 0) != 0:
        return default
    try:
        return json.loads(str(getattr(result, "stdout", "") or ""))
    except (TypeError, ValueError):
        return default


def _deployment_sort_key(value: Mapping[str, Any]) -> tuple[str, int]:
    created_at = str(value.get("created_at") or value.get("updated_at") or "")
    try:
        deployment_id = int(value.get("id") or 0)
    except (TypeError, ValueError):
        deployment_id = 0
    return created_at, deployment_id


def _vercel_branch_preview_url(comments: Any) -> str:
    if not isinstance(comments, list):
        return ""
    for comment in reversed(comments[-100:]):
        if not isinstance(comment, Mapping):
            continue
        author = comment.get("user")
        login = (
            str(author.get("login") or "").lower()
            if isinstance(author, Mapping)
            else ""
        )
        if "vercel" not in login:
            continue
        body = str(comment.get("body") or "")
        for candidate in re.findall(r"https://[a-z0-9.-]+\.vercel\.app", body):
            hostname = str(urlsplit(candidate).hostname or "").lower()
            if "-git-" in hostname and _is_vercel_preview_url(candidate):
                return candidate
    return ""


def collect_vercel_preview(
    *,
    repository: str,
    head_sha: str,
    branch: str,
    pr_number: int,
    root: Path,
    run: CommandRunner,
) -> PreviewDeployment:
    """Read the newest exact-head Vercel Preview deployment from GitHub."""

    repository = str(repository or "").strip()
    head_sha = str(head_sha or "").strip().lower()
    branch = str(branch or "").strip()
    if not repository or not head_sha or not branch or int(pr_number or 0) < 1:
        return PreviewDeployment(
            status="blocked",
            observed_sha=head_sha,
            diagnostic_code="preview_identity_missing",
        )

    deployments_result = run(
        [
            "gh",
            "api",
            (
                f"repos/{repository}/deployments?ref={head_sha}"
                "&environment=Preview&per_page=100"
            ),
        ],
        cwd=root,
        timeout=30,
        github=True,
    )
    if int(getattr(deployments_result, "returncode", 1) or 0) != 0:
        return PreviewDeployment(
            status="pending",
            observed_sha=head_sha,
            diagnostic_code="preview_deployments_unavailable",
        )

    payload = _json_payload(deployments_result, default=[])
    deployments = payload if isinstance(payload, list) else []
    candidates: list[Mapping[str, Any]] = []
    for deployment in deployments[:100]:
        if not isinstance(deployment, Mapping):
            continue
        creator = deployment.get("creator")
        creator_login = (
            str(creator.get("login") or "").lower()
            if isinstance(creator, Mapping)
            else ""
        )
        if "vercel" not in creator_login:
            continue
        if str(deployment.get("sha") or "").strip().lower() != head_sha:
            continue
        if str(deployment.get("ref") or "").strip() not in {branch, head_sha}:
            continue
        environment = str(deployment.get("environment") or "").strip().lower()
        if environment != "preview":
            continue
        candidates.append(deployment)

    if not candidates:
        return PreviewDeployment(status="pending", observed_sha=head_sha)

    deployment = max(candidates, key=_deployment_sort_key)
    deployment_id = str(deployment.get("id") or "").strip()[:40]
    if not deployment_id.isdigit():
        return PreviewDeployment(
            status="pending",
            observed_sha=head_sha,
            diagnostic_code="preview_deployment_id_invalid",
        )

    statuses_result = run(
        [
            "gh",
            "api",
            f"repos/{repository}/deployments/{deployment_id}/statuses?per_page=100",
        ],
        cwd=root,
        timeout=30,
        github=True,
    )
    if int(getattr(statuses_result, "returncode", 1) or 0) != 0:
        return PreviewDeployment(
            status="pending",
            observed_sha=head_sha,
            deployment_id=deployment_id,
            diagnostic_code="preview_statuses_unavailable",
        )

    statuses_payload = _json_payload(statuses_result, default=[])
    statuses = statuses_payload if isinstance(statuses_payload, list) else []
    latest = next((item for item in statuses if isinstance(item, Mapping)), None)
    if latest is None:
        return PreviewDeployment(
            status="pending",
            observed_sha=head_sha,
            deployment_id=deployment_id,
        )

    state = str(latest.get("state") or "").strip().lower()
    deployment_url = str(latest.get("environment_url") or "").strip()
    if state == "success" and _is_vercel_preview_url(deployment_url):
        comments_result = run(
            [
                "gh",
                "api",
                f"repos/{repository}/issues/{int(pr_number)}/comments?per_page=100",
            ],
            cwd=root,
            timeout=30,
            github=True,
        )
        branch_url = _vercel_branch_preview_url(
            _json_payload(comments_result, default=[])
        )
        if not branch_url:
            return PreviewDeployment(
                status="pending",
                observed_sha=head_sha,
                deployment_id=deployment_id,
                diagnostic_code="preview_branch_url_missing",
            )
        return PreviewDeployment(
            status="ready",
            observed_sha=head_sha,
            url=branch_url[:1200],
            deployment_id=deployment_id,
        )
    if state == "success":
        return PreviewDeployment(
            status="pending",
            observed_sha=head_sha,
            deployment_id=deployment_id,
            diagnostic_code="preview_url_missing",
        )
    if state in {"error", "failure"}:
        return PreviewDeployment(
            status="failed",
            observed_sha=head_sha,
            deployment_id=deployment_id,
            diagnostic_code=f"preview_{state}",
        )
    return PreviewDeployment(
        status="pending",
        observed_sha=head_sha,
        deployment_id=deployment_id,
    )


__all__ = ["PreviewDeployment", "collect_vercel_preview"]

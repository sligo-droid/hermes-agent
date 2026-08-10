"""Exact-head preview deployment discovery for pull requests."""

from __future__ import annotations

import json
import re
from base64 import b64decode
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit


CommandRunner = Callable[..., Any]
VERCEL_GITHUB_APP_ID = 8329


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


def _is_vercel_bot(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and str(value.get("login") or "").lower() == "vercel[bot]"
        and str(value.get("type") or "").lower() == "bot"
    )


def _vercel_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _utc_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _vercel_comment_payload(body: str) -> Mapping[str, Any] | None:
    first_line = body.splitlines()[0] if body else ""
    match = re.fullmatch(r"\[vc\]: #[^:\s]+:([A-Za-z0-9+/=]+)", first_line)
    if match is None:
        return None
    try:
        decoded = b64decode(match.group(1), validate=True).decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _vercel_branch_preview_url(
    comments: Any,
    *,
    repository: str,
    branch: str,
    pr_number: int,
    deployment_url: str,
    deployment_updated_at: Any,
    vercel_deployment_id: str,
) -> str:
    if not isinstance(comments, list):
        return ""
    try:
        owner, repository_name = repository.split("/", 1)
    except ValueError:
        return ""
    team_slug = _vercel_slug(owner)
    project_slug = _vercel_slug(repository_name)
    branch_slug = _vercel_slug(branch)
    if not team_slug or not project_slug or not branch_slug:
        return ""
    expected_url = (
        f"https://{project_slug}-git-{branch_slug}-{team_slug}.vercel.app"
    )
    deployment_host = str(urlsplit(deployment_url).hostname or "").lower()
    if not (
        deployment_host.startswith(f"{project_slug}-")
        and deployment_host.endswith(f"-{team_slug}.vercel.app")
    ):
        return ""
    deployment_time = _utc_timestamp(deployment_updated_at)
    if deployment_time is None:
        return ""

    for comment in reversed(comments[-100:]):
        if not isinstance(comment, Mapping):
            continue
        author = comment.get("user")
        app = comment.get("performed_via_github_app")
        if not _is_vercel_bot(author) or not isinstance(app, Mapping):
            continue
        if app.get("id") != VERCEL_GITHUB_APP_ID or app.get("slug") != "vercel":
            continue
        comment_time = _utc_timestamp(comment.get("updated_at"))
        if comment_time is None or comment_time < deployment_time:
            continue
        body = str(comment.get("body") or "")
        payload = _vercel_comment_payload(body)
        if payload is None:
            continue
        projects = payload.get("projects")
        if not isinstance(projects, list):
            continue
        project = next(
            (
                item
                for item in projects
                if isinstance(item, Mapping)
                and str(item.get("name") or "").lower() == project_slug
            ),
            None,
        )
        if project is None:
            continue
        expected_host = str(urlsplit(expected_url).hostname or "")
        preview_url = str(project.get("previewUrl") or "").strip()
        if preview_url not in {expected_url, expected_host}:
            continue
        if str(project.get("nextCommitStatus") or "") != "DEPLOYED":
            continue
        inspector_url = str(project.get("inspectorUrl") or "").strip()
        if not inspector_url.startswith(
            f"https://vercel.com/{team_slug}/{project_slug}/"
        ):
            continue
        if urlsplit(inspector_url).path.rsplit("/", 1)[-1] != vercel_deployment_id:
            continue
        review_url = urlsplit(str(payload.get("requestReviewUrl") or ""))
        review_query = parse_qs(review_url.query)
        if (
            review_url.scheme != "https"
            or review_url.netloc != "vercel.com"
            or review_url.path != "/vercel-agent/request-review"
            or review_query.get("owner") != [owner]
            or review_query.get("repo") != [repository_name]
            or review_query.get("pr") != [str(pr_number)]
        ):
            continue
        if (
            f"[{project_slug}](https://vercel.com/{team_slug}/{project_slug})"
            not in body.lower()
        ):
            continue
        if f"[preview]({expected_url})" not in body.lower():
            continue
        if inspector_url not in body:
            continue
        return expected_url
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
        if not _is_vercel_bot(deployment.get("creator")):
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
    if (
        state == "success"
        and _is_vercel_preview_url(deployment_url)
        and _is_vercel_bot(latest.get("creator"))
    ):
        inspection_result = run(
            ["vercel", "inspect", deployment_url, "--json"],
            cwd=root,
            timeout=30,
        )
        inspection = _json_payload(inspection_result, default={})
        if not isinstance(inspection, Mapping):
            return PreviewDeployment(
                status="pending",
                observed_sha=head_sha,
                deployment_id=deployment_id,
                diagnostic_code="preview_inspection_unavailable",
            )
        try:
            owner, repository_name = repository.split("/", 1)
        except ValueError:
            owner = repository_name = ""
        project_slug = _vercel_slug(repository_name)
        expected_branch_url = (
            f"https://{project_slug}-git-{_vercel_slug(branch)}-"
            f"{_vercel_slug(owner)}.vercel.app"
        )
        inspected_url = str(inspection.get("url") or "").strip()
        inspected_id = str(inspection.get("id") or "").strip()
        aliases = inspection.get("aliases")
        if not (
            inspected_id.startswith("dpl_")
            and inspected_url == str(urlsplit(deployment_url).hostname or "")
            and str(inspection.get("name") or "").lower() == project_slug
            and str(inspection.get("target") or "").lower() == "preview"
            and str(inspection.get("readyState") or "").upper() == "READY"
            and isinstance(aliases, list)
            and str(urlsplit(expected_branch_url).hostname or "") in aliases
        ):
            return PreviewDeployment(
                status="pending",
                observed_sha=head_sha,
                deployment_id=deployment_id,
                diagnostic_code="preview_inspection_unavailable",
            )
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
            _json_payload(comments_result, default=[]),
            repository=repository,
            branch=branch,
            pr_number=int(pr_number),
            deployment_url=deployment_url,
            vercel_deployment_id=inspected_id.removeprefix("dpl_"),
            deployment_updated_at=(
                latest.get("updated_at")
                or latest.get("created_at")
                or deployment.get("updated_at")
                or deployment.get("created_at")
            ),
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

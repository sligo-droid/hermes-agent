"""Secret-safe OpenAI Codex auth incident correlation helpers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


PROVIDER_ROUTE = "openai-codex"
INCIDENT_TYPE = "credential_invalidation"

_AUTH_RE = re.compile(r"(?i)(token_invalidated|authenticationerror|auth(?:entication)?\s+failed|\b401\b)")
_ROUTE_RE = re.compile(r"(?i)(openai[-_/ ]?codex|codex|hermes_cli\.proxy\.adapters\.openai_codex)")
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(authorization)\b\s*[:=]\s*bearer\s+[^\s,;}\]]+"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|authorization)\b\s*[:=]\s*([^\s,;}\]]+)"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r'(?i)("(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|authorization)"\s*:\s*)"[^"]*"'),
]
_ISO_RE = re.compile(r"\b(20\d\d-\d\d-\d\d[T ][0-2]\d:[0-5]\d:[0-5]\d(?:\.\d+)?Z?)\b")
_CRON_PATH_RE = re.compile(r"cron/output/([^/]+)/")


@dataclass
class CodexAuthIncident:
    provider_route: str = PROVIDER_ROUTE
    incident_type: str = INCIDENT_TYPE
    affected_cron_jobs: list[dict[str, str]] = field(default_factory=list)
    affected_consumers: list[str] = field(default_factory=list)
    first_seen: str | None = None
    last_seen: str | None = None
    current_state: str = "unknown"
    historical_evidence_count: int = 0
    next_action: str = (
        "Manually verify or refresh the OpenAI Codex credential; do not rotate secrets automatically."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_route": self.provider_route,
            "incident_type": self.incident_type,
            "affected_cron_jobs": self.affected_cron_jobs,
            "affected_consumers": self.affected_consumers,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "current_state": self.current_state,
            "historical_evidence_count": self.historical_evidence_count,
            "next_action": self.next_action,
        }


def redact(text: str, limit: int | None = None) -> str:
    """Remove credential-shaped values from operator-facing incident text."""
    safe = str(text or "")
    for pattern in _SECRET_PATTERNS[:3]:
        safe = pattern.sub(lambda m: f"{m.group(1)}=<redacted>", safe)
    safe = _SECRET_PATTERNS[3].sub(lambda m: f'{m.group(1)}"<redacted>"', safe)
    if limit is not None and len(safe) > limit:
        return safe[:limit] + "...<truncated>"
    return safe


def is_codex_auth_failure(text: str) -> bool:
    """Return True only for OpenAI/Codex route authentication failures."""
    value = str(text or "")
    return bool(_AUTH_RE.search(value) and _ROUTE_RE.search(value))


def _timestamp(text: str) -> str | None:
    match = _ISO_RE.search(text or "")
    return match.group(1) if match else None


def _consumer_label(text: str, source: str) -> str:
    haystack = f"{source}\n{text}".lower()
    if "honcho" in haystack:
        return "honcho-proxy"
    if "proxy.adapters.openai_codex" in haystack or "openai_codex" in haystack or "502" in haystack:
        return "openai-codex-proxy"
    if "cron" in haystack:
        return "cron.scheduler"
    if "agent.conversation_loop" in haystack or "conversation_loop" in haystack:
        return "agent.conversation_loop"
    return "openai-codex-route"


def _add_cron_job(target: dict[str, dict[str, str]], job_id: str | None, name: str | None = None) -> None:
    if not job_id:
        return
    entry = target.setdefault(str(job_id), {"id": str(job_id), "name": str(name or job_id)})
    if name and entry.get("name") == entry["id"]:
        entry["name"] = str(name)


def classify_codex_auth_incident(
    evidence: Iterable[dict[str, Any]],
    *,
    auth_status_text: str = "",
) -> CodexAuthIncident | None:
    """Group Codex auth evidence into one provider-route incident."""
    cron_jobs: dict[str, dict[str, str]] = {}
    consumers: set[str] = set()
    timestamps: list[str] = []
    count = 0

    for item in evidence:
        text = str(item.get("text") or "")
        source = str(item.get("source") or "")
        combined = f"{source}\n{text}"
        if not is_codex_auth_failure(combined):
            continue
        count += 1
        consumers.add(_consumer_label(text, source))
        _add_cron_job(cron_jobs, item.get("cron_job_id"), item.get("cron_job_name"))
        path_match = _CRON_PATH_RE.search(source)
        if path_match:
            _add_cron_job(cron_jobs, path_match.group(1), None)
        ts = str(item.get("timestamp") or "") or _timestamp(combined)
        if ts:
            timestamps.append(ts)

    if count == 0:
        return None

    status = auth_status_text.lower()
    if "logged in" in status and not re.search(r"(?i)(auth failed|expired|invalid|401|403)", status):
        current_state = "recovered_logged_in"
    elif re.search(r"(?i)(auth failed|expired|invalid|401|403|not logged in)", status):
        current_state = "current_auth_failure"
    else:
        current_state = "unknown_current_status"

    sorted_jobs = sorted(cron_jobs.values(), key=lambda item: (item.get("id") or "", item.get("name") or ""))
    sorted_timestamps = sorted(timestamps)
    return CodexAuthIncident(
        affected_cron_jobs=sorted_jobs,
        affected_consumers=sorted(consumers),
        first_seen=sorted_timestamps[0] if sorted_timestamps else None,
        last_seen=sorted_timestamps[-1] if sorted_timestamps else None,
        current_state=current_state,
        historical_evidence_count=count,
    )


def render_codex_auth_incident_summary(incident: CodexAuthIncident | dict[str, Any]) -> str:
    """Render a compact Markdown summary without raw evidence or secrets."""
    data = incident.to_dict() if isinstance(incident, CodexAuthIncident) else dict(incident)
    jobs = data.get("affected_cron_jobs") or []
    job_text = ", ".join(
        f"{redact(str(job.get('id') or 'unknown'))} ({redact(str(job.get('name') or 'unnamed'))})"
        for job in jobs
        if isinstance(job, dict)
    ) or "none identified"
    consumers = ", ".join(redact(str(item)) for item in data.get("affected_consumers") or []) or "none identified"
    current_state = str(data.get("current_state") or "unknown_current_status")
    if current_state == "recovered_logged_in":
        state_text = "appears recovered/currently logged in; historical auth failures remain"
    elif current_state == "current_auth_failure":
        state_text = "currently failing auth/status checks"
    else:
        state_text = "current auth status unknown; historical auth failures detected"
    return redact(
        "### OpenAI Codex auth-route incident\n"
        f"- Provider route: `{PROVIDER_ROUTE}`\n"
        f"- Incident type: `{INCIDENT_TYPE}`\n"
        f"- State: {state_text}\n"
        f"- Evidence count: {int(data.get('historical_evidence_count') or 0)}\n"
        f"- Window: {data.get('first_seen') or 'unknown'} to {data.get('last_seen') or 'unknown'}\n"
        f"- Affected cron jobs: {job_text}\n"
        f"- Affected consumers: {consumers}\n"
        f"- Next action: {data.get('next_action') or CodexAuthIncident().next_action}"
    )


def summarize_failure_text(text: str, *, job: dict[str, Any] | None = None) -> str | None:
    """Return a one-incident summary for a single cron failure, if it matches."""
    job = job or {}
    incident = classify_codex_auth_incident(
        [
            {
                "source": "cron.scheduler",
                "text": text,
                "cron_job_id": job.get("id"),
                "cron_job_name": job.get("name") or job.get("prompt"),
            }
        ]
    )
    return render_codex_auth_incident_summary(incident) if incident else None


def _read_tail(path: Path, *, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            if path.stat().st_size > max_bytes:
                handle.seek(-max_bytes, 2)
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def collect_codex_auth_evidence(hermes_home: Path, *, max_log_bytes: int = 256_000, max_output_files: int = 20) -> list[dict[str, Any]]:
    """Collect bounded, on-disk evidence without invoking model inference."""
    home = Path(hermes_home)
    evidence: list[dict[str, Any]] = []

    errors_log = home / "logs" / "errors.log"
    if errors_log.exists():
        for line in _read_tail(errors_log, max_bytes=max_log_bytes).splitlines():
            if is_codex_auth_failure(line):
                evidence.append({"source": str(errors_log), "text": line, "timestamp": _timestamp(line)})

    jobs_path = home / "cron" / "jobs.json"
    job_names: dict[str, str] = {}
    if jobs_path.exists():
        try:
            payload = json.loads(jobs_path.read_text(encoding="utf-8"))
            jobs = payload.get("jobs") if isinstance(payload, dict) else payload
            for job in jobs if isinstance(jobs, list) else []:
                if not isinstance(job, dict):
                    continue
                job_id = str(job.get("id") or "")
                name = str(job.get("name") or job.get("prompt") or job_id)
                if job_id:
                    job_names[job_id] = name
                status_text = "\n".join(str(job.get(key) or "") for key in ("last_status", "last_error", "error"))
                if is_codex_auth_failure(status_text):
                    evidence.append(
                        {"source": str(jobs_path), "text": status_text, "cron_job_id": job_id, "cron_job_name": name}
                    )
        except (OSError, json.JSONDecodeError):
            pass

    output_root = home / "cron" / "output"
    if output_root.exists():
        files = sorted(output_root.glob("*/*.md"), key=lambda item: item.stat().st_mtime, reverse=True)[:max_output_files]
        for path in files:
            text = _read_tail(path, max_bytes=64_000)
            if is_codex_auth_failure(text):
                job_id = path.parent.name
                evidence.append(
                    {
                        "source": str(path),
                        "text": text,
                        "cron_job_id": job_id,
                        "cron_job_name": job_names.get(job_id, job_id),
                        "timestamp": _timestamp(text),
                    }
                )

    return evidence

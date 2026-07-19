"""Pure project inspection target resolution."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit


MAX_PROJECT_INSPECTION_CANDIDATES = 12
MAX_PROJECT_INSPECTION_URL_LENGTH = 2048
_MAX_PROJECT_CONFIG_ENTRIES = 256
_MAX_CONFIGURED_URLS_PER_ENVIRONMENT = 24
_MAX_QUERY_FIELDS = 24
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_QUERY_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_secret",
        "clientsecret",
        "credential",
        "credentials",
        "jwt",
        "key",
        "password",
        "secret",
        "session",
        "sig",
        "signature",
        "token",
    }
)
_SECRET_QUERY_COMPACT = frozenset(
    {
        "accesstoken",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "idtoken",
        "privatekey",
        "refreshtoken",
        "sessionid",
    }
)


@dataclass(frozen=True)
class ProjectInspectionCandidate:
    """One safe browser target, ordered by inspection preference."""

    url: str
    environment: str
    location: str

    def to_dict(self) -> dict[str, str]:
        return {
            "url": self.url,
            "environment": self.environment,
            "location": self.location,
        }


@dataclass(frozen=True)
class ProjectInspectionResolution:
    """The matched project and its bounded inspection targets."""

    project_key: Optional[str]
    matched_by: Optional[str]
    candidates: tuple[ProjectInspectionCandidate, ...] = ()


def normalize_github_repo(value: Any) -> Optional[str]:
    """Return a lowercase ``owner/repo`` identity for an exact GitHub repo."""

    raw = str(value or "").strip()
    if not raw or _CONTROL_CHAR_RE.search(raw):
        return None

    if raw.startswith("git@github.com:"):
        path = raw[len("git@github.com:") :]
    elif re.fullmatch(r"[^/\s]+/[^/\s]+(?:\.git)?", raw):
        path = raw
    else:
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return None
        if (parsed.hostname or "").lower().rstrip(".") != "github.com":
            return None
        if parsed.query or parsed.fragment:
            return None
        path = parsed.path.lstrip("/")

    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    if any(part in {".", ".."} or re.search(r"[^A-Za-z0-9_.-]", part) for part in parts):
        return None
    return f"{parts[0].lower()}/{parts[1].lower()}"


def _is_secret_query_name(name: str) -> bool:
    decoded = unquote(name).strip().lower()
    if not decoded:
        return False
    if decoded in _SECRET_QUERY_PARTS:
        return True
    compact = re.sub(r"[^a-z0-9]+", "", decoded)
    if compact in _SECRET_QUERY_PARTS or compact in _SECRET_QUERY_COMPACT:
        return True
    parts = {part for part in re.split(r"[^a-z0-9]+", decoded) if part}
    return bool(parts & _SECRET_QUERY_PARTS)


def _is_local_or_private_host(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host.endswith((".local", ".internal", ".lan", ".home")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return not address.is_global


def normalize_project_inspection_url(value: Any, *, environment: str) -> Optional[str]:
    """Validate and canonicalize one absolute HTTP(S) inspection URL."""

    raw = str(value or "").strip()
    if not raw or len(raw) > MAX_PROJECT_INSPECTION_URL_LENGTH:
        return None
    if (
        _CONTROL_CHAR_RE.search(raw)
        or _CONTROL_CHAR_RE.search(unquote(raw))
        or any(char.isspace() for char in raw)
    ):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if scheme not in {"http", "https"} or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if "\\" in parsed.netloc or _CONTROL_CHAR_RE.search(unquote(parsed.netloc)):
        return None
    try:
        query_fields = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=_MAX_QUERY_FIELDS,
        )
    except ValueError:
        return None
    if any(_is_secret_query_name(name) for name, _ in query_fields):
        return None

    local_or_private = _is_local_or_private_host(hostname)
    if environment == "production" and local_or_private:
        return None
    if environment not in {"development", "production"}:
        return None

    host = hostname.lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, host, path, parsed.query, parsed.fragment))


def _configured_urls(value: Any) -> list[Any]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value[:_MAX_CONFIGURED_URLS_PER_ENVIRONMENT])
    return []


def _inspection_section(project: Mapping[str, Any]) -> Mapping[str, Any]:
    inspection = project.get("inspection")
    return inspection if isinstance(inspection, Mapping) else project


def _configured_repo(project: Mapping[str, Any]) -> Optional[str]:
    for key in ("repository", "github_repo", "github_url"):
        normalized = normalize_github_repo(project.get(key))
        if normalized:
            return normalized
    return None


def _select_project(
    projects: Mapping[str, Any],
    *,
    github_repo: Any = None,
    project_key: Any = None,
) -> tuple[Optional[str], Optional[str], Optional[Mapping[str, Any]]]:
    normalized_repo = normalize_github_repo(github_repo)
    if normalized_repo:
        for index, (key, value) in enumerate(projects.items()):
            if index >= _MAX_PROJECT_CONFIG_ENTRIES:
                break
            if isinstance(key, str) and isinstance(value, Mapping):
                if _configured_repo(value) == normalized_repo:
                    return key, "github_repo", value

    explicit_key = str(project_key or "")
    value = projects.get(explicit_key)
    if explicit_key and isinstance(value, Mapping):
        return explicit_key, "project_key", value
    return None, None, None


def resolve_project_inspection(
    projects: Any,
    *,
    github_repo: Any = None,
    project_key: Any = None,
) -> ProjectInspectionResolution:
    """Resolve safe candidates by exact repository identity, then explicit key."""

    if not isinstance(projects, Mapping):
        return ProjectInspectionResolution(project_key=None, matched_by=None)
    selected_key, matched_by, project = _select_project(
        projects,
        github_repo=github_repo,
        project_key=project_key,
    )
    if project is None:
        return ProjectInspectionResolution(project_key=None, matched_by=None)

    inspection = _inspection_section(project)
    development_values = _configured_urls(inspection.get("development_urls"))
    production_values = _configured_urls(inspection.get("production_urls"))
    buckets: dict[str, list[ProjectInspectionCandidate]] = {
        "local_development": [],
        "external_development": [],
        "production": [],
    }
    seen: set[str] = set()

    for environment, values in (
        ("development", development_values),
        ("production", production_values),
    ):
        for value in values:
            url = normalize_project_inspection_url(value, environment=environment)
            if not url or url in seen:
                continue
            seen.add(url)
            location = "local" if _is_local_or_private_host(urlsplit(url).hostname or "") else "external"
            bucket = (
                "local_development"
                if environment == "development" and location == "local"
                else "external_development"
                if environment == "development"
                else "production"
            )
            buckets[bucket].append(
                ProjectInspectionCandidate(
                    url=url,
                    environment=environment,
                    location=location,
                )
            )

    ordered = (
        buckets["local_development"]
        + buckets["external_development"]
        + buckets["production"]
    )[:MAX_PROJECT_INSPECTION_CANDIDATES]
    return ProjectInspectionResolution(
        project_key=selected_key,
        matched_by=matched_by,
        candidates=tuple(ordered),
    )


def normalize_project_inspection_candidates(value: Any) -> tuple[ProjectInspectionCandidate, ...]:
    """Revalidate a serialized candidate list at a session boundary."""

    if not isinstance(value, (list, tuple)):
        return ()
    development: list[Any] = []
    production: list[Any] = []
    for item in value[: MAX_PROJECT_INSPECTION_CANDIDATES * 2]:
        if isinstance(item, ProjectInspectionCandidate):
            item = item.to_dict()
        if not isinstance(item, Mapping):
            continue
        environment = str(item.get("environment") or "").lower()
        if environment == "development":
            development.append(item.get("url"))
        elif environment == "production":
            production.append(item.get("url"))
    resolution = resolve_project_inspection(
        {
            "session": {
                "inspection": {
                    "development_urls": development,
                    "production_urls": production,
                }
            }
        },
        project_key="session",
    )
    return resolution.candidates


def project_inspection_candidates_to_dicts(
    candidates: Iterable[ProjectInspectionCandidate],
) -> list[dict[str, str]]:
    return [candidate.to_dict() for candidate in tuple(candidates)[:MAX_PROJECT_INSPECTION_CANDIDATES]]


def serialize_project_inspection_candidates(value: Any) -> str:
    candidates = normalize_project_inspection_candidates(value)
    return json.dumps(project_inspection_candidates_to_dicts(candidates), separators=(",", ":"))

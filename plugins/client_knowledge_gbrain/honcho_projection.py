"""Deterministic Honcho projection after verified GBrain assimilation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hermes_cli.config import load_config

from .client import GBrainClient, load_settings
from .derived import DerivedStore
from .scope import full_project_slug, validate_canonical_project_slug, validate_page, validate_project_key
from .spool import RawSpool
from .store import DEFAULT_LEASE_SECONDS, IntakeStore, JobClaim

PROMOTABLE_KINDS = frozenset({"decision", "requirement", "fact", "preference"})


class HonchoProjectionFailure(ValueError):
    def __init__(self, error_class: str, *, operator_blocked: bool = False) -> None:
        super().__init__(error_class)
        self.error_class = error_class
        self.operator_blocked = operator_blocked


@dataclass(frozen=True, slots=True)
class HonchoProjectionSettings:
    enabled: bool
    max_jobs_per_run: int
    lease_seconds: float
    retry_delay_seconds: float
    observer_peer_id: str
    max_remote_conclusions: int

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "HonchoProjectionSettings":
        ck = config.get("client_knowledge", {})
        raw = ck.get("honcho_projection", {}) if isinstance(ck, Mapping) else {}
        if not isinstance(raw, Mapping):
            raise HonchoProjectionFailure("honcho_projection_config_invalid", operator_blocked=True)
        observer = str(raw.get("observer_peer_id") or "hermes").strip()
        if not observer or len(observer) > 128:
            raise HonchoProjectionFailure("honcho_projection_config_invalid", operator_blocked=True)
        return cls(
            enabled=bool(raw.get("enabled", False)),
            max_jobs_per_run=max(1, min(100, int(raw.get("max_jobs_per_run", 10)))),
            lease_seconds=max(5.0, float(raw.get("lease_seconds", DEFAULT_LEASE_SECONDS))),
            retry_delay_seconds=max(0.0, float(raw.get("retry_delay_seconds", 60))),
            observer_peer_id=observer,
            max_remote_conclusions=max(
                1, min(5000, int(raw.get("max_remote_conclusions", 500)))
            ),
        )


def _project_peer(config: Mapping[str, Any], project_key: str) -> str:
    projects = config.get("projects")
    project = projects.get(project_key) if isinstance(projects, Mapping) else None
    peer = str(project.get("honcho_peer_id") or "").strip() if isinstance(project, Mapping) else ""
    if not peer or len(peer) > 128:
        raise HonchoProjectionFailure("honcho_project_peer_missing", operator_blocked=True)
    return peer


def promotable_page(page: Mapping[str, Any]) -> bool:
    fm = page.get("frontmatter")
    return bool(
        isinstance(fm, Mapping)
        and fm.get("status") == "current"
        and fm.get("confidence") == "high"
        and fm.get("sensitivity") in {"public", "internal"}
        and fm.get("impact") == "ordinary"
        and fm.get("honcho_projection") == "eligible"
        and fm.get("kind") in PROMOTABLE_KINDS
        and fm.get("source_refs")
    )


class HonchoProjectionApi:
    def __init__(self, scope: Any, *, max_items: int = 500) -> None:
        self.scope = scope
        self.max_items = max(1, min(5000, int(max_items)))

    def list(self) -> list[Any]:
        try:
            page = self.scope.list(page=1, size=min(100, self.max_items))
        except Exception as exc:
            raise HonchoProjectionFailure("honcho_projection_list_failed") from exc
        total = getattr(page, "total", None)
        if total is not None and int(total) > self.max_items:
            raise HonchoProjectionFailure("honcho_projection_remote_set_truncated", operator_blocked=True)
        result: list[Any] = []
        while page is not None:
            result.extend(list(getattr(page, "items", page) or []))
            if len(result) > self.max_items:
                raise HonchoProjectionFailure(
                    "honcho_projection_remote_set_truncated", operator_blocked=True
                )
            next_page = getattr(page, "get_next_page", None)
            page = next_page() if callable(next_page) else None
        if total is not None and len(result) != int(total):
            raise HonchoProjectionFailure("honcho_projection_remote_set_incomplete", operator_blocked=True)
        return result

    @staticmethod
    def _id(value: Any) -> str:
        return str(getattr(value, "id", None) or (value.get("id") if isinstance(value, Mapping) else "") or "")

    @staticmethod
    def _content(value: Any) -> str:
        return str(getattr(value, "content", None) or (value.get("content") if isinstance(value, Mapping) else "") or "")

    def resolve_marker(self, marker: str, content: str) -> tuple[str, str]:
        matches = [value for value in self.list() if marker in self._content(value)]
        if len(matches) > 1:
            raise HonchoProjectionFailure("honcho_projection_duplicate_remote_marker", operator_blocked=True)
        if not matches:
            return "", ""
        conclusion_id = self._id(matches[0])
        if not conclusion_id:
            raise HonchoProjectionFailure("honcho_projection_remote_identity_invalid")
        return (conclusion_id, "") if self._content(matches[0]) == content else ("", conclusion_id)

    def adopt(self, marker: str, content: str) -> str:
        exact, _stale = self.resolve_marker(marker, content)
        return exact

    def create(self, content: str) -> str:
        try:
            created = list(self.scope.create([{"content": content}]) or [])
        except Exception as exc:
            raise HonchoProjectionFailure("honcho_projection_create_uncertain") from exc
        if len(created) != 1 or not self._id(created[0]) or self._content(created[0]) != content:
            raise HonchoProjectionFailure("honcho_projection_create_identity_invalid")
        return self._id(created[0])

    def delete(self, conclusion_id: str) -> None:
        try:
            self.scope.delete(conclusion_id)
        except Exception as exc:
            message = str(exc).lower()
            if "404" not in message and "not found" not in message:
                raise HonchoProjectionFailure("honcho_projection_delete_failed") from exc
        if any(self._id(value) == conclusion_id for value in self.list()):
            raise HonchoProjectionFailure("honcho_projection_delete_unverified")


def _default_scope(observer_peer_id: str, project_peer_id: str) -> Any:
    from plugins.memory.honcho.client import get_honcho_client

    client = get_honcho_client()
    return client.peer(observer_peer_id).conclusions_of(project_peer_id)


def _projection_identity(project_key: str, slug: str) -> tuple[str, str]:
    payload = f"{project_key}\0{slug}".encode()
    return hashlib.sha256(payload).hexdigest(), "ckp:" + hashlib.sha256(payload).hexdigest()[:24]


def _page_sha256(client: GBrainClient, project_key: str, slug: str) -> str:
    relative = validate_canonical_project_slug(slug, project_key=project_key)
    root = client.assert_source_checkout()
    target = root / f"{relative}.md"
    current = root
    for component in Path(f"{relative}.md").parts:
        current = current / component
        item = os.lstat(current)
        if stat.S_ISLNK(item.st_mode):
            raise HonchoProjectionFailure("honcho_projection_page_path_unsafe", operator_blocked=True)
    if not target.is_file():
        raise HonchoProjectionFailure("honcho_projection_page_missing", operator_blocked=True)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _projection_content(project_key: str, slug: str, page: Mapping[str, Any], marker: str) -> str:
    claim_text = str(page.get("compiled_truth") or "").strip()
    if not claim_text or len(claim_text) > 1200:
        raise HonchoProjectionFailure("honcho_projection_claim_invalid", operator_blocked=True)
    punctuation = "" if claim_text.endswith((".", "!", "?")) else "."
    return (
        f"[{project_key.upper()}] {claim_text}{punctuation} "
        f"Source: gbrain:{slug} [{marker}]"
    )


def _apply_projection(
    *, store: IntakeStore, api: HonchoProjectionApi, client: GBrainClient,
    project_key: str, slug: str, page: Mapping[str, Any] | None,
) -> str:
    projection_key, marker = _projection_identity(project_key, slug)
    existing = store.get_honcho_projection(project_key, slug)
    page_sha = _page_sha256(client, project_key, slug) if page is not None else ""
    if page is None or not promotable_page(page):
        conclusion_id = str(existing.get("conclusion_id") or "") if existing else ""
        if conclusion_id:
            store.upsert_honcho_projection(
                projection_key=projection_key, project_key=project_key,
                page_slug=slug, page_sha256=page_sha, marker=marker,
                exact_content=str(existing.get("exact_content") or ""),
                state="retracting", conclusion_id=conclusion_id,
            )
            api.delete(conclusion_id)
        if existing:
            store.upsert_honcho_projection(
                projection_key=projection_key, project_key=project_key,
                page_slug=slug, page_sha256=page_sha, marker=marker,
                exact_content=str(existing.get("exact_content") or ""),
                state="retracted", obsolete_conclusion_id=conclusion_id,
            )
        return "retracted"

    content = _projection_content(project_key, slug, page, marker)
    old_id = ""
    if existing:
        if str(existing.get("exact_content") or "") == content:
            old_id = str(existing.get("obsolete_conclusion_id") or "")
        else:
            old_id = str(existing.get("conclusion_id") or existing.get("obsolete_conclusion_id") or "")
    conclusion_id, remote_stale_id = api.resolve_marker(marker, content)
    if remote_stale_id:
        if old_id and old_id != remote_stale_id:
            raise HonchoProjectionFailure(
                "honcho_projection_remote_marker_conflicts_with_ledger", operator_blocked=True
            )
        old_id = remote_stale_id
    if not conclusion_id:
        store.upsert_honcho_projection(
            projection_key=projection_key, project_key=project_key,
            page_slug=slug, page_sha256=page_sha, marker=marker,
            exact_content=content, state="creating", obsolete_conclusion_id=old_id,
        )
        conclusion_id = api.create(content)
    if old_id and old_id != conclusion_id:
        store.upsert_honcho_projection(
            projection_key=projection_key, project_key=project_key,
            page_slug=slug, page_sha256=page_sha, marker=marker,
            exact_content=content, state="retiring", conclusion_id=conclusion_id,
            obsolete_conclusion_id=old_id,
        )
        api.delete(old_id)
    store.upsert_honcho_projection(
        projection_key=projection_key, project_key=project_key,
        page_slug=slug, page_sha256=page_sha, marker=marker,
        exact_content=content, state="confirmed", conclusion_id=conclusion_id,
    )
    return conclusion_id


def _publication_slugs(store: IntakeStore, assimilation: Mapping[str, Any]) -> list[str]:
    publication = store.get_publication(str(assimilation["assimilation_id"]))
    if publication is None or publication.get("state") != "committed":
        raise HonchoProjectionFailure("honcho_projection_publication_missing", operator_blocked=True)
    try:
        manifest = json.loads(str(publication["manifest_json"]))
    except (TypeError, ValueError) as exc:
        raise HonchoProjectionFailure("honcho_projection_manifest_invalid", operator_blocked=True) from exc
    slugs: list[str] = []
    for row in manifest:
        path = str(row.get("path") or "") if isinstance(row, Mapping) else ""
        if not path.endswith(".md"):
            raise HonchoProjectionFailure("honcho_projection_manifest_invalid", operator_blocked=True)
        slug = path[:-3]
        validate_canonical_project_slug(slug, project_key=str(assimilation["project_key"]))
        slugs.append(slug)
    return list(dict.fromkeys(slugs))


def _source_project_slugs(client: GBrainClient, project_key: str, limit: int) -> list[str]:
    root = client.assert_source_checkout()
    project_root = root / "projects" / project_key
    if not project_root.exists():
        return []
    slugs: list[str] = []
    pending = [project_root]
    while pending:
        current = pending.pop()
        for entry in os.scandir(current):
            if entry.is_symlink():
                raise HonchoProjectionFailure("honcho_projection_page_path_unsafe", operator_blocked=True)
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"):
                relative = Path(entry.path).relative_to(root).as_posix()[:-3]
                validate_canonical_project_slug(relative, project_key=project_key)
                slugs.append(relative)
                if len(slugs) > limit:
                    raise HonchoProjectionFailure("honcho_reconcile_page_limit", operator_blocked=True)
    return sorted(slugs)


class HonchoProjectionWorker:
    def __init__(
        self,
        store: IntakeStore,
        derived: DerivedStore,
        client: GBrainClient,
        settings: HonchoProjectionSettings,
        config: Mapping[str, Any],
        *,
        scope_factory: Any = _default_scope,
    ) -> None:
        self.store = store
        self.derived = derived
        self.client = client
        self.settings = settings
        self.config = config
        self.scope_factory = scope_factory

    def process_claim(self, claim: JobClaim) -> str:
        artifact, assimilation = self.store.get_assimilation_for_projection_claim(claim)
        value = self.derived.read_json(
            "assimilations", assimilation["assimilation_id"],
            assimilation["output_sha256"], assimilation["output_bytes"],
        )
        proposal = value["proposal"]
        project_peer = _project_peer(self.config, artifact.project_key)
        api = HonchoProjectionApi(
            self.scope_factory(self.settings.observer_peer_id, project_peer),
            max_items=self.settings.max_remote_conclusions,
        )
        receipt_parts: list[str] = []
        for slug in _publication_slugs(self.store, assimilation):
            page = validate_page(
                self.client.get_page(slug), project_key=artifact.project_key,
                source_id=self.client.settings.source_id,
            )
            receipt_parts.append(
                f"{slug}:{_apply_projection(store=self.store, api=api, client=self.client, project_key=artifact.project_key, slug=slug, page=page)}"
            )
        digest = hashlib.sha256("\n".join(sorted(receipt_parts)).encode()).hexdigest()
        self.store.complete_honcho_projection(
            claim, receipt_id=f"honcho:{assimilation['assimilation_id']}", output_sha256=digest
        )
        return str(assimilation["assimilation_id"])


def reconcile_honcho_project(
    *, store: IntakeStore, client: GBrainClient, project_key: str,
    config: Mapping[str, Any], scope_factory: Any = _default_scope,
) -> dict[str, int]:
    project_key = validate_project_key(project_key)
    settings = HonchoProjectionSettings.from_config(config)
    project_peer = _project_peer(config, project_key)
    api = HonchoProjectionApi(
        scope_factory(settings.observer_peer_id, project_peer),
        max_items=settings.max_remote_conclusions,
    )
    result = {"confirmed": 0, "retracted": 0}
    limit = max(1, min(5000, int(
        config.get("client_knowledge", {}).get("honcho_projection", {}).get(
            "max_reconcile_pages", 500
        )
    )))
    slugs = _source_project_slugs(client, project_key, limit)
    seen = set(slugs)
    for slug in slugs:
        page = validate_page(
            client.get_page(slug), project_key=project_key,
            source_id=client.settings.source_id,
        )
        outcome = _apply_projection(
            store=store, api=api, client=client, project_key=project_key,
            slug=slug, page=page,
        )
        result["retracted" if outcome == "retracted" else "confirmed"] += 1
    for row in store.list_honcho_projections(project_key):
        if str(row["page_slug"]) in seen:
            continue
        _apply_projection(
            store=store, api=api, client=client, project_key=project_key,
            slug=str(row["page_slug"]), page=None,
        )
        result["retracted"] += 1
    return result


def run_honcho_projection_once(
    *, store: IntakeStore, derived: DerivedStore, client: GBrainClient | None = None,
    config: Mapping[str, Any] | None = None, scope_factory: Any = _default_scope,
) -> dict[str, int]:
    effective = dict(config or load_config() or {})
    settings = HonchoProjectionSettings.from_config(effective)
    result = {"processed": 0, "succeeded": 0, "failed": 0, "operator_blocked": 0}
    if not settings.enabled:
        return result
    gbrain = client or GBrainClient(load_settings(effective))
    worker = HonchoProjectionWorker(
        store, derived, gbrain, settings, effective, scope_factory=scope_factory
    )
    spool = RawSpool()
    for _ in range(settings.max_jobs_per_run):
        claim = store.claim_next(
            stage="honcho_projected", spool=spool, lease_seconds=settings.lease_seconds
        )
        if claim is None:
            break
        result["processed"] += 1
        try:
            worker.process_claim(claim)
            result["succeeded"] += 1
        except HonchoProjectionFailure as exc:
            if exc.operator_blocked:
                if store.block_stage(claim.job_id, claim.claim_token, error_class=exc.error_class):
                    result["operator_blocked"] += 1
            elif store.fail_stage(
                claim.job_id, claim.claim_token, error_class=exc.error_class,
                retry_delay=settings.retry_delay_seconds,
            ):
                result["failed"] += 1
    return result


__all__ = [
    "HonchoProjectionApi", "HonchoProjectionFailure", "HonchoProjectionSettings",
    "HonchoProjectionWorker", "PROMOTABLE_KINDS", "promotable_page",
    "reconcile_honcho_project", "run_honcho_projection_once",
]

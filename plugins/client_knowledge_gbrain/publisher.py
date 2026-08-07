"""Hardened Git publisher for one project-scoped GBrain source checkout."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .client import GBrainClient, _assert_nonsymlink_directory
from .scope import full_project_slug, validate_project_key


class PublicationFailure(RuntimeError):
    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


@dataclass(frozen=True, slots=True)
class PublicationFile:
    relative_slug: str
    content: bytes | None
    expected_prior_sha256: str = ""


@dataclass(frozen=True, slots=True)
class PublicationResult:
    commit_sha: str
    manifest_json: str
    changed_paths: tuple[str, ...]


class GitSourcePublisher:
    """Create one audited commit and move one branch with compare-and-swap."""

    def __init__(
        self,
        client: GBrainClient,
        *,
        project_key: str,
        store: Any | None = None,
    ) -> None:
        self.client = client
        self.project_key = validate_project_key(project_key)
        configured_root = getattr(client.settings, "source_checkout", None)
        if configured_root is not None:
            _assert_nonsymlink_directory(
                Path(configured_root), "configured GBrain source checkout"
            )
        self.root = Path(client.assert_source_checkout())
        _assert_nonsymlink_directory(self.root, "configured GBrain source checkout")
        self.branch = client.settings.source_branch
        self.store = store
        self.git = shutil.which("git")
        if not self.git:
            raise PublicationFailure("git_unavailable")
        self._private = Path(tempfile.mkdtemp(prefix="client-knowledge-git-"))
        (self._private / "home").mkdir(mode=0o700)
        (self._private / "config").mkdir(mode=0o700)
        (self._private / "hooks").mkdir(mode=0o700)
        self.env = {
            "HOME": str(self._private / "home"),
            "XDG_CONFIG_HOME": str(self._private / "config"),
            "PATH": str(Path(self.git).parent),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }

    def close(self) -> None:
        shutil.rmtree(self._private, ignore_errors=True)

    def __enter__(self) -> "GitSourcePublisher":
        self._validate_repository()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _git(self, *args: str, input_bytes: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
        process_env = dict(self.env)
        process_env.update(env or {})
        command = [
            str(self.git),
            "-c", f"core.hooksPath={self._private / 'hooks'}",
            "-c", "commit.gpgSign=false",
            "-c", "tag.gpgSign=false",
            "-c", f"core.attributesFile={os.devnull}",
            "-c", "diff.external=",
            "-C", str(self.root),
            *args,
        ]
        completed = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_env,
            timeout=max(5.0, self.client.settings.timeout_seconds),
            check=False,
        )
        if completed.returncode != 0:
            raise PublicationFailure("git_command_failed")
        return completed.stdout

    def _git_text(
        self,
        *args: str,
        input_bytes: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        return self._git(*args, input_bytes=input_bytes, env=env).decode(
            "utf-8", "strict"
        ).strip()

    def _git_quiet_clean(self, *args: str) -> bool:
        command = [
            str(self.git), "-c", f"core.hooksPath={self._private / 'hooks'}",
            "-c", f"core.attributesFile={os.devnull}", "-C", str(self.root), *args,
        ]
        completed = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=self.env,
            timeout=max(5.0, self.client.settings.timeout_seconds), check=False,
        )
        if completed.returncode not in {0, 1}:
            raise PublicationFailure("git_command_failed")
        return completed.returncode == 0

    def _validate_repository(self) -> None:
        _assert_nonsymlink_directory(self.root, "configured GBrain source checkout")
        dot_git = self.root / ".git"
        item = os.lstat(dot_git)
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
            raise PublicationFailure("git_metadata_not_contained")
        self._reject_symlinks_below(dot_git)
        if self._git_text("rev-parse", "--is-bare-repository") != "false":
            raise PublicationFailure("git_source_is_bare")
        top = Path(self._git_text("rev-parse", "--show-toplevel"))
        git_dir = Path(self._git_text("rev-parse", "--absolute-git-dir"))
        common_raw = Path(self._git_text("rev-parse", "--git-common-dir"))
        common = common_raw if common_raw.is_absolute() else self.root / common_raw
        if top != self.root or git_dir != dot_git or common.resolve() != dot_git:
            raise PublicationFailure("git_metadata_not_contained")
        if self._git_text("rev-parse", "--symbolic-full-name", "HEAD") != self.branch_ref:
            raise PublicationFailure("git_branch_mismatch")
        for unsafe in (
            dot_git / "objects" / "info" / "alternates",
            dot_git / "objects" / "info" / "http-alternates",
            dot_git / "info" / "grafts",
        ):
            if unsafe.exists() or unsafe.is_symlink():
                raise PublicationFailure("git_external_object_metadata")
        if self._git_text("replace", "-l"):
            raise PublicationFailure("git_replace_refs_present")
        local = self._git_text("config", "--local", "--list", "--name-only").splitlines()
        rejected_prefixes = ("include.", "includeif.", "filter.")
        rejected_exact = {
            "core.hookspath", "extensions.worktreeconfig", "diff.external",
        }
        if any(
            key.lower() in rejected_exact
            or key.lower().startswith(rejected_prefixes)
            or key.lower().endswith(".textconv")
            for key in local
        ):
            raise PublicationFailure("git_local_config_unsafe")

    @property
    def branch_ref(self) -> str:
        parts = self.branch.split("/")
        if (
            not self.branch
            or self.branch.startswith(("-", ".", "/"))
            or self.branch.endswith(("/", ".", ".lock"))
            or ".." in self.branch
            or "@{" in self.branch
            or any(not part or part.startswith(".") or part.endswith((".", ".lock")) for part in parts)
            or any(char in self.branch for char in " ~^:?*[\\")
            or any(ord(char) < 32 or ord(char) == 127 for char in self.branch)
        ):
            raise PublicationFailure("git_branch_invalid")
        return f"refs/heads/{self.branch}"

    @staticmethod
    def _reject_symlinks_below(root: Path) -> None:
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                entries = list(os.scandir(current))
            except OSError as exc:
                raise PublicationFailure("git_metadata_not_contained") from exc
            for entry in entries:
                try:
                    if entry.is_symlink():
                        raise PublicationFailure("git_metadata_symlink_present")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                except OSError as exc:
                    raise PublicationFailure("git_metadata_not_contained") from exc

    def head(self) -> str:
        return self._git_text("rev-parse", "HEAD")

    def _config_hash(self) -> str:
        data = (self.root / ".git" / "config").read_bytes()
        return hashlib.sha256(data).hexdigest()

    def _workspace_snapshot(self, expected_head: str, *, require_branch: bool = True) -> dict[str, str]:
        if require_branch and self._git_text("rev-parse", self.branch_ref) != expected_head:
            raise PublicationFailure("git_head_changed")
        expected_tree = self._git_text("rev-parse", f"{expected_head}^{{tree}}")
        index_tree = self._git_text("write-tree")
        if index_tree != expected_tree:
            raise PublicationFailure("git_index_not_clean")
        if not self._git_quiet_clean("diff-index", "--cached", "--quiet", expected_head):
            raise PublicationFailure("git_index_not_clean")
        if not self._git_quiet_clean("diff-files", "--quiet"):
            raise PublicationFailure("git_worktree_not_clean")
        untracked = self._git("ls-files", "--others", "-z")
        if untracked:
            raise PublicationFailure("git_untracked_files_present")
        stage = self._git("ls-files", "--stage", "-z")
        if b" 0000000000000000000000000000000000000000 " in stage:
            raise PublicationFailure("git_index_intent_to_add")
        flags = self._git_text("ls-files", "-v").splitlines()
        if any(line and line[0] != "H" for line in flags):
            raise PublicationFailure("git_hidden_index_state")
        index = self.root / ".git" / "index"
        index_data = index.read_bytes() if index.exists() else b""
        tracked = self._git("ls-files", "--stage", "-z")
        return {
            "expected_tree": expected_tree,
            "index_tree": index_tree,
            "index_sha256": hashlib.sha256(index_data).hexdigest(),
            "tracked_sha256": hashlib.sha256(tracked).hexdigest(),
            "config_sha256": self._config_hash(),
        }

    def _path_for_slug(self, relative_slug: str) -> tuple[str, Path]:
        slug = full_project_slug(self.project_key, relative_slug)
        git_path = f"{slug}.md"
        current = self.root
        for component in Path(git_path).parts[:-1]:
            current = current / component
            if current.exists() or current.is_symlink():
                item = os.lstat(current)
                if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
                    raise PublicationFailure("publication_path_unsafe")
        target = self.root / git_path
        if target.exists() or target.is_symlink():
            item = os.lstat(target)
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
                raise PublicationFailure("publication_path_unsafe")
        return git_path, target

    def _assert_target_path_safe(self, git_path: str) -> Path:
        prefix = f"projects/{self.project_key}/"
        if not git_path.startswith(prefix) or not git_path.endswith(".md"):
            raise PublicationFailure("publication_path_unsafe")
        target = self.root / git_path
        current = self.root
        for component in Path(git_path).parts[:-1]:
            current = current / component
            if current.exists() or current.is_symlink():
                item = os.lstat(current)
                if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
                    raise PublicationFailure("publication_path_unsafe")
        if target.exists() or target.is_symlink():
            item = os.lstat(target)
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
                raise PublicationFailure("publication_path_unsafe")
        return target

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _prior_bytes(self, path: str, expected_head: str) -> bytes | None:
        completed = subprocess.run(
            [
                str(self.git), "-c", f"core.hooksPath={self._private / 'hooks'}",
                "-C", str(self.root), "show", f"{expected_head}:{path}",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
            timeout=max(5.0, self.client.settings.timeout_seconds), check=False,
        )
        if completed.returncode == 0:
            return completed.stdout
        return None

    def _manifest(
        self, files: Iterable[PublicationFile], expected_head: str
    ) -> tuple[list[dict[str, Any]], dict[str, bytes | None], dict[str, Path]]:
        manifest: list[dict[str, Any]] = []
        contents: dict[str, bytes | None] = {}
        targets: dict[str, Path] = {}
        for item in files:
            path, target = self._path_for_slug(item.relative_slug)
            if path in contents:
                raise PublicationFailure("publication_duplicate_path")
            prior = self._prior_bytes(path, expected_head)
            prior_sha = self._sha256(prior) if prior is not None else ""
            if item.expected_prior_sha256 != prior_sha:
                raise PublicationFailure("publication_prior_hash_mismatch")
            content = None if item.content is None else bytes(item.content)
            if content is not None:
                try:
                    content.decode("utf-8", "strict")
                except UnicodeError as exc:
                    raise PublicationFailure("publication_content_not_utf8") from exc
            manifest.append(
                {
                    "path": path,
                    "action": "delete" if content is None else ("add" if prior is None else "modify"),
                    "prior_sha256": prior_sha,
                    "content_sha256": self._sha256(content) if content is not None else "",
                }
            )
            contents[path] = content
            targets[path] = target
        if not manifest:
            raise PublicationFailure("publication_manifest_empty")
        return sorted(manifest, key=lambda row: row["path"]), contents, targets

    def _audit_commit(self, expected_head: str, commit_sha: str, manifest: list[dict[str, Any]]) -> None:
        parents = self._git_text("rev-list", "--parents", "-n", "1", commit_sha).split()
        if parents != [commit_sha, expected_head]:
            raise PublicationFailure("publication_commit_parent_mismatch")
        raw = self._git("diff-tree", "--name-status", "-r", "--no-renames", "--no-commit-id", "-z", expected_head, commit_sha)
        parts = raw.decode("utf-8", "strict").split("\0")
        changed: dict[str, str] = {}
        for index in range(0, len(parts) - 1, 2):
            status_code, path = parts[index], parts[index + 1]
            if status_code and path:
                changed[path] = status_code
        expected = {
            row["path"]: {"add": "A", "modify": "M", "delete": "D"}[row["action"]]
            for row in manifest
        }
        if changed != expected:
            raise PublicationFailure("publication_changed_path_audit_failed")
        for row in manifest:
            if row["action"] == "delete":
                continue
            entry = self._git_text("ls-tree", commit_sha, "--", row["path"]).split()
            if len(entry) < 4 or entry[0] != "100644" or entry[1] != "blob":
                raise PublicationFailure("publication_blob_mode_invalid")
            blob = self._git("show", f"{commit_sha}:{row['path']}")
            if self._sha256(blob) != row["content_sha256"]:
                raise PublicationFailure("publication_blob_hash_mismatch")

    def _materialization_state_is_recoverable(
        self,
        expected_head: str,
        manifest: list[dict[str, Any]],
        contents: Mapping[str, bytes | None],
        targets: Mapping[str, Path],
    ) -> bool:
        try:
            if self._git_text("write-tree") != self._git_text(
                "rev-parse", f"{expected_head}^{{tree}}"
            ):
                return False
            if not self._git_quiet_clean("diff-index", "--cached", "--quiet", expected_head):
                return False
            changed = set(
                value.decode("utf-8", "strict")
                for value in self._git("diff-files", "--name-only", "-z").split(b"\0")
                if value
            )
            untracked = set(
                value.decode("utf-8", "strict")
                for value in self._git("ls-files", "--others", "-z").split(b"\0")
                if value
            )
        except (PublicationFailure, UnicodeError):
            return False
        expected_paths = {row["path"] for row in manifest}
        if not changed.issubset(expected_paths) or not untracked.issubset(expected_paths):
            return False
        for row in manifest:
            path = row["path"]
            target = targets[path]
            try:
                self._assert_target_path_safe(path)
            except PublicationFailure:
                return False
            if target.exists():
                try:
                    actual = target.read_bytes()
                except OSError:
                    return False
                actual_sha = self._sha256(actual)
            else:
                actual_sha = ""
            desired = contents[path]
            desired_sha = self._sha256(desired) if desired is not None else ""
            if actual_sha not in {row["prior_sha256"], desired_sha}:
                return False
        return True

    def _materialize_commit(
        self,
        *,
        commit_sha: str,
        assimilation_id: str,
        manifest: list[dict[str, Any]],
        contents: Mapping[str, bytes | None],
        targets: Mapping[str, Path],
    ) -> None:
        for row in manifest:
            path = row["path"]
            target = targets[path]
            content = contents[path]
            target.parent.mkdir(parents=True, exist_ok=True)
            self._assert_target_path_safe(path)
            if content is None:
                if target.exists():
                    target.unlink()
                continue
            temporary = target.parent / f".{target.name}.tmp-{assimilation_id[:12]}"
            if temporary.exists() or temporary.is_symlink():
                raise PublicationFailure("publication_temporary_path_exists")
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._assert_target_path_safe(path)
                os.replace(temporary, target)
            finally:
                if fd >= 0:
                    os.close(fd)
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()
        self._git("read-tree", commit_sha)
        if self._git_text("write-tree") != self._git_text(
            "rev-parse", f"{commit_sha}^{{tree}}"
        ):
            raise PublicationFailure("git_materialization_index_mismatch")
        self._git("update-index", "--refresh")
        if (
            not self._git_quiet_clean("diff-files", "--quiet")
            or self._git("ls-files", "--others", "-z")
        ):
            raise PublicationFailure("git_materialization_not_clean")

    def publish(
        self,
        *,
        artifact_id: str,
        assimilation_id: str,
        assimilation_version: str,
        proposal_sha256: str,
        expected_head: str,
        authored_at: int,
        files: Iterable[PublicationFile],
        review_id: str = "",
        interpretation_id: str = "",
    ) -> PublicationResult:
        self._validate_repository()
        manifest, contents, targets = self._manifest(files, expected_head)
        manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        if self.store is not None:
            self.store.record_publication(
                assimilation_id=assimilation_id, artifact_id=artifact_id,
                assimilation_version=assimilation_version, proposal_sha256=proposal_sha256,
                branch_ref=self.branch_ref, expected_head=expected_head,
                manifest_json=manifest_json, state="prepared",
            )
        current_head = self._git_text("rev-parse", self.branch_ref)
        first_snapshot: dict[str, str] | None = None
        if current_head != expected_head:
            message = self._git_text("show", "-s", "--format=%B", current_head)
            trailers = (
                f"Client-Knowledge-Artifact: {artifact_id}",
                f"Client-Knowledge-Assimilation: {assimilation_id}",
                f"Client-Knowledge-Assimilation-Version: {assimilation_version}",
                f"Client-Knowledge-Proposal-SHA256: {proposal_sha256}",
            )
            if all(value in message for value in trailers):
                self._audit_commit(expected_head, current_head, manifest)
                commit_sha = current_head
                try:
                    self._workspace_snapshot(commit_sha)
                except PublicationFailure:
                    if not self._materialization_state_is_recoverable(
                        expected_head, manifest, contents, targets
                    ):
                        raise PublicationFailure("git_adoption_workspace_unsafe")
                    self._materialize_commit(
                        commit_sha=commit_sha,
                        assimilation_id=assimilation_id,
                        manifest=manifest,
                        contents=contents,
                        targets=targets,
                    )
                if self.store is not None:
                    self.store.record_publication(
                        assimilation_id=assimilation_id,
                        artifact_id=artifact_id,
                        assimilation_version=assimilation_version,
                        proposal_sha256=proposal_sha256,
                        branch_ref=self.branch_ref,
                        expected_head=expected_head,
                        manifest_json=manifest_json,
                        state="committed",
                        commit_sha=commit_sha,
                    )
                return PublicationResult(
                    commit_sha,
                    manifest_json,
                    tuple(row["path"] for row in manifest),
                )
            else:
                raise PublicationFailure("git_head_changed")
        else:
            first_snapshot = self._workspace_snapshot(expected_head)
            index_path = self._private / "index"
            temp_env = {"GIT_INDEX_FILE": str(index_path)}
            self._git("read-tree", expected_head, env=temp_env)
            for row in manifest:
                path = row["path"]
                content = contents[path]
                if content is None:
                    self._git("update-index", "--force-remove", "--", path, env=temp_env)
                else:
                    blob = self._git_text("hash-object", "-w", "--no-filters", "--stdin", input_bytes=content, env=temp_env)
                    self._git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=temp_env)
            tree = self._git_text("write-tree", env=temp_env)
            commit_message = (
                f"client-knowledge: assimilate {artifact_id[:12]}\n\n"
                f"Client-Knowledge-Artifact: {artifact_id}\n"
                f"Client-Knowledge-Interpretation: {interpretation_id or 'unknown'}\n"
                f"Client-Knowledge-Assimilation: {assimilation_id}\n"
                f"Client-Knowledge-Assimilation-Version: {assimilation_version}\n"
                f"Client-Knowledge-Proposal-SHA256: {proposal_sha256}\n"
                f"Client-Knowledge-Review: {review_id or 'none'}\n"
            ).encode("utf-8")
            authored = f"@{max(1, int(authored_at))} +0000"
            commit_env = {
                "GIT_AUTHOR_NAME": "Hermes Client Knowledge",
                "GIT_AUTHOR_EMAIL": "client-knowledge@localhost",
                "GIT_COMMITTER_NAME": "Hermes Client Knowledge",
                "GIT_COMMITTER_EMAIL": "client-knowledge@localhost",
                "GIT_AUTHOR_DATE": authored,
                "GIT_COMMITTER_DATE": authored,
            }
            commit_sha = self._git_text(
                "commit-tree", tree, "-p", expected_head,
                input_bytes=commit_message, env=commit_env,
            )
            self._audit_commit(expected_head, commit_sha, manifest)
            second_snapshot = self._workspace_snapshot(expected_head)
            if second_snapshot != first_snapshot:
                raise PublicationFailure("git_workspace_changed_before_cas")
            try:
                self._git("update-ref", self.branch_ref, commit_sha, expected_head)
            except PublicationFailure as exc:
                raise PublicationFailure("git_cas_failed") from exc
        assert first_snapshot is not None
        post_cas = self._workspace_snapshot(expected_head, require_branch=False)
        if post_cas != first_snapshot:
            if self.store is not None:
                self.store.record_publication(
                    assimilation_id=assimilation_id, artifact_id=artifact_id,
                    assimilation_version=assimilation_version, proposal_sha256=proposal_sha256,
                    branch_ref=self.branch_ref, expected_head=expected_head,
                    manifest_json=manifest_json, state="cas_succeeded_materialization_blocked",
                    commit_sha=commit_sha, error_class="git_workspace_changed_after_cas",
                )
            raise PublicationFailure("git_workspace_changed_after_cas")
        if not self._materialization_state_is_recoverable(
            expected_head, manifest, contents, targets
        ):
            raise PublicationFailure("git_target_changed_before_materialization")
        self._materialize_commit(
            commit_sha=commit_sha,
            assimilation_id=assimilation_id,
            manifest=manifest,
            contents=contents,
            targets=targets,
        )
        if self.store is not None:
            self.store.record_publication(
                assimilation_id=assimilation_id, artifact_id=artifact_id,
                assimilation_version=assimilation_version, proposal_sha256=proposal_sha256,
                branch_ref=self.branch_ref, expected_head=expected_head,
                manifest_json=manifest_json, state="committed", commit_sha=commit_sha,
            )
        return PublicationResult(commit_sha, manifest_json, tuple(row["path"] for row in manifest))


__all__ = [
    "GitSourcePublisher", "PublicationFailure", "PublicationFile", "PublicationResult",
]

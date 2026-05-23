"""Codex worker credential inheritance helpers."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CodexWorkerHomeLease:
    """Temporary Codex home containing short-lived worker credentials."""

    def __init__(self, tmp_dir: tempfile.TemporaryDirectory[str], credential_id: Optional[str]):
        self._tmp_dir = tmp_dir
        self.path = Path(tmp_dir.name)
        self.credential_id = credential_id

    def cleanup(self) -> None:
        self._tmp_dir.cleanup()

    def __enter__(self) -> "CodexWorkerHomeLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()


def _codex_worker_home_root() -> Path:
    from hermes_constants import get_hermes_home

    return (get_hermes_home() / "tmp" / "codex-worker-homes").resolve()


def _string_attr(entry: Any, name: str) -> str:
    value = getattr(entry, name, "")
    return value.strip() if isinstance(value, str) else ""


def _entry_tokens(entry: Any) -> Optional[dict[str, str]]:
    access_token = _string_attr(entry, "access_token")
    refresh_token = _string_attr(entry, "refresh_token")
    id_token = _string_attr(entry, "id_token")
    if not access_token or not refresh_token or not id_token:
        return None
    tokens = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
    }
    account_id = _string_attr(entry, "account_id")
    if account_id:
        tokens["account_id"] = account_id
    return tokens


def _entry_is_usable(entry: Any) -> bool:
    if entry is None:
        return False
    if str(getattr(entry, "last_status", "") or "").strip().lower() == "exhausted":
        return False
    return _entry_tokens(entry) is not None


def _load_codex_pool():
    try:
        from agent.credential_pool import load_pool

        pool = load_pool("openai-codex")
        if pool is not None and pool.has_credentials():
            return pool
    except Exception as exc:
        logger.debug("Could not load openai-codex credential pool: %s", exc)
    return None


def _select_usable_pool_entry(pool: Any) -> Any:
    try:
        entry = pool.current()
    except Exception:
        entry = None
    if _entry_is_usable(entry):
        return entry

    seen_ids = {getattr(entry, "id", None)} if entry is not None else set()
    try:
        entry = pool.select()
    except Exception as exc:
        logger.debug("Could not select openai-codex worker credential: %s", exc)
        entry = None
    if _entry_is_usable(entry):
        return entry
    if entry is not None:
        seen_ids.add(getattr(entry, "id", None))

    try:
        entries = pool.entries()
    except Exception:
        entries = []
    for entry in entries:
        if getattr(entry, "id", None) in seen_ids:
            continue
        if _entry_is_usable(entry):
            return entry
    return None


def select_codex_worker_credential(parent_agent: Any = None) -> tuple[Any, Any]:
    """Return ``(pool, entry)`` for the Codex credential a worker should use.

    Prefer the parent agent's active ``openai-codex`` pool entry. If the caller
    is not itself running on that provider, fall back to loading the
    ``openai-codex`` pool and selecting the first currently available entry.
    """
    pool = None
    if parent_agent is not None:
        parent_provider = str(getattr(parent_agent, "provider", "") or "").strip().lower()
        if parent_provider == "openai-codex":
            pool = getattr(parent_agent, "_credential_pool", None)

    if pool is None:
        pool = _load_codex_pool()
    if pool is None:
        return None, None

    entry = _select_usable_pool_entry(pool)
    if entry is not None:
        return pool, entry
    return pool, None


def _copy_codex_file(
    codex_home: Path,
    name: str,
    *,
    source_env: dict[str, str] | None = None,
    overwrite: bool = False,
) -> None:
    env = os.environ if source_env is None else source_env
    source_home = Path(env.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    src = source_home / name
    dst = codex_home / name
    if src.exists() and (overwrite or not dst.exists()):
        try:
            shutil.copy2(src, dst)
        except OSError:
            pass


def _codex_auth_has_id_token(codex_home: Path) -> bool:
    auth_path = codex_home / "auth.json"
    if not auth_path.is_file():
        return False
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    return isinstance(tokens, dict) and bool(str(tokens.get("id_token", "") or "").strip())


def _write_minimal_config(codex_home: Path) -> None:
    config = codex_home / "config.toml"
    if config.exists():
        return
    config.write_text(
        "\n".join(
            [
                'sandbox_mode = "workspace-write"',
                'approval_policy = "never"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_codex_auth(codex_home: Path, entry: Any) -> bool:
    tokens = _entry_tokens(entry)
    if tokens is None:
        return False

    auth_path = codex_home / "auth.json"
    payload: dict[str, Any] = {}
    if auth_path.exists():
        try:
            loaded = json.loads(auth_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except Exception:
            payload = {}
    payload["tokens"] = tokens
    payload.setdefault("auth_mode", "chatgpt")
    last_refresh = getattr(entry, "last_refresh", None)
    if last_refresh:
        payload["last_refresh"] = last_refresh
    auth_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        auth_path.chmod(0o600)
    except OSError:
        pass
    return True


def prepare_codex_worker_home(
    codex_home: Path | str,
    *,
    parent_agent: Any = None,
    source_env: dict[str, str] | None = None,
    allow_fallback: bool = True,
) -> Optional[str]:
    """Prepare an isolated Codex home and return inherited credential id.

    The active Hermes ``openai-codex`` credential wins. If no pool credential is
    available and ``allow_fallback`` is true, this preserves the previous
    behavior of copying the user's existing Codex auth files when present.
    Pool credentials without ``id_token`` are treated as unavailable because
    Codex CLI rejects those auth files before it can refresh them.
    ``source_env`` lets callers choose the environment used for that fallback
    without mutating global state.
    """
    path = Path(codex_home).expanduser()
    path.mkdir(parents=True, exist_ok=True)

    pool, entry = select_codex_worker_credential(parent_agent)
    credential_id = None
    copied_fallback_auth = False
    if entry is not None and _write_codex_auth(path, entry):
        credential_id = str(getattr(entry, "id", "") or "").strip() or None
        logger.info(
            "Codex worker inheriting openai-codex pool credential %s",
            credential_id or "<unknown>",
        )
    elif allow_fallback:
        _copy_codex_file(
            path,
            "auth.json",
            source_env=source_env,
            overwrite=not _codex_auth_has_id_token(path),
        )
        copied_fallback_auth = True

    if copied_fallback_auth:
        _copy_codex_file(path, "credentials.json", source_env=source_env)
    _write_minimal_config(path)
    return credential_id


def create_codex_worker_home(
    *,
    parent_agent: Any = None,
    source_env: dict[str, str] | None = None,
    allow_fallback: bool = True,
    prefix: str = "codex-worker-",
) -> CodexWorkerHomeLease:
    """Create a temporary Codex home lease populated with worker auth."""
    root = _codex_worker_home_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    tmp_dir = tempfile.TemporaryDirectory(prefix=prefix, dir=str(root))
    try:
        credential_id = prepare_codex_worker_home(
            Path(tmp_dir.name),
            parent_agent=parent_agent,
            source_env=source_env,
            allow_fallback=allow_fallback,
        )
    except Exception:
        tmp_dir.cleanup()
        raise
    return CodexWorkerHomeLease(tmp_dir, credential_id)


def cleanup_codex_worker_home(codex_home: Path | str | None) -> None:
    """Best-effort removal for detached worker homes after token sync."""
    if not codex_home:
        return
    cleanup_root = os.environ.get("HERMES_CODEX_WORKER_CLEANUP_ROOT", "").strip()
    root = Path(cleanup_root).expanduser().resolve() if cleanup_root else _codex_worker_home_root()
    path = Path(codex_home).expanduser().resolve()
    allowed = path == root if cleanup_root else path != root and root in path.parents
    if not allowed:
        logger.warning("Refusing to clean non-worker Codex home: %s", path)
        return
    try:
        shutil.rmtree(path)
    except OSError:
        try:
            for child in path.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            path.rmdir()
        except OSError:
            pass


def mark_codex_worker_credential_auth_failed(
    credential_id: str | None,
    *,
    message: str | None = None,
) -> bool:
    """Mark the worker's pool credential exhausted after a Codex auth failure."""
    credential_id = str(credential_id or "").strip()
    if not credential_id:
        return False
    try:
        from agent.credential_pool import load_pool

        pool = load_pool("openai-codex")
    except Exception as exc:
        logger.debug("Could not load openai-codex pool for worker auth failure: %s", exc)
        return False

    try:
        with pool._lock:  # type: ignore[attr-defined]
            for entry in pool._entries:  # type: ignore[attr-defined]
                if getattr(entry, "id", None) != credential_id:
                    continue
                if getattr(pool, "_current_id", None) == credential_id:
                    pool._current_id = None  # type: ignore[attr-defined]
                pool._mark_exhausted(  # type: ignore[attr-defined]
                    entry,
                    401,
                    {
                        "reason": "worker_auth_failed",
                        "message": message or "Codex worker authentication failed.",
                    },
                )
                return True
    except Exception as exc:
        logger.debug("Could not mark Codex worker credential %s failed: %s", credential_id, exc)
    return False


def sync_codex_worker_home(
    codex_home: Path | str | None,
    credential_id: str | None,
) -> None:
    """Best-effort sync of worker-refreshed Codex OAuth tokens back to Hermes."""
    if not codex_home or not credential_id:
        return
    auth_path = Path(codex_home).expanduser() / "auth.json"
    if not auth_path.is_file():
        return
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:
        return
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        return
    access_token = str(tokens.get("access_token", "") or "").strip()
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    id_token = str(tokens.get("id_token", "") or "").strip()
    if not access_token or not refresh_token:
        return

    try:
        from agent.credential_pool import load_pool

        pool = load_pool("openai-codex")
    except Exception as exc:
        logger.debug("Could not reload openai-codex pool for worker sync: %s", exc)
        return

    updated_entry = None
    try:
        with pool._lock:  # type: ignore[attr-defined]
            for entry in pool._entries:  # type: ignore[attr-defined]
                if getattr(entry, "id", None) != credential_id:
                    continue
                if (
                    getattr(entry, "access_token", None) == access_token
                    and getattr(entry, "refresh_token", None) == refresh_token
                ):
                    return
                extra = dict(getattr(entry, "extra", {}) or {})
                if id_token:
                    extra["id_token"] = id_token
                account_id = str(tokens.get("account_id", "") or "").strip()
                if account_id:
                    extra["account_id"] = account_id
                updated_entry = replace(
                    entry,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    extra=extra,
                    last_status=None,
                    last_status_at=None,
                    last_error_code=None,
                    last_error_reason=None,
                    last_error_message=None,
                    last_error_reset_at=None,
                )
                pool._replace_entry(entry, updated_entry)  # type: ignore[attr-defined]
                pool._persist()  # type: ignore[attr-defined]
                break
    except Exception as exc:
        logger.debug("Could not sync Codex worker credential %s: %s", credential_id, exc)
        return

    if updated_entry is not None and getattr(updated_entry, "source", None) == "device_code":
        try:
            from hermes_cli.auth import _save_codex_tokens

            _save_codex_tokens(dict(tokens))
        except Exception as exc:
            logger.debug("Could not sync Codex singleton credentials: %s", exc)

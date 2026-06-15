"""Read-only Kanban board filesystem health diagnostics."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db as kb


def _safe_stat(path: Path) -> tuple[bool, int | None, str | None]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False, None, None
    except OSError as exc:
        return False, None, f"stat_error: {exc}"
    return True, int(stat.st_size), None


def _read_header(path: Path) -> tuple[bytes | None, str | None]:
    try:
        with path.open("rb") as handle:
            return handle.read(64), None
    except OSError as exc:
        return None, f"read_error: {exc}"


def _classify_sqlite_header(path: Path, exists: bool, size: int | None) -> tuple[str, str | None]:
    if not exists:
        return "missing", None
    if size == 0:
        return "zero_byte", None
    if size is None:
        return "unreadable", "size unavailable"
    header, read_error = _read_header(path)
    if read_error:
        return "unreadable", read_error
    if header is not None and header.startswith(kb._SQLITE_HEADER):
        return "valid", None
    reason = kb._invalid_sqlite_header_reason(path)
    return "invalid", reason or "invalid SQLite header"


def _read_only_integrity_check(path: Path) -> str:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        return f"open_error: {exc}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.OperationalError as exc:
        return f"operational_error: {exc}"
    except sqlite3.DatabaseError as exc:
        return f"database_error: {exc}"
    finally:
        conn.close()
    return str(row[0] if row else "<no row>")


def _sidecar_info(path: Path, suffix: str) -> dict[str, Any]:
    sidecar = path.parent / f"{path.name}{suffix}"
    exists, size, stat_error = _safe_stat(sidecar)
    return {
        "path": str(sidecar),
        "exists": exists,
        "size": size,
        "stat_error": stat_error,
    }


def _corrupt_backup_info(path: Path) -> tuple[int, int | None]:
    latest: int | None = None
    count = 0
    try:
        candidates = list(path.parent.glob(f"{path.name}.corrupt.*.bak"))
    except OSError:
        return 0, None
    for candidate in candidates:
        try:
            stat = candidate.stat()
        except OSError:
            continue
        count += 1
        mtime = int(stat.st_mtime)
        if latest is None or mtime > latest:
            latest = mtime
    return count, latest


def _board_has_health_artifact(path: Path) -> bool:
    if (path / "board.json").exists():
        return True
    db = path / "kanban.db"
    for candidate in (
        db,
        path / "kanban.db-wal",
        path / "kanban.db-shm",
    ):
        if candidate.exists():
            return True
    try:
        return any(path.glob("kanban.db.corrupt.*.bak")) or any(path.glob("kanban.db.backup.*.bak"))
    except OSError:
        return False


def discover_board_slugs() -> list[str]:
    """Return board slugs with on-disk health artifacts, without writes."""
    seen = {kb.DEFAULT_BOARD}
    slugs = [kb.DEFAULT_BOARD]
    root = kb.boards_root()
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower()) if root.is_dir() else []
    except OSError:
        children = []
    for child in children:
        if not child.is_dir() or child.name == "_archived":
            continue
        try:
            slug = kb._normalize_board_slug(child.name)
        except ValueError:
            continue
        if not slug or slug in seen or not _board_has_health_artifact(child):
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def scan_board(slug: str) -> dict[str, Any]:
    normed = kb._normalize_board_slug(slug) or kb.DEFAULT_BOARD
    db_path = kb.kanban_db_path(normed)
    exists, size, stat_error = _safe_stat(db_path)
    header_status, header_reason = _classify_sqlite_header(db_path, exists, size)
    integrity_status: str | None = None
    if exists and size and header_status == "valid":
        integrity_status = _read_only_integrity_check(db_path)
    backup_count, latest_backup_mtime = _corrupt_backup_info(db_path)
    wal = _sidecar_info(db_path, "-wal")
    shm = _sidecar_info(db_path, "-shm")
    return {
        "slug": normed,
        "db_path": str(db_path),
        "exists": exists,
        "size": size,
        "stat_error": stat_error,
        "zero_byte_stub": bool(exists and size == 0),
        "sqlite_header_status": header_status,
        "sqlite_header_reason": header_reason,
        "integrity_status": integrity_status,
        "wal_present": bool(wal["exists"]),
        "wal_path": wal["path"],
        "wal_size": wal["size"],
        "wal_stat_error": wal["stat_error"],
        "shm_present": bool(shm["exists"]),
        "shm_path": shm["path"],
        "shm_size": shm["size"],
        "shm_stat_error": shm["stat_error"],
        "corrupt_backup_count": backup_count,
        "latest_corrupt_backup_mtime": latest_backup_mtime,
    }


def scan_boards() -> list[dict[str, Any]]:
    return [scan_board(slug) for slug in discover_board_slugs()]

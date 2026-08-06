"""Post-preservation Gmail header routing and isolated attachment parsing."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses
from pathlib import Path
from typing import Mapping, Sequence


HEADER_TIERS = ("Delivered-To", "X-Original-To", "To", "Cc")


@dataclass(frozen=True, slots=True)
class AliasResolution:
    project_key: str
    delivered_alias: str
    tier: str
    conflict: bool = False


def resolve_alias(raw: bytes, aliases: Mapping[str, str], *, max_header_bytes: int) -> AliasResolution:
    marker = raw.find(b"\r\n\r\n")
    separator = 4
    if marker < 0:
        marker = raw.find(b"\n\n")
        separator = 2
    if marker < 0 or marker + separator > int(max_header_bytes):
        raise ValueError("Gmail header section is missing or exceeds its bound")
    message = BytesHeaderParser(policy=policy.default).parsebytes(raw[: marker + separator])
    normalized = {str(key).strip().lower(): value for key, value in aliases.items()}
    for tier in HEADER_TIERS:
        matched = []
        for _display, address in getaddresses(message.get_all(tier, [])):
            address = address.strip().lower()
            if address in normalized:
                matched.append((address, normalized[address]))
        if not matched:
            continue
        projects = {project for _alias, project in matched}
        if len(projects) > 1:
            return AliasResolution("unmapped", "", tier, True)
        return AliasResolution(matched[0][1], matched[0][0], tier)
    return AliasResolution("unmapped", "", "", False)


def parse_attachments(
    raw_path: Path,
    settings: Mapping[str, int | float],
) -> tuple[list[dict], str]:
    output_root = Path(tempfile.mkdtemp(prefix="gmail-mime-", dir=str(raw_path.parent)))
    output_root.chmod(0o700)
    request = json.dumps(
        {"source": str(raw_path), "output": str(output_root), "settings": dict(settings)},
        sort_keys=True,
    ).encode()
    worker = Path(__file__).with_name("gmail_mime_worker.py")
    env = {"PATH": os.environ.get("PATH", "")}
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(worker)],
            input=request,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(1.0, float(settings["timeout_seconds"])),
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(output_root, ignore_errors=True)
        return [], "mime_timeout"
    try:
        payload = json.loads(completed.stdout[:128 * 1024])
    except (UnicodeError, json.JSONDecodeError):
        shutil.rmtree(output_root, ignore_errors=True)
        return [], "mime_worker_invalid"
    if completed.returncode != 0 or not payload.get("ok"):
        error = str(payload.get("error_class") or "mime_worker_failed")[:120]
        shutil.rmtree(output_root, ignore_errors=True)
        return [], error
    attachments = payload.get("attachments")
    if not isinstance(attachments, list) or len(attachments) > int(settings["max_attachments"]):
        shutil.rmtree(output_root, ignore_errors=True)
        return [], "mime_worker_invalid"
    return attachments, ""


def cleanup_attachments(attachments: Sequence[Mapping[str, object]]) -> None:
    roots = {Path(str(item["path"])).parent for item in attachments if item.get("path")}
    for root in roots:
        shutil.rmtree(root, ignore_errors=True)


__all__ = [
    "AliasResolution",
    "HEADER_TIERS",
    "cleanup_attachments",
    "parse_attachments",
    "resolve_alias",
]

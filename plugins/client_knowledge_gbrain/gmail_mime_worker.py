"""Resource-bounded MIME worker. Invoked only after raw preservation."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import sys
from email import policy
from email.parser import BytesFeedParser
from pathlib import Path


class LimitExceeded(ValueError):
    pass


def _limits(settings: dict) -> None:
    memory = int(settings["memory_bytes"])
    cpu = int(settings["cpu_seconds"])
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (int(settings["max_total_attachment_bytes"]),) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _header_guard(path: Path, settings: dict) -> None:
    total = 0
    count = 0
    with path.open("rb") as handle:
        while True:
            line = handle.readline(int(settings["max_header_line_bytes"]) + 1)
            if not line:
                break
            if len(line) > int(settings["max_header_line_bytes"]):
                raise LimitExceeded("mime_header_line_limit")
            total += len(line)
            if total > int(settings["max_header_bytes"]):
                raise LimitExceeded("mime_header_bytes_limit")
            if line in {b"\n", b"\r\n"}:
                break
            if not line[:1].isspace():
                count += 1
                if count > int(settings["max_header_count"]):
                    raise LimitExceeded("mime_header_count_limit")


def run(request: dict) -> dict:
    settings = request["settings"]
    _limits(settings)
    source = Path(request["source"])
    output = Path(request["output"])
    output.mkdir(mode=0o700, exist_ok=True)
    _header_guard(source, settings)
    constructed = 0

    def factory(*args, **kwargs):
        nonlocal constructed
        constructed += 1
        if constructed > int(settings["max_mime_parts"]):
            raise LimitExceeded("mime_part_limit")
        from email.message import EmailMessage
        return EmailMessage(*args, **kwargs)

    parser = BytesFeedParser(policy=policy.default.clone(message_factory=factory))
    with source.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            parser.feed(chunk)
    message = parser.close()
    stack = [(message, 0, "1")]
    attachments = []
    total_bytes = 0
    while stack:
        part, depth, part_path = stack.pop()
        if depth > int(settings["max_mime_depth"]):
            raise LimitExceeded("mime_depth_limit")
        if part.is_multipart():
            children = list(part.iter_parts())
            for index, child in reversed(list(enumerate(children, 1))):
                stack.append((child, depth + 1, f"{part_path}.{index}"))
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename() or ""
        if disposition != "attachment" and not filename:
            continue
        if len(attachments) >= int(settings["max_attachments"]):
            raise LimitExceeded("mime_attachment_count_limit")
        try:
            payload = part.get_payload(decode=True)
        except Exception as exc:
            raise LimitExceeded("mime_attachment_decode_invalid") from exc
        if payload is None:
            payload = b""
        if len(payload) > int(settings["max_attachment_bytes"]):
            raise LimitExceeded("mime_attachment_bytes_limit")
        total_bytes += len(payload)
        if total_bytes > int(settings["max_total_attachment_bytes"]):
            raise LimitExceeded("mime_total_attachment_bytes_limit")
        destination = output / f"attachment-{len(attachments):04d}"
        with destination.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        attachments.append(
            {
                "part_path": part_path,
                "filename": str(filename)[:255],
                "mime_type": part.get_content_type().lower(),
                "byte_size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "path": str(destination),
            }
        )
    return {"attachments": attachments, "part_count": constructed}


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read(128 * 1024))
        result = run(request)
        sys.stdout.write(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except BaseException as exc:
        error = str(exc) if isinstance(exc, LimitExceeded) else exc.__class__.__name__
        sys.stdout.write(json.dumps({"ok": False, "error_class": error[:120]}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

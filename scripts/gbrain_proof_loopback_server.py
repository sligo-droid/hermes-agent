#!/usr/bin/env python3
"""Deterministic Anthropic-compatible loopback server for Silo G Lane B."""

from __future__ import annotations

import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    response_text = args.response.read_text(encoding="utf-8")
    records = []

    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesGBrainProof/1"

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            record = {
                "ordinal": len(records) + 1,
                "method": "POST",
                "path": self.path,
                "header_names": sorted(name.lower() for name in self.headers.keys()),
                "body_sha256": hashlib.sha256(_canonical_json(parsed)).hexdigest() if parsed is not None else None,
                "body": parsed,
            }
            records.append(record)
            args.log.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            payload = {
                "id": "msg_hermes_gbrain_proof",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "text", "text": response_text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 321, "output_tokens": 87},
            }
            encoded = _canonical_json(payload)
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            if len(records) >= 1:
                self.server.shutdown()

        def log_message(self, _format, *_args):
            return

    args.log.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

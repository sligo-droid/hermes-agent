"""Dependency-free frontend preview smoke helper for workers."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen


@dataclass
class Probe:
    route: str
    expect_status: int = 200
    expect_text: str = ""


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid URL: {url}")
    return parsed.scheme, parsed.hostname, parsed.port


def resolve_probe_url(base_url: str, route: str) -> str:
    base_origin = _origin(base_url)
    resolved = urljoin(base_url.rstrip("/") + "/", route)
    if _origin(resolved) != base_origin:
        raise ValueError(
            f"probe URL {resolved} does not use exact preview host:port from {base_url}"
        )
    return resolved


def fetch_url(url: str, *, timeout: float) -> tuple[int, str]:
    try:
        with urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return int(response.status), body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body


def wait_for_url(url: str, *, timeout: float = 30.0, interval: float = 0.25) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            fetch_url(url, timeout=min(2.0, interval + 0.5))
            return
        except (OSError, URLError) as exc:
            last_error = str(exc)
            time.sleep(interval)
    raise RuntimeError(f"preview did not respond at exact URL {url}: {last_error}")


def run_smoke(
    *,
    url: str,
    cmd: str,
    routes: list[Probe],
    wait_timeout: float = 30.0,
    request_timeout: float = 5.0,
    cwd: str | None = None,
) -> None:
    if not routes:
        routes = [Probe(route="/")]
    probe_urls = [(probe, resolve_probe_url(url, probe.route)) for probe in routes]
    proc = subprocess.Popen(cmd, cwd=cwd, shell=True, start_new_session=True)
    try:
        wait_for_url(url, timeout=wait_timeout)
        for probe, probe_url in probe_urls:
            status, body = fetch_url(probe_url, timeout=request_timeout)
            if status != probe.expect_status:
                raise RuntimeError(
                    f"{probe_url} returned HTTP {status}, expected {probe.expect_status}"
                )
            if probe.expect_text and probe.expect_text not in body:
                raise RuntimeError(f"{probe_url} did not contain expected text")
    finally:
        _terminate_process_tree(proc)


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (AttributeError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _parse_route(values: list[str]) -> list[Probe]:
    probes: list[Probe] = []
    for raw in values:
        parts = raw.split("::", 2)
        route = parts[0]
        status = int(parts[1]) if len(parts) > 1 and parts[1] else 200
        text = parts[2] if len(parts) > 2 else ""
        probes.append(Probe(route=route, expect_status=status, expect_text=text))
    return probes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Exact preview URL, including host and port.")
    parser.add_argument("--cmd", required=True, help="Preview command to start.")
    parser.add_argument("--route", action="append", default=[], help="Route, or route::status::expected text.")
    parser.add_argument("--wait-timeout", type=float, default=30.0)
    parser.add_argument("--request-timeout", type=float, default=5.0)
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args(argv)
    try:
        run_smoke(
            url=args.url,
            cmd=args.cmd,
            routes=_parse_route(args.route),
            wait_timeout=args.wait_timeout,
            request_timeout=args.request_timeout,
            cwd=args.cwd,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

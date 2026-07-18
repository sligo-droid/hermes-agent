#!/usr/bin/env python3
"""Run dependency-free source-CI security checks before project installs.

This intentionally uses only the Python standard library.  It is invoked by
the untrusted source workflow before ``uv pip install`` or ``npm ci`` so a
dangerous diff cannot first execute through a project dependency install.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable


_INSTALL_HOOKS = {
    "setup.py",
    "setup.cfg",
    "sitecustomize.py",
    "usercustomize.py",
    "__init__.pth",
}
_MCP_CATALOG_PREFIX = "optional-mcps/"
_MCP_CATALOG_FILE = "hermes_cli/mcp_catalog.py"
_B64_DECODE_RE = re.compile(r"base64\.(?:b64decode|decodebytes|urlsafe_b64decode)", re.IGNORECASE)
_EVAL_RE = re.compile(r"\b(?:exec|eval)\s*\(", re.IGNORECASE)
_OBFUSCATED_SUBPROCESS_RE = re.compile(r"subprocess\.(?:Popen|call|run)\s*\(", re.IGNORECASE)
_OBFUSCATION_RE = re.compile(r"base64|\\x[0-9a-f]{2}|\bchr\s*\(", re.IGNORECASE)
_QUOTED_DEP_RE = re.compile(r"[\"']([^\"']+)[\"']")


def _added_lines(diff: str) -> list[str]:
    return [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


def critical_findings(paths: Iterable[str], diff: str) -> list[str]:
    """Return the narrow, high-signal supply-chain findings for a PR diff."""
    normalized_paths = [str(path).strip() for path in paths if str(path).strip()]
    findings: list[str] = []

    pth_files = [path for path in normalized_paths if path.endswith(".pth")]
    if pth_files:
        findings.append(".pth file added or modified: " + ", ".join(pth_files[:10]))

    install_hooks = [path for path in normalized_paths if path in _INSTALL_HOOKS]
    if install_hooks:
        findings.append("install-hook file added or modified: " + ", ".join(install_hooks[:10]))

    for line in _added_lines(diff):
        if _B64_DECODE_RE.search(line) and _EVAL_RE.search(line):
            findings.append("base64 decode passed directly to exec/eval")
            break
    for line in _added_lines(diff):
        if _OBFUSCATED_SUBPROCESS_RE.search(line) and _OBFUSCATION_RE.search(line):
            findings.append("subprocess call with an encoded or obfuscated command")
            break
    return findings


def unbounded_dependency_specs(pyproject_diff: str) -> list[str]:
    """Return added PyPI ranges with a lower but no upper bound.

    Git requirements and exact pins are intentionally excluded.  This keeps
    the old audit's policy while handling dependency strings with extras,
    markers, or commas in one small standard-library parser.
    """
    unbounded: list[str] = []
    for line in _added_lines(pyproject_diff):
        for candidate in _QUOTED_DEP_RE.findall(line):
            spec = candidate.strip()
            if ">=" not in spec or "<" in spec or "git+" in spec or "@ git" in spec:
                continue
            if spec not in unbounded:
                unbounded.append(spec)
    return unbounded


def is_mcp_catalog_path(path: str) -> bool:
    return path.startswith(_MCP_CATALOG_PREFIX) or path == _MCP_CATALOG_FILE


def _git_output(root: Path, args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def _diff_reference(
    root: Path,
    base: str,
    head: str,
    *,
    options: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
) -> tuple[str, bool]:
    """Read a commit range, falling back to HEAD's tree versus the empty tree."""

    suffix = ["--", *paths] if paths else []
    if base and head and not set(base) == {"0"}:
        code, output, _error = _git_output(root, ["diff", *options, f"{base}...{head}", *suffix])
        if code == 0:
            return output, True
    if head:
        # ``git diff --root <commit>`` compares the commit to the working tree
        # and can return an empty scan for a clean checkout. ``diff-tree
        # --root`` is the commit-to-tree operation: for a root commit it uses
        # the empty tree, and for any other commit it scans the committed tree
        # against its parent without consulting mutable workspace state.
        code, output, _error = _git_output(
            root,
            ["diff-tree", "--root", "--no-commit-id", "-r", *options, head, *suffix],
        )
        if code == 0:
            return output, True
    return "", False


def _changed_paths(root: Path, base: str, head: str) -> tuple[list[str], bool]:
    output, available = _diff_reference(root, base, head, options=("--name-only",))
    return [line.strip() for line in output.splitlines() if line.strip()], available


def _scan_diff(root: Path, base: str, head: str) -> tuple[str, bool]:
    return _diff_reference(
        root,
        base,
        head,
        options=("--unified=0",),
        paths=(
            ".",
            ":(exclude)uv.lock",
            ":(exclude)*.lock",
            ":(exclude)package-lock.json",
            ":(exclude)yarn.lock",
        ),
    )


def _pyproject_diff(root: Path, base: str, head: str) -> tuple[str, bool]:
    return _diff_reference(root, base, head, options=("--unified=0",), paths=("pyproject.toml",))


def _pr_labels(root: Path, number: str) -> tuple[list[str], str]:
    result = subprocess.run(
        ["gh", "pr", "view", number, "--json", "labels", "--jq", ".labels[].name"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [], (result.stderr or result.stdout or "could not read PR labels").strip()
    return [line.strip() for line in result.stdout.splitlines() if line.strip()], ""


def run_preflight(root: Path, *, base: str, head: str, pr_number: str = "") -> list[str]:
    """Run all migrated supply-chain checks and return human-readable errors."""
    paths, paths_available = _changed_paths(root, base, head)
    scan_diff, scan_available = _scan_diff(root, base, head)
    pyproject_diff, pyproject_available = _pyproject_diff(root, base, head)
    findings = critical_findings(paths, scan_diff)
    findings.extend(
        f"PyPI dependency without an upper bound: {spec}"
        for spec in unbounded_dependency_specs(pyproject_diff)
    )

    if any(is_mcp_catalog_path(path) for path in paths) and pr_number:
        labels, label_error = _pr_labels(root, pr_number)
        if label_error:
            findings.append(f"could not verify mcp-catalog-reviewed label: {label_error}")
        elif "mcp-catalog-reviewed" not in labels:
            findings.append("MCP catalog changes require the mcp-catalog-reviewed label")

    # The workflow classifier already enables every lane for this case.  A
    # source-CI preflight that cannot read the diff must fail loudly instead
    # of silently treating an uninspectable supply-chain change as clean.
    if not (paths_available and scan_available and pyproject_available):
        findings.append("could not obtain a complete Git diff for source-CI preflight")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="base commit SHA")
    parser.add_argument("--head", default="", help="head commit SHA")
    parser.add_argument("--pr-number", default="", help="PR number; enables MCP label validation")
    parser.add_argument("--root", default=".", help="repository root (for local testing)")
    args = parser.parse_args(argv)

    findings = run_preflight(
        Path(args.root).resolve(),
        base=str(args.base or "").strip(),
        head=str(args.head or "").strip(),
        pr_number=str(args.pr_number or "").strip(),
    )
    if not findings:
        print("Source CI preflight passed.")
        return 0
    for finding in findings:
        print(f"::error::{finding}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

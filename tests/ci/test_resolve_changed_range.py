from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "resolve_changed_range.py"
_SPEC = importlib.util.spec_from_file_location("resolve_changed_range", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Failed to load resolve_changed_range.py")
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

BASE = "a" * 40
HEAD = "b" * 40
MERGE_BASE = "c" * 40
ZERO = "0" * 40


def _result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_pull_request_uses_proven_three_dot_range(tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        if args[:2] == ["cat-file", "-e"]:
            return _result()
        if args[:1] == ["merge-base"]:
            return _result(stdout=MERGE_BASE + "\n")
        if args[:3] == ["diff", "--name-only", "--no-renames"]:
            return _result(stdout="agent/a.py\nweb/a.ts\n")
        raise AssertionError(args)

    result = _MOD.resolve_changed_range(
        tmp_path,
        event_name="pull_request",
        base_sha=BASE,
        head_sha=HEAD,
        run=run,
    )

    assert result.reliable is True
    assert result.paths == ("agent/a.py", "web/a.ts")
    assert ["diff", "--name-only", "--no-renames", f"{BASE}...{HEAD}", "--"] in calls
    assert not any("--deepen" in " ".join(call) for call in calls)


def test_normal_push_requires_ancestry_and_uses_two_dot_range(tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        if args[:2] == ["cat-file", "-e"]:
            return _result()
        if args[:3] == ["merge-base", "--is-ancestor", BASE]:
            return _result()
        if args[:3] == ["diff", "--name-only", "--no-renames"]:
            return _result(stdout="run_agent.py\n")
        raise AssertionError(args)

    result = _MOD.resolve_changed_range(
        tmp_path,
        event_name="push",
        base_sha=BASE,
        head_sha=HEAD,
        run=run,
    )

    assert result.reliable is True
    assert result.paths == ("run_agent.py",)
    assert ["diff", "--name-only", "--no-renames", f"{BASE}..{HEAD}", "--"] in calls


def test_initial_push_and_invalid_shas_fail_open_without_git(tmp_path):
    calls = []

    initial = _MOD.resolve_changed_range(
        tmp_path,
        event_name="push",
        base_sha=ZERO,
        head_sha=HEAD,
        run=lambda args: calls.append(args),
    )
    invalid = _MOD.resolve_changed_range(
        tmp_path,
        event_name="pull_request",
        base_sha="HEAD^",
        head_sha=HEAD,
        run=lambda args: calls.append(args),
    )

    assert initial.reliable is False
    assert initial.paths == ()
    assert initial.reason == "initial_push_has_no_reliable_base"
    assert invalid.reliable is False
    assert invalid.reason == "invalid_full_sha"
    assert calls == []


def test_missing_commit_gets_one_exact_bounded_fetch(tmp_path):
    calls = []
    head_available = False

    def run(args):
        nonlocal head_available
        calls.append(args)
        if args == ["cat-file", "-e", f"{BASE}^{{commit}}"]:
            return _result()
        if args == ["cat-file", "-e", f"{HEAD}^{{commit}}"]:
            return _result() if head_available else _result(returncode=1)
        if args == ["fetch", "--no-tags", "--depth=1", "origin", HEAD]:
            head_available = True
            return _result()
        if args[:3] == ["merge-base", "--is-ancestor", BASE]:
            return _result()
        if args[:3] == ["diff", "--name-only", "--no-renames"]:
            return _result(stdout="agent/a.py\n")
        raise AssertionError(args)

    result = _MOD.resolve_changed_range(
        tmp_path,
        event_name="push",
        base_sha=BASE,
        head_sha=HEAD,
        run=run,
    )

    assert result.reliable is True
    assert calls.count(["fetch", "--no-tags", "--depth=1", "origin", HEAD]) == 1


def test_history_deepening_is_fixed_and_bounded(tmp_path):
    calls = []
    proofs = 0

    def run(args):
        nonlocal proofs
        calls.append(args)
        if args[:2] == ["cat-file", "-e"]:
            return _result()
        if args[:1] == ["merge-base"]:
            proofs += 1
            if proofs < 3:
                return _result(returncode=1)
            return _result(stdout=MERGE_BASE + "\n")
        if args[:2] == ["fetch", "--no-tags"]:
            return _result()
        if args[:3] == ["diff", "--name-only", "--no-renames"]:
            return _result(stdout="docs/a.md\n")
        raise AssertionError(args)

    result = _MOD.resolve_changed_range(
        tmp_path,
        event_name="pull_request",
        base_sha=BASE,
        head_sha=HEAD,
        deepen_steps=(50, 200),
        run=run,
    )

    assert result.reliable is True
    deepen = [call for call in calls if any(part.startswith("--deepen=") for part in call)]
    assert deepen == [
        ["fetch", "--no-tags", "--deepen=50", "origin", BASE, HEAD],
        ["fetch", "--no-tags", "--deepen=200", "origin", BASE, HEAD],
    ]
    assert not any("--unshallow" in call for call in calls)


def test_force_push_or_unproven_ancestry_fails_open_after_bounds(tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        if args[:2] == ["cat-file", "-e"]:
            return _result()
        if args[:3] == ["merge-base", "--is-ancestor", BASE]:
            return _result(returncode=1)
        if args[:2] == ["fetch", "--no-tags"]:
            return _result()
        raise AssertionError(args)

    result = _MOD.resolve_changed_range(
        tmp_path,
        event_name="push",
        base_sha=BASE,
        head_sha=HEAD,
        deepen_steps=(10, 20),
        run=run,
    )

    assert result.reliable is False
    assert result.paths == ()
    assert result.reason == "push_ancestry_unproven"
    assert len([call for call in calls if any(part.startswith("--deepen=") for part in call)]) == 2
    assert not any(call[:1] == ["diff"] for call in calls)


def test_missing_commit_after_exact_fetch_fails_open(tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        if args == ["cat-file", "-e", f"{BASE}^{{commit}}"]:
            return _result()
        if args == ["cat-file", "-e", f"{HEAD}^{{commit}}"]:
            return _result(returncode=1)
        if args == ["fetch", "--no-tags", "--depth=1", "origin", HEAD]:
            return _result(returncode=1, stderr="not found")
        raise AssertionError(args)

    result = _MOD.resolve_changed_range(
        tmp_path,
        event_name="pull_request",
        base_sha=BASE,
        head_sha=HEAD,
        run=run,
    )

    assert result.reliable is False
    assert result.reason == "commit_unavailable"
    assert not any(call[:1] == ["diff"] for call in calls)


@pytest.mark.parametrize("invalid", ["a" * 7, "a" * 41, "a" * 63])
def test_non_exact_sha_lengths_fail_open_without_git(tmp_path, invalid):
    calls = []

    result = _MOD.resolve_changed_range(
        tmp_path,
        event_name="pull_request",
        base_sha=invalid,
        head_sha=HEAD,
        run=lambda args: calls.append(args),
    )

    assert result == _MOD.RangeResolution(False, reason="invalid_full_sha")
    assert calls == []


def test_exact_64_character_shas_are_accepted(tmp_path):
    base = "a" * 64
    head = "b" * 64
    merge_base = "c" * 64

    def run(args):
        if args[:2] == ["cat-file", "-e"]:
            return _result()
        if args[:1] == ["merge-base"]:
            return _result(stdout=merge_base + "\n")
        if args[:3] == ["diff", "--name-only", "--no-renames"]:
            return _result(stdout="agent/a.py\n")
        raise AssertionError(args)

    result = _MOD.resolve_changed_range(
        tmp_path,
        event_name="pull_request",
        base_sha=base,
        head_sha=head,
        run=run,
    )

    assert result.reliable is True
    assert result.paths == ("agent/a.py",)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (subprocess.TimeoutExpired(["git"], 1), "runner_unavailable"),
        (OSError("git unavailable"), "runner_unavailable"),
        (RuntimeError("injected runner failure"), "runner_exception"),
    ],
)
def test_runner_failures_emit_unreliable_empty_range(tmp_path, failure, reason):
    def run(_args):
        raise failure

    result = _MOD.resolve_changed_range(
        tmp_path,
        event_name="pull_request",
        base_sha=BASE,
        head_sha=HEAD,
        run=run,
    )

    assert result.reliable is False
    assert result.paths == ()
    assert result.reason == reason


def test_main_returns_zero_and_empty_stdout_when_runner_raises(monkeypatch, tmp_path, capsys):
    def runner(_root):
        def run(_args):
            raise RuntimeError("injected runner failure")

        return run

    monkeypatch.setattr(_MOD, "_default_runner", runner)
    result = _MOD.main(
        [
            "--event-name",
            "pull_request",
            "--base",
            BASE,
            "--head",
            HEAD,
            "--root",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == "\n"
    assert "fail-open (runner_exception)" in captured.err

from __future__ import annotations

import datetime as dt
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import post_merge_receipts as receipts


TARGET = "a" * 40
OTHER = "b" * 40


def _legacy_canonical_success(canonical_path, _branch, merge_sha):
    root = Path(canonical_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "legacy-pid").write_text(str(os.getpid()), encoding="utf-8")
    return {"state": "synced", "head": merge_sha, "merge_commit": merge_sha}


def _legacy_canonical_result_object(_canonical_path, branch, merge_sha):
    from hermes_cli.canonical_checkout_sync import CanonicalCheckoutSyncResult

    return CanonicalCheckoutSyncResult(
        state="synced",
        error="",
        path="/canonical",
        branch=branch,
        head=merge_sha,
        merge_commit=merge_sha,
        synced_at="2026-07-18T00:00:00Z",
    )


def _legacy_canonical_timeout(canonical_path, _branch, _merge_sha):
    root = Path(canonical_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "legacy-timeout-pid").write_text(str(os.getpid()), encoding="utf-8")
    heartbeat = root / "legacy-timeout-heartbeat"
    counter = 0
    while True:
        counter += 1
        heartbeat.write_text(str(counter), encoding="utf-8")
        time.sleep(0.005)


def _legacy_canonical_raises(_canonical_path, _branch, _merge_sha):
    raise RuntimeError("token=must-not-escape https://protected.invalid/canonical")


def _assert_process_dead(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"isolated canonical synchronizer {pid} remained alive")


def _state(tmp_path, *, mode="enforce", requirements=None):
    return {
        "mode": mode,
        "workspace": {
            "path": str(tmp_path / "workspace"),
            "canonical_path": str(tmp_path / "canonical"),
            "repository": "owner/repo",
            "base_branch": "main",
        },
        "policy": {"post_merge_requirements": requirements or {}},
        "post_merge": receipts.initialize_post_merge_receipts(
            {
                "workspace": {"canonical_path": str(tmp_path / "canonical")},
                "policy": {"post_merge_requirements": requirements or {}},
            },
            target_sha=TARGET,
        ),
    }


def _completed(args, *, returncode=0, stdout="", stderr=""):
    return SimpleNamespace(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_initialize_sets_every_receipt_before_collection(tmp_path):
    initialized = receipts.initialize_post_merge_receipts(
        _state(tmp_path, requirements={"ci": True}),
        target_sha=TARGET,
    )

    assert initialized["target_sha"] == TARGET
    assert set(initialized) == {
        "target_sha",
        "canonical_sync",
        "ci",
        "deployment",
        "production_qa",
        "restart",
    }
    assert initialized["canonical_sync"]["status"] == "pending"
    assert initialized["ci"]["status"] == "pending"
    assert initialized["deployment"]["status"] == "not_configured"


def test_exact_push_ci_uses_newest_rerun_and_basic_job(tmp_path):
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "run", "list"]:
            return _completed(
                args,
                stdout=json.dumps(
                    [
                        {
                            "databaseId": 10,
                            "headSha": TARGET,
                            "event": "pull_request",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "workflowName": "Basic Tests",
                            "updatedAt": "2026-07-18T00:05:00Z",
                        },
                        {
                            "databaseId": 11,
                            "headSha": OTHER,
                            "event": "push",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "workflowName": "Basic Tests",
                            "updatedAt": "2026-07-18T00:06:00Z",
                        },
                        {
                            "databaseId": 12,
                            "headSha": TARGET,
                            "event": "push",
                            "status": "COMPLETED",
                            "conclusion": "FAILURE",
                            "workflowName": "Basic Tests",
                            "updatedAt": "2026-07-18T00:01:00Z",
                        },
                        {
                            "databaseId": 13,
                            "headSha": TARGET,
                            "event": "push",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "workflowName": "Basic Tests",
                            "updatedAt": "2026-07-18T00:03:00Z",
                        },
                    ]
                ),
            )
        if args[:3] == ["gh", "run", "view"]:
            assert args[3] == "13"
            return _completed(
                args,
                stdout=json.dumps(
                    {
                        "jobs": [
                            {"name": "other", "status": "COMPLETED", "conclusion": "SUCCESS"},
                            {"name": "basic", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        ]
                    }
                ),
            )
        raise AssertionError(args)

    state = _state(tmp_path, requirements={"ci": True})
    gathered = receipts.collect_post_merge_receipts(
        state,
        run=run,
        sync_canonical=lambda *_args, **_kwargs: {"state": "synced"},
        now=100,
    )

    assert gathered["ci"] == {"status": "passed", "checked_at": 100, "observed_sha": TARGET}
    assert any(call[:3] == ["gh", "run", "view"] for call in calls)


def test_newest_exact_push_failure_cannot_be_hidden_by_older_success(tmp_path):
    def run(args, **_kwargs):
        if args[:3] == ["gh", "run", "list"]:
            return _completed(
                args,
                stdout=json.dumps(
                    [
                        {
                            "databaseId": 20,
                            "headSha": TARGET,
                            "event": "push",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "workflowName": "Basic Tests",
                            "updatedAt": "2026-07-18T00:01:00Z",
                        },
                        {
                            "databaseId": 21,
                            "headSha": TARGET,
                            "event": "push",
                            "status": "COMPLETED",
                            "conclusion": "FAILURE",
                            "workflowName": "Basic Tests",
                            "updatedAt": "2026-07-18T00:02:00Z",
                        },
                    ]
                ),
            )
        raise AssertionError(args)

    state = _state(tmp_path, requirements={"ci": True})
    state["workspace"]["canonical_path"] = ""
    gathered = receipts.collect_post_merge_receipts(state, run=run, now=100)

    assert gathered["ci"]["status"] == "failed"
    assert gathered["ci"]["observed_sha"] == TARGET
    assert gathered["ci"]["diagnostic_code"] == "post_merge_ci_failed"


def test_registered_adapters_require_exact_observed_sha(tmp_path):
    receipts.register_deployment_adapter(
        "test-deploy-exact",
        lambda **_kwargs: {"status": "passed", "observed_sha": TARGET},
    )
    receipts.register_production_qa_adapter(
        "test-production-wrong",
        lambda **_kwargs: {"status": "passed", "observed_sha": OTHER},
    )
    state = _state(
        tmp_path,
        requirements={"deployment": True, "production_qa": True},
    )
    gathered = receipts.collect_post_merge_receipts(
        state,
        config={
            "repositories": {
                "owner/repo": {
                    "deployment_adapter": "test-deploy-exact",
                    "production_qa_adapter": "test-production-wrong",
                }
            }
        },
        sync_canonical=lambda *_args, **_kwargs: {"state": "synced"},
        now=100,
    )

    assert gathered["deployment"]["status"] == "passed"
    assert gathered["deployment"]["observed_sha"] == TARGET
    assert gathered["production_qa"]["status"] == "failed"
    assert gathered["production_qa"]["diagnostic_code"] == "observed_sha_mismatch"
    assert "observed_sha" not in gathered["production_qa"]


def test_missing_required_adapter_blocks_only_enforce_mode(tmp_path):
    enforce = _state(tmp_path, requirements={"deployment": True})
    shadow = _state(tmp_path, mode="shadow", requirements={"deployment": True})

    enforced = receipts.collect_post_merge_receipts(
        enforce,
        sync_canonical=lambda *_args, **_kwargs: {"state": "synced"},
        now=100,
    )
    observed = receipts.collect_post_merge_receipts(
        shadow,
        sync_canonical=lambda *_args, **_kwargs: {"state": "synced"},
        now=100,
    )

    assert enforced["deployment"]["status"] == "failed"
    assert enforced["deployment"]["diagnostic_code"] == "required_adapter_missing"
    assert observed["deployment"]["status"] == "not_configured"


def test_independent_collectors_run_concurrently_and_return_one_update(tmp_path):
    barrier = multiprocessing.get_context("fork").Barrier(2)

    def adapter(**_kwargs):
        barrier.wait(timeout=2)
        return {"status": "passed", "observed_sha": TARGET}

    receipts.register_deployment_adapter("test-concurrent-deploy", adapter)
    receipts.register_production_qa_adapter("test-concurrent-qa", adapter)
    state = _state(
        tmp_path,
        requirements={"deployment": True, "production_qa": True},
    )

    gathered = receipts.collect_post_merge_receipts(
        state,
        config={
            "repositories": {
                "owner/repo": {
                    "deployment_adapter": "test-concurrent-deploy",
                    "production_qa_adapter": "test-concurrent-qa",
                }
            }
        },
        sync_canonical=lambda *_args, **_kwargs: {"state": "synced"},
        now=100,
        max_workers=2,
    )

    assert gathered["deployment"]["status"] == "passed"
    assert gathered["production_qa"]["status"] == "passed"


def test_adapter_receives_exact_target_and_bounded_timeout(tmp_path):
    observed = multiprocessing.get_context("fork").Queue()

    def adapter(**kwargs):
        control = kwargs.pop("control")
        prior_receipt = kwargs.pop("prior_receipt")
        observed.put((kwargs, isinstance(control, receipts.PostMergeControl), control.cancelled(), prior_receipt))
        return {"status": "passed", "observed_sha": kwargs["target_sha"]}

    receipts.register_restart_adapter("test-restart-target", adapter)
    state = _state(tmp_path, requirements={"restart": True})
    gathered = receipts.collect_post_merge_receipts(
        state,
        config={
            "collector_timeout_s": 2,
            "adapter_timeout_s": 1.25,
            "repositories": {"owner/repo": {"restart_adapter": "test-restart-target"}},
        },
        sync_canonical=lambda *_args, **_kwargs: {
            "state": "synced",
            "head": TARGET,
            "merge_commit": TARGET,
        },
        now=100,
    )

    assert gathered["restart"] == {"status": "passed", "checked_at": 100, "observed_sha": TARGET}
    adapter_kwargs, is_control, cancelled, prior_receipt = observed.get(timeout=1)
    assert is_control is True
    assert cancelled is False
    assert prior_receipt == {"status": "pending"}
    assert adapter_kwargs == {
        "target_sha": TARGET,
        "repository": "owner/repo",
        "workspace_path": str(tmp_path / "workspace"),
        "canonical_path": str(tmp_path / "canonical"),
        "timeout_s": 1.25,
    }


def test_shadow_collection_observes_ci_without_mutating_adapters(tmp_path):
    called = []

    def forbidden_adapter(**_kwargs):
        called.append("adapter")
        raise AssertionError("shadow invoked mutating adapter")

    receipts.register_deployment_adapter("test-shadow-deploy", forbidden_adapter)
    receipts.register_restart_adapter("test-shadow-restart", forbidden_adapter)

    def run(args, **_kwargs):
        if args[:3] == ["gh", "run", "list"]:
            return _completed(
                args,
                stdout=json.dumps(
                    [
                        {
                            "databaseId": 41,
                            "headSha": TARGET,
                            "event": "push",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "workflowName": "Basic Tests",
                            "updatedAt": "2026-07-18T00:03:00Z",
                        }
                    ]
                ),
            )
        if args[:3] == ["gh", "run", "view"]:
            return _completed(
                args,
                stdout=json.dumps(
                    {
                        "jobs": [
                            {
                                "name": "basic",
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                            }
                        ]
                    }
                ),
            )
        raise AssertionError(args)

    state = _state(
        tmp_path,
        mode="shadow",
        requirements={
            "canonical_sync": True,
            "ci": True,
            "deployment": True,
            "restart": True,
        },
    )
    gathered = receipts.collect_post_merge_receipts(
        state,
        config={
            "repositories": {
                "owner/repo": {
                    "deployment_adapter": "test-shadow-deploy",
                    "restart_adapter": "test-shadow-restart",
                }
            }
        },
        run=run,
        sync_canonical=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("shadow synchronized canonical checkout")
        ),
        now=100,
        read_only=True,
    )

    assert called == []
    assert gathered["ci"]["status"] == "passed"
    for name in ("canonical_sync", "deployment", "restart"):
        assert gathered[name]["status"] == "not_configured"
        assert gathered[name]["diagnostic_code"] == "shadow_not_executed"


def test_plugin_discovery_can_register_repository_adapter(monkeypatch, tmp_path):
    from hermes_cli import plugins

    calls = []
    monkeypatch.setattr(receipts, "_ADAPTER_DISCOVERY_ATTEMPTED", False)

    def discover_plugins():
        calls.append("discover")
        receipts.register_deployment_adapter(
            "plugin-deploy",
            lambda **kwargs: {
                "status": "passed",
                "observed_sha": kwargs["target_sha"],
            },
        )

    monkeypatch.setattr(plugins, "discover_plugins", discover_plugins)
    state = _state(tmp_path, requirements={"deployment": True})
    state["workspace"]["canonical_path"] = ""
    gathered = receipts.collect_post_merge_receipts(
        state,
        config={
            "repositories": {
                "owner/repo": {"deployment_adapter": "plugin-deploy"}
            }
        },
        now=100,
    )

    assert calls == ["discover"]
    assert gathered["deployment"] == {
        "status": "passed",
        "checked_at": 100,
        "observed_sha": TARGET,
    }


def test_concurrent_initial_collectors_wait_for_adapter_discovery(monkeypatch, tmp_path):
    from hermes_cli import plugins

    monkeypatch.setattr(receipts, "_ADAPTER_DISCOVERY_ATTEMPTED", False)
    discovery_started = threading.Event()
    release_discovery = threading.Event()
    discovery_calls = []

    def discover_plugins():
        discovery_calls.append("discover")
        discovery_started.set()
        assert release_discovery.wait(timeout=2)
        receipts.register_deployment_adapter(
            "plugin-concurrent-deploy",
            lambda **kwargs: {
                "status": "passed",
                "observed_sha": kwargs["target_sha"],
            },
        )

    monkeypatch.setattr(plugins, "discover_plugins", discover_plugins)
    state = _state(tmp_path, requirements={"deployment": True})
    state["workspace"]["canonical_path"] = ""
    results = []

    def collect():
        results.append(
            receipts.collect_post_merge_receipts(
                state,
                config={
                    "repositories": {
                        "owner/repo": {
                            "deployment_adapter": "plugin-concurrent-deploy",
                        }
                    }
                },
                now=100,
            )["deployment"]
        )

    threads = [threading.Thread(target=collect) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert discovery_started.wait(timeout=1)
    time.sleep(0.05)
    assert results == []
    release_discovery.set()
    for thread in threads:
        thread.join(timeout=2)
        assert thread.is_alive() is False

    assert discovery_calls == ["discover"]
    assert results == [
        {"status": "passed", "checked_at": 100, "observed_sha": TARGET},
        {"status": "passed", "checked_at": 100, "observed_sha": TARGET},
    ]


def test_collectors_emit_concurrency_safe_trusted_spans(tmp_path):
    from agent.runtime_spans import RuntimeSpanRecorder

    recorder = RuntimeSpanRecorder(work_id="closeout-1")
    receipts.register_deployment_adapter(
        "test-span-deploy",
        lambda **kwargs: {"status": "passed", "observed_sha": kwargs["target_sha"]},
    )
    state = _state(tmp_path, requirements={"deployment": True})
    state["workspace"]["canonical_path"] = ""
    gathered = receipts.collect_post_merge_receipts(
        state,
        config={
            "repositories": {
                "owner/repo": {"deployment_adapter": "test-span-deploy"}
            }
        },
        now=100,
        span_recorder=recorder,
        span_parent_id="span-closeout-parent",
        span_attempt_id="revision-2",
    )

    assert gathered["deployment"]["status"] == "passed"
    spans = recorder.export()
    assert len(spans) == 1
    assert spans[0]["phase"] == "deployment"
    assert spans[0]["parent_id"].startswith("ref_")
    assert spans[0]["attempt_id"].startswith("att_")
    assert spans[0]["concurrency_id"].startswith("con_")
    assert "span-closeout-parent" not in repr(spans[0])
    assert "revision-2" not in repr(spans[0])
    assert f"post-merge-{TARGET[:12]}" not in repr(spans[0])
    assert spans[0]["metadata"]["collector"].startswith("meta_")
    assert spans[0]["metadata"]["repository"].startswith("meta_")
    assert "owner/repo" not in repr(spans[0])


def test_gateway_restart_adapter_requests_restart_then_observes_exact_target(tmp_path):
    root = tmp_path / "canonical"
    root.mkdir()
    signals = []

    def run(args, **_kwargs):
        if args == ["git", "rev-parse", "HEAD"]:
            return _completed(args, stdout=TARGET + "\n")
        if args[:3] == ["git", "status", "--porcelain"]:
            return _completed(args, stdout="")
        raise AssertionError(args)

    now = dt.datetime(2026, 7, 18, 0, 0, tzinfo=dt.timezone.utc)
    runtime = {
        "pid": 1234,
        "start_time": 777,
        "kind": "hermes-gateway",
        "argv": ["python", "gateway/run.py"],
        "gateway_state": "running",
        "restart_requested": False,
        "source_commit": OTHER,
        "source_identity_kind": "git",
        "source_root": str(root),
        "source_dirty": False,
        "platforms": {},
        "updated_at": now.isoformat(),
    }
    first = receipts.gateway_restart_adapter(
        target_sha=TARGET,
        repository="owner/repo",
        workspace_path="",
        canonical_path=str(root),
        timeout_s=2,
        run=run,
        read_status=lambda: runtime,
        signal_process=lambda pid, sig: signals.append((pid, sig)),
        get_running_pid=lambda: 1234,
        get_process_start_time=lambda _pid: 777,
        now_utc=lambda: now,
    )
    assert first == {
        "status": "pending",
        "diagnostic_code": "gateway_restart_requested",
        "baseline_pid": 1234,
        "baseline_start_time": 777,
    }
    assert signals and signals[0][0] == 1234

    # The original process reporting the target is not replacement proof.
    runtime.update(source_commit=TARGET)
    unchanged = receipts.gateway_restart_adapter(
        target_sha=TARGET,
        repository="owner/repo",
        workspace_path="",
        canonical_path=str(root),
        timeout_s=2,
        run=run,
        read_status=lambda: runtime,
        signal_process=lambda pid, sig: signals.append((pid, sig)),
        get_running_pid=lambda: 1234,
        get_process_start_time=lambda _pid: 777,
        now_utc=lambda: now,
        prior_receipt=first,
    )
    assert unchanged == {
        "status": "pending",
        "diagnostic_code": "gateway_restart_replacement_not_observed",
        "baseline_pid": 1234,
        "baseline_start_time": 777,
    }

    runtime.update(pid=5678, start_time=888)
    second = receipts.gateway_restart_adapter(
        target_sha=TARGET,
        repository="owner/repo",
        workspace_path="",
        canonical_path=str(root),
        timeout_s=2,
        run=run,
        read_status=lambda: runtime,
        signal_process=lambda pid, sig: signals.append((pid, sig)),
        get_running_pid=lambda: 5678,
        get_process_start_time=lambda _pid: 888,
        now_utc=lambda: now,
        prior_receipt=first,
    )
    assert second == {
        "status": "passed",
        "observed_sha": TARGET,
        "baseline_pid": 1234,
        "baseline_start_time": 777,
    }
    assert len(signals) == 1


def test_gateway_restart_adapter_rejects_stale_runtime_without_signalling(tmp_path):
    root = tmp_path / "canonical"
    root.mkdir()
    now = dt.datetime(2026, 7, 18, 0, 30, tzinfo=dt.timezone.utc)
    runtime = {
        "pid": 1234,
        "start_time": 777,
        "kind": "hermes-gateway",
        "argv": ["python", "gateway/run.py"],
        "gateway_state": "running",
        "restart_requested": False,
        "source_commit": OTHER,
        "source_identity_kind": "git",
        "source_root": str(root),
        "source_dirty": False,
        "platforms": {},
        "updated_at": (now - dt.timedelta(hours=1)).isoformat(),
    }
    signals = []

    def run(args, **_kwargs):
        if args == ["git", "rev-parse", "HEAD"]:
            return _completed(args, stdout=TARGET + "\n")
        if args[:3] == ["git", "status", "--porcelain"]:
            return _completed(args, stdout="")
        raise AssertionError(args)

    result = receipts.gateway_restart_adapter(
        target_sha=TARGET,
        repository="owner/repo",
        workspace_path="",
        canonical_path=str(root),
        timeout_s=2,
        run=run,
        read_status=lambda: runtime,
        signal_process=lambda pid, sig: signals.append((pid, sig)),
        get_running_pid=lambda: 1234,
        get_process_start_time=lambda _pid: 777,
        now_utc=lambda: now,
        runtime_max_age_s=60,
    )

    assert result == {"status": "blocked", "diagnostic_code": "restart_runtime_stale"}
    assert signals == []


def test_gateway_restart_adapter_rejects_reused_pid_without_signalling(tmp_path):
    root = tmp_path / "canonical"
    root.mkdir()
    now = dt.datetime(2026, 7, 18, 0, 0, tzinfo=dt.timezone.utc)
    runtime = {
        "pid": 1234,
        "start_time": 111,
        "kind": "hermes-gateway",
        "argv": ["python", "gateway/run.py"],
        "gateway_state": "running",
        "restart_requested": False,
        "source_commit": OTHER,
        "source_identity_kind": "git",
        "source_root": str(root),
        "source_dirty": False,
        "platforms": {},
        "updated_at": now.isoformat(),
    }
    signals = []

    def run(args, **_kwargs):
        if args == ["git", "rev-parse", "HEAD"]:
            return _completed(args, stdout=TARGET + "\n")
        if args[:3] == ["git", "status", "--porcelain"]:
            return _completed(args, stdout="")
        raise AssertionError(args)

    result = receipts.gateway_restart_adapter(
        target_sha=TARGET,
        repository="owner/repo",
        workspace_path="",
        canonical_path=str(root),
        timeout_s=2,
        run=run,
        read_status=lambda: runtime,
        signal_process=lambda pid, sig: signals.append((pid, sig)),
        get_running_pid=lambda: 1234,
        get_process_start_time=lambda _pid: 222,
        now_utc=lambda: now,
    )

    assert result == {
        "status": "blocked",
        "diagnostic_code": "restart_runtime_start_time_mismatch",
    }
    assert signals == []


def test_gateway_restart_adapter_fails_closed_on_wrong_or_dirty_source(tmp_path):
    root = tmp_path / "canonical"
    root.mkdir()

    def wrong_head(args, **_kwargs):
        if args == ["git", "rev-parse", "HEAD"]:
            return _completed(args, stdout=OTHER + "\n")
        return _completed(args, stdout="")

    result = receipts.gateway_restart_adapter(
        target_sha=TARGET,
        repository="owner/repo",
        workspace_path="",
        canonical_path=str(root),
        timeout_s=2,
        run=wrong_head,
        read_status=lambda: {},
        signal_process=lambda *_args: (_ for _ in ()).throw(AssertionError("signalled")),
    )
    assert result == {
        "status": "blocked",
        "diagnostic_code": "restart_source_sha_mismatch",
    }


def test_overdue_adapter_stops_before_timeout_receipt_can_be_persisted(tmp_path):
    started = tmp_path / "adapter-started"
    heartbeat = tmp_path / "adapter-heartbeat"
    child_pid = tmp_path / "adapter-pid"

    def adapter(**_kwargs):
        child_pid.write_text(str(os.getpid()), encoding="utf-8")
        started.write_text("started", encoding="utf-8")
        counter = 0
        while True:
            counter += 1
            heartbeat.write_text(str(counter), encoding="utf-8")
            time.sleep(0.005)

    receipts.register_deployment_adapter("test-timeout-deploy", adapter)
    state = _state(tmp_path, requirements={"deployment": True})
    began = time.monotonic()
    gathered = receipts.collect_post_merge_receipts(
        state,
        config={
            "collector_timeout_s": 0.1,
            "adapter_timeout_s": 0.1,
            "repositories": {"owner/repo": {"deployment_adapter": "test-timeout-deploy"}},
        },
        sync_canonical=lambda *_args, **_kwargs: {"state": "synced"},
        now=100,
        max_workers=2,
    )

    assert started.exists()
    assert child_pid.exists()
    heartbeat_after_return = heartbeat.read_text(encoding="utf-8")
    time.sleep(0.05)
    assert heartbeat.read_text(encoding="utf-8") == heartbeat_after_return
    try:
        os.kill(int(child_pid.read_text(encoding="utf-8")), 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("timed-out adapter process remained alive")
    assert time.monotonic() - began < 1
    assert gathered["deployment"] == {
        "status": "blocked",
        "checked_at": 100,
        "diagnostic_code": "collector_timeout",
    }


def test_plugin_discovery_failure_is_retryable(monkeypatch, tmp_path):
    from hermes_cli import plugins

    monkeypatch.setattr(receipts, "_ADAPTER_DISCOVERY_ATTEMPTED", False)
    calls = []

    def discover_plugins():
        calls.append("discover")
        if len(calls) == 1:
            raise RuntimeError("transient discovery failure")
        receipts.register_deployment_adapter(
            "plugin-retry-deploy",
            lambda **kwargs: {"status": "passed", "observed_sha": kwargs["target_sha"]},
        )

    monkeypatch.setattr(plugins, "discover_plugins", discover_plugins)
    state = _state(tmp_path, requirements={"deployment": True})
    state["workspace"]["canonical_path"] = ""
    config = {
        "repositories": {
            "owner/repo": {"deployment_adapter": "plugin-retry-deploy"}
        }
    }

    first = receipts.collect_post_merge_receipts(state, config=config, now=100)
    second = receipts.collect_post_merge_receipts(state, config=config, now=101)

    assert first["deployment"]["diagnostic_code"] == "required_adapter_missing"
    assert second["deployment"] == {
        "status": "passed",
        "checked_at": 101,
        "observed_sha": TARGET,
    }
    assert calls == ["discover", "discover"]


def test_cooperative_canonical_sync_is_classified_once_and_succeeds(
    monkeypatch,
    tmp_path,
):
    signature_calls = []
    observed = {}
    original_signature = receipts.inspect.signature

    def sync_canonical(_canonical_path, _branch, merge_sha, *, control):
        observed["pid"] = os.getpid()
        observed["control"] = control
        return {
            "state": "synced",
            "head": merge_sha,
            "merge_commit": merge_sha,
        }

    def counted_signature(callback):
        if callback is sync_canonical:
            signature_calls.append(callback)
        return original_signature(callback)

    monkeypatch.setattr(receipts.inspect, "signature", counted_signature)
    gathered = receipts.collect_post_merge_receipts(
        _state(tmp_path, requirements={"canonical_sync": True}),
        sync_canonical=sync_canonical,
        now=100,
    )

    assert signature_calls == [sync_canonical]
    assert observed["pid"] == os.getpid()
    assert isinstance(observed["control"], receipts.PostMergeControl)
    assert gathered["canonical_sync"] == {
        "status": "passed",
        "checked_at": 100,
        "observed_sha": TARGET,
    }


def test_legacy_canonical_sync_succeeds_in_reaped_isolated_process(tmp_path):
    canonical = tmp_path / "canonical"
    gathered = receipts.collect_post_merge_receipts(
        _state(tmp_path, requirements={"canonical_sync": True}),
        config={"collector_timeout_s": 3},
        sync_canonical=_legacy_canonical_success,
        now=100,
    )

    child_pid = int((canonical / "legacy-pid").read_text(encoding="utf-8"))
    assert child_pid != os.getpid()
    _assert_process_dead(child_pid)
    assert gathered["canonical_sync"] == {
        "status": "passed",
        "checked_at": 100,
        "observed_sha": TARGET,
    }


def test_legacy_canonical_result_object_normalization_remains_compatible(tmp_path):
    gathered = receipts.collect_post_merge_receipts(
        _state(tmp_path, requirements={"canonical_sync": True}),
        config={"collector_timeout_s": 3},
        sync_canonical=_legacy_canonical_result_object,
        now=100,
    )

    assert gathered["canonical_sync"] == {
        "status": "passed",
        "checked_at": 100,
        "observed_sha": TARGET,
    }


def test_legacy_canonical_timeout_reaps_child_before_return(tmp_path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    began = time.monotonic()
    gathered = receipts.collect_post_merge_receipts(
        _state(tmp_path, requirements={"canonical_sync": True}),
        config={"collector_timeout_s": 1.5},
        sync_canonical=_legacy_canonical_timeout,
        now=100,
    )

    pid_path = canonical / "legacy-timeout-pid"
    heartbeat = canonical / "legacy-timeout-heartbeat"
    assert pid_path.exists()
    assert heartbeat.exists()
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    heartbeat_after_return = heartbeat.read_text(encoding="utf-8")
    _assert_process_dead(child_pid)
    time.sleep(0.05)
    assert heartbeat.read_text(encoding="utf-8") == heartbeat_after_return
    assert time.monotonic() - began < 3
    assert gathered["canonical_sync"] == {
        "status": "blocked",
        "checked_at": 100,
        "diagnostic_code": "collector_timeout",
    }


def test_legacy_canonical_cancellation_reaps_child_before_return(tmp_path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    started = canonical / "legacy-timeout-pid"
    gathered = receipts.collect_post_merge_receipts(
        _state(tmp_path, requirements={"canonical_sync": True}),
        config={"collector_timeout_s": 3},
        sync_canonical=_legacy_canonical_timeout,
        now=100,
        mutation_allowed=lambda: not started.exists(),
    )

    child_pid = int(started.read_text(encoding="utf-8"))
    heartbeat = canonical / "legacy-timeout-heartbeat"
    heartbeat_after_return = heartbeat.read_text(encoding="utf-8")
    _assert_process_dead(child_pid)
    time.sleep(0.05)
    assert heartbeat.read_text(encoding="utf-8") == heartbeat_after_return
    assert gathered["canonical_sync"]["status"] == "blocked"


def test_legacy_canonical_callback_exception_is_bounded_and_redacted(tmp_path):
    gathered = receipts.collect_post_merge_receipts(
        _state(tmp_path, requirements={"canonical_sync": True}),
        config={"collector_timeout_s": 3},
        sync_canonical=_legacy_canonical_raises,
        now=100,
    )

    assert gathered["canonical_sync"] == {
        "status": "failed",
        "checked_at": 100,
        "diagnostic_code": "canonical_sync_failed",
    }
    assert "must-not-escape" not in repr(gathered["canonical_sync"])
    assert "protected.invalid" not in repr(gathered["canonical_sync"])


def test_dynamic_legacy_canonical_callback_fails_closed_without_invocation(tmp_path):
    called = []

    def dynamic_sync(_canonical_path, _branch, merge_sha):
        called.append(merge_sha)
        return {
            "state": "synced",
            "head": merge_sha,
            "merge_commit": merge_sha,
        }

    gathered = receipts.collect_post_merge_receipts(
        _state(tmp_path, requirements={"canonical_sync": True}),
        sync_canonical=dynamic_sync,
        now=100,
    )

    assert called == []
    assert gathered["canonical_sync"] == {
        "status": "blocked",
        "checked_at": 100,
        "diagnostic_code": "canonical_sync_callback_not_picklable",
    }


def test_legacy_canonical_fails_closed_without_safe_process_start(
    monkeypatch,
    tmp_path,
):
    original_get_context = receipts.multiprocessing.get_context

    def get_context(method=None):
        if method in receipts._SAFE_PROCESS_START_METHODS:
            raise ValueError("safe process start unavailable")
        return original_get_context(method)

    monkeypatch.setattr(receipts.multiprocessing, "get_context", get_context)
    gathered = receipts.collect_post_merge_receipts(
        _state(tmp_path, requirements={"canonical_sync": True}),
        sync_canonical=_legacy_canonical_success,
        now=100,
    )

    assert not (tmp_path / "canonical" / "legacy-pid").exists()
    assert gathered["canonical_sync"] == {
        "status": "blocked",
        "checked_at": 100,
        "diagnostic_code": "canonical_sync_isolation_unavailable",
    }


def test_canonical_receipt_requires_exact_observed_head_and_merge_target(tmp_path):
    state = _state(tmp_path, requirements={"canonical_sync": True})

    missing = receipts.collect_post_merge_receipts(
        state,
        sync_canonical=lambda *_args, **_kwargs: {
            "state": "synced",
            "merge_commit": TARGET,
        },
        now=100,
    )
    mismatched = receipts.collect_post_merge_receipts(
        state,
        sync_canonical=lambda *_args, **_kwargs: {
            "state": "synced",
            "head": OTHER,
            "merge_commit": TARGET,
        },
        now=101,
    )
    inconsistent = receipts.collect_post_merge_receipts(
        state,
        sync_canonical=lambda *_args, **_kwargs: {
            "state": "synced",
            "head": TARGET,
            "merge_commit": OTHER,
        },
        now=102,
    )

    assert missing["canonical_sync"] == {
        "status": "failed",
        "checked_at": 100,
        "diagnostic_code": "canonical_head_missing",
    }
    assert mismatched["canonical_sync"]["diagnostic_code"] == "canonical_head_mismatch"
    assert "observed_sha" not in mismatched["canonical_sync"]
    assert inconsistent["canonical_sync"]["diagnostic_code"] == "canonical_merge_target_mismatch"


def test_restart_waits_for_canonical_exact_target(tmp_path):
    canonical_observation = tmp_path / "canonical-head"
    canonical_observation.write_text(OTHER, encoding="utf-8")

    def sync_canonical(*_args, **_kwargs):
        time.sleep(0.05)
        canonical_observation.write_text(TARGET, encoding="utf-8")
        return {"state": "synced", "head": TARGET, "merge_commit": TARGET}

    def restart_adapter(**_kwargs):
        observed = canonical_observation.read_text(encoding="utf-8")
        return {"status": "passed", "observed_sha": observed}

    receipts.register_restart_adapter("test-canonical-ordered-restart", restart_adapter)
    state = _state(
        tmp_path,
        requirements={"canonical_sync": True, "restart": True},
    )
    gathered = receipts.collect_post_merge_receipts(
        state,
        config={
            "repositories": {
                "owner/repo": {"restart_adapter": "test-canonical-ordered-restart"}
            }
        },
        sync_canonical=sync_canonical,
        now=100,
        max_workers=2,
    )

    assert gathered["canonical_sync"]["status"] == "passed"
    assert gathered["restart"]["status"] == "passed"
    assert gathered["restart"]["observed_sha"] == TARGET


def test_post_merge_target_requires_exact_full_sha(tmp_path):
    state = _state(tmp_path)
    for invalid in ("a" * 7, "a" * 41, "a" * 63):
        try:
            receipts.initialize_post_merge_receipts(state, target_sha=invalid)
        except ValueError as exc:
            assert "exact full SHA" in str(exc)
        else:
            raise AssertionError(f"accepted invalid SHA length {len(invalid)}")

    initialized = receipts.initialize_post_merge_receipts(state, target_sha="c" * 64)
    assert initialized["target_sha"] == "c" * 64

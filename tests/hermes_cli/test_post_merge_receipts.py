from __future__ import annotations

import datetime as dt
import json
import threading
import time
from types import SimpleNamespace

from hermes_cli import post_merge_receipts as receipts


TARGET = "a" * 40
OTHER = "b" * 40


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
        sync_canonical=lambda *_args: {"state": "synced"},
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
        sync_canonical=lambda *_args: {"state": "synced"},
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
        sync_canonical=lambda *_args: {"state": "synced"},
        now=100,
    )
    observed = receipts.collect_post_merge_receipts(
        shadow,
        sync_canonical=lambda *_args: {"state": "synced"},
        now=100,
    )

    assert enforced["deployment"]["status"] == "failed"
    assert enforced["deployment"]["diagnostic_code"] == "required_adapter_missing"
    assert observed["deployment"]["status"] == "not_configured"


def test_independent_collectors_run_concurrently_and_return_one_update(tmp_path):
    barrier = threading.Barrier(2)

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
        sync_canonical=lambda *_args: {"state": "synced"},
        now=100,
        max_workers=2,
    )

    assert gathered["deployment"]["status"] == "passed"
    assert gathered["production_qa"]["status"] == "passed"


def test_adapter_receives_exact_target_and_bounded_timeout(tmp_path):
    observed = {}

    def adapter(**kwargs):
        observed.update(kwargs)
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
        sync_canonical=lambda *_args: {"state": "synced"},
        now=100,
    )

    assert gathered["restart"] == {"status": "passed", "checked_at": 100, "observed_sha": TARGET}
    control = observed.pop("control")
    assert isinstance(control, receipts.PostMergeControl)
    assert control.cancelled() is False
    assert observed == {
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
        sync_canonical=lambda *_args: (_ for _ in ()).throw(
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
    }
    assert signals and signals[0][0] == 1234

    runtime.update(source_commit=TARGET)
    second = receipts.gateway_restart_adapter(
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
    assert second == {"status": "passed", "observed_sha": TARGET}
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
    started = threading.Event()
    stopped = threading.Event()
    late_side_effect = threading.Event()

    def adapter(*, control, **_kwargs):
        started.set()
        while not control.cancelled():
            time.sleep(0.005)
        if control.mutation_allowed():
            late_side_effect.set()
        stopped.set()
        return {"status": "blocked", "diagnostic_code": "collector_cancelled"}

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
        sync_canonical=lambda *_args: {"state": "synced"},
        now=100,
        max_workers=2,
    )

    assert started.is_set()
    assert stopped.is_set()
    assert late_side_effect.is_set() is False
    assert time.monotonic() - began < 1
    assert gathered["deployment"] == {
        "status": "blocked",
        "checked_at": 100,
        "diagnostic_code": "collector_timeout",
    }


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

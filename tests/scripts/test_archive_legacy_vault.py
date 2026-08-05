import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "archive_legacy_vault.py"
MERGE_SHA = "a" * 40


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("archive_legacy_vault", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    def __init__(self, root: Path):
        self.root = root
        self.units = {
            "gmail-intake-pubsub.service": {"active": "active", "enabled": "enabled"},
            "gmail-intake-watch-renew.timer": {"active": "active", "enabled": "enabled"},
            "gmail-intake-watch-renew.service": {"active": "active", "enabled": "disabled"},
            "hermes-gateway.service": {"active": "active", "enabled": "enabled"},
            "qmd-skills-refresh.service": {"active": "inactive", "enabled": "static", "failed": "inactive"},
        }
        self.calls = []
        self.ps_returncode = 0
        self.ps_stdout = ""
        self.fail_restart = False
        self.qmd_service_fails = False
        self.rotate_gateway_identity = True

    def run(self, args, *, timeout=60, check=True):
        self.calls.append(tuple(args))
        if args[:4] == ["git", "-C", str(self.root / "repo"), "merge-base"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args and args[0] in {"pgrep", "git"}:
            return subprocess.CompletedProcess(args, 1 if args[0] == "pgrep" else 0, "", "")
        if args and args[0] == "lsof":
            return subprocess.CompletedProcess(args, 1, "", "")
        if args and args[0] == "ps":
            return subprocess.CompletedProcess(args, self.ps_returncode, self.ps_stdout, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def git(self, repo, *args, check=True):
        if args == ("rev-parse", "HEAD"):
            return MERGE_SHA
        if args == ("status", "--porcelain"):
            return ""
        return ""

    def systemctl(self, *args, check=True):
        self.calls.append(("systemctl", *args))
        action = args[0]
        if action == "list-units":
            return ""
        if action in {"is-active", "is-enabled", "is-failed"}:
            unit = args[1]
            key = {"is-active": "active", "is-enabled": "enabled", "is-failed": "failed"}[action]
            return self.units.get(unit, {}).get(key, "inactive")
        if action in {"disable", "stop"}:
            unit = args[-1]
            self.units.setdefault(unit, {})["active"] = "inactive"
            if action == "disable":
                self.units[unit]["enabled"] = "disabled"
            return ""
        if action == "enable":
            self.units.setdefault(args[-1], {})["enabled"] = "enabled"
            return ""
        if action in {"start", "restart"}:
            unit = args[-1]
            if action == "restart" and self.fail_restart:
                if check:
                    raise RuntimeError("restart failed")
                return ""
            if unit == "qmd-skills-refresh.service":
                self.units[unit]["failed"] = "failed" if self.qmd_service_fails else "inactive"
                self.units[unit]["active"] = "inactive"
            else:
                self.units.setdefault(unit, {})["active"] = "active"
            if action == "restart" and unit == "hermes-gateway.service" and self.rotate_gateway_identity:
                home = self.root / "home"
                pid_record = json.loads((home / "gateway.pid").read_text())
                pid_record["pid"] += 1
                pid_record["start_time"] += 1
                (home / "gateway.pid").write_text(json.dumps(pid_record) + "\n")
                state = json.loads((home / "gateway_state.json").read_text())
                state.update(pid_record)
                state["gateway_state"] = "running"
                state["active_agents"] = 0
                (home / "gateway_state.json").write_text(json.dumps(state))
            return ""
        return ""

    def qmd(self, *args, check=True):
        self.calls.append(("qmd", *args))
        assert "pid-docs" not in args
        return "QMD Status skills healthy"


class FakeHoncho:
    def __init__(self):
        self.restores = []
        self.cards = {}

    def inventory(self, config_path, host):
        return {
            "enabled": True,
            "host": host,
            "workspace": "test",
            "scopes": [{"observer": "assistant", "target": "user"}],
        }

    def scrub(self, config_path, host, journal):
        key = (str(config_path), host, "assistant", "user")
        self.cards[key] = ["old"]
        intent = journal.intent("honcho_card_set", {
            "config_path": str(config_path), "host": host,
            "observer": "assistant", "target": "user", "before": ["old"], "after": ["clean"],
        })
        self.cards[key] = ["clean"]
        journal.after_mutation("honcho_card_set")
        journal.complete(intent, {"applied": True})
        return {
            "host": host,
            "changed_cards": [{
                "observer": "assistant", "target": "user", "before": ["old"], "after": ["clean"],
            }],
            "corrective_conclusions": [],
            "deleted_conclusions": [],
            "context_scopes": [{"observer": "assistant", "target": "user", "marker_present": False}],
            "remaining_search_hits": 1,
            "messages_preserved": True,
            "sessions_preserved": True,
        }

    def restore(self, config_path, host, snapshot, journal=None):
        self.restores.append((config_path, host, snapshot))
        for changed in snapshot.get("changed_cards", []):
            self.cards[(str(config_path), host, changed["observer"], changed["target"])] = changed["before"]

    def assert_unchanged(self, config_path, host, snapshot):
        for changed in snapshot.get("changed_cards", []):
            current = self.cards.get((str(config_path), host, changed["observer"], changed["target"]), changed["before"])
            assert current in (changed["before"], changed["after"])


def make_controller(tmp_path, mod, *, fault=""):
    home = tmp_path / "home"
    home.mkdir()
    (home / "profiles/uiux").mkdir(parents=True)
    (home / "obsidian-vault/sub").mkdir(parents=True)
    (home / "obsidian-vault/note.md").write_text("hello")
    (home / "obsidian-vault/sub/data.bin").write_bytes(b"\x00\x01")
    (home / "honcho.json").write_text(json.dumps({
        "enabled": True,
        "baseUrl": "http://127.0.0.1:8000",
        "hosts": {"hermes": {}, "hermes_uiux": {}},
    }))
    gateway_record = {"pid": 1234, "kind": "hermes-gateway", "start_time": 5678}
    (home / "gateway_state.json").write_text(json.dumps({
        **gateway_record, "active_agents": 0, "gateway_state": "running",
    }))
    (home / "gateway.pid").write_text(json.dumps(gateway_record) + "\n")
    for skill_home in (home, home / "profiles/uiux"):
        skills = skill_home / "skills"
        (skills / "note-taking/obsidian-project-curation").mkdir(parents=True)
        (skills / "note-taking/obsidian-project-curation/SKILL.md").write_text(
            "---\nname: obsidian-project-curation\ndescription: retired\n---\n"
        )
        (skills / "productivity/json-canvas").mkdir(parents=True)
        (skills / "productivity/json-canvas/SKILL.md").write_text(
            "---\nname: json-canvas\ndescription: Imported from Obsidian.\n---\n"
        )
        (skills / "safe/markdown").mkdir(parents=True)
        (skills / "safe/markdown/SKILL.md").write_text("---\nname: markdown\n---\n")
        (skills / ".hub").mkdir()
        (skills / ".hub/lock.json").write_text(json.dumps({"version": 1, "installed": {
            "json-canvas": {"identifier": "org/obsidian-json-canvas", "install_path": "productivity/json-canvas"},
            "markdown": {"identifier": "org/markdown", "install_path": "safe/markdown"},
        }}))
        (skills / ".hub/taps.json").write_text(json.dumps({"taps": [
            {"repo": "org/obsidian-skills"}, {"repo": "org/clean-skills"}
        ]}))
        (skills / ".usage.json").write_text(json.dumps({
            "obsidian-project-curation": {"use_count": 1}, "markdown": {"use_count": 2}
        }))
        (skills / ".bundled_manifest").write_text("obsidian-project-curation:abc\nmarkdown:def\n")
        (skill_home / ".skills_prompt_snapshot.json").write_text(json.dumps({"skills": ["obsolete"]}))
    (home / "memories").mkdir()
    (home / "memories/MEMORY.md").write_text(
        "Keep this current fact.\n\nUse Obsidian as the official vault and sync notes there.\n"
    )
    env_path = home / ".env"
    env_path.write_text("SAFE_NAME=value\nOBSIDIAN_TOKEN=secret-not-for-output\n")
    os.chmod(env_path, 0o640)
    gmail_script = home / "scripts/gmail-intake/gmail_intake_collector.py"
    gmail_script.parent.mkdir(parents=True)
    gmail_script.write_text(
        'parser.add_argument("--invoke-agent", default=os.environ.get("GMAIL_INTAKE_INVOKE_AGENT") == "1")\n'
        'if result["gated"] and args.invoke_agent:\n    pass\n'
        'if result["gated"] and invoke_agent_flag:\n    pass\n'
    )
    archive_parent = tmp_path / "archives"
    archive_parent.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = mod.HostPaths(
        home=home,
        systemd_dir=tmp_path / "systemd",
        archive_root=archive_parent / "hermes-retired-note-system",
        canonical_repo=repo,
        receipt_root=home / "retired-note-cutover-receipts",
        profile_homes=[home / "profiles/uiux"],
    )
    runner = FakeRunner(tmp_path)
    honcho = FakeHoncho()
    controller = mod.CutoverController(
        paths,
        runner=runner,
        honcho=honcho,
        now=lambda: "20260805T120000Z",
        fault_after_step=fault,
    )
    return controller, runner, honcho


def mutable_snapshot(controller, runner, mod):
    home = controller.paths.home
    paths = [
        home / ".env",
        home / "memories/MEMORY.md",
        home / "scripts/gmail-intake/gmail_intake_collector.py",
        home / "skills/.hub/lock.json",
        home / "skills/.hub/taps.json",
        home / "skills/.usage.json",
        home / "skills/.bundled_manifest",
        home / ".skills_prompt_snapshot.json",
        home / "profiles/uiux/skills/.hub/lock.json",
        home / "profiles/uiux/skills/.hub/taps.json",
        home / "profiles/uiux/skills/.usage.json",
        home / "profiles/uiux/skills/.bundled_manifest",
        home / "profiles/uiux/.skills_prompt_snapshot.json",
    ]
    files = {
        str(path): (path.read_bytes(), mod.file_fingerprint(path))
        for path in paths
    }
    skill_roots = [
        home / "skills/note-taking/obsidian-project-curation",
        home / "skills/productivity/json-canvas",
        home / "profiles/uiux/skills/note-taking/obsidian-project-curation",
        home / "profiles/uiux/skills/productivity/json-canvas",
    ]
    return {
        "vault": mod.tree_manifest(home / "obsidian-vault"),
        "files": files,
        "skills": {str(path): mod.tree_manifest(path) for path in skill_roots},
        "units": json.loads(json.dumps(runner.units)),
    }


def assert_mutable_snapshot(controller, runner, mod, snapshot):
    home = controller.paths.home
    assert mod.tree_manifest(home / "obsidian-vault") == snapshot["vault"]
    for raw_path, (content, fingerprint) in snapshot["files"].items():
        path = Path(raw_path)
        assert path.read_bytes() == content
        assert mod.file_fingerprint(path) == fingerprint
    for raw_path, manifest in snapshot["skills"].items():
        assert mod.tree_manifest(Path(raw_path)) == manifest
    for unit, state in snapshot["units"].items():
        assert runner.units[unit]["active"] == state["active"]
        assert runner.units[unit]["enabled"] == state["enabled"]


def test_preflight_is_dry_run_and_redacts_content(tmp_path, mod, capsys):
    controller, _, _ = make_controller(tmp_path, mod)
    source = controller.paths.home / "obsidian-vault"
    before = mod.tree_manifest(source)

    result = controller.preflight()
    public = mod.public_result(result)
    printed = json.dumps(public)

    assert public["dry_run"] is True
    assert public["host_count"] == 2
    assert public["selected_host_count"] == 2
    assert public["skill_removal_count"] == 4
    assert "note.md" not in printed
    assert "secret-not-for-output" not in printed
    assert mod.tree_manifest(source)["tree_hash"] == before["tree_hash"]
    assert not controller.paths.receipt_root.exists()
    assert not controller.paths.archive_root.exists()


def test_apply_requires_explicit_confirmation(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    with pytest.raises(mod.StopCutover, match="requires --confirm"):
        controller.apply(MERGE_SHA, "no")


def test_apply_requires_full_immutable_merge_sha(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    with pytest.raises(mod.StopCutover, match="full immutable commit hash"):
        controller.apply("short-sha", mod.CONFIRMATION)


def test_apply_verify_archive_and_restore_transactionally(tmp_path, mod):
    controller, runner, honcho = make_controller(tmp_path, mod)
    home = controller.paths.home
    original = mod.tree_manifest(home / "obsidian-vault")
    env_mode = stat.S_IMODE((home / ".env").stat().st_mode)

    result = controller.apply(MERGE_SHA, mod.CONFIRMATION)

    receipt_id = result["receipt_id"]
    receipt_dir = controller.paths.receipt_root / receipt_id
    receipt = json.loads((receipt_dir / "RECEIPT.json").read_text())
    destination = Path(receipt["steps"]["vault"]["destination"])
    assert result["state"] == "verified"
    assert not (home / "obsidian-vault").exists()
    run_dir = destination.parent
    assert all((run_dir / name).is_file() for name in mod.ARCHIVE_RECEIPT_FILES)
    assert not any((destination / name).exists() for name in mod.ARCHIVE_RECEIPT_FILES)
    assert stat.S_IMODE(receipt_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((receipt_dir / "RECEIPT.json").stat().st_mode) == 0o600
    assert "OBSIDIAN_TOKEN" not in (home / ".env").read_text()
    assert "SAFE_NAME=value" in (home / ".env").read_text()
    assert stat.S_IMODE((home / ".env").stat().st_mode) == env_mode
    assert "official vault" not in (home / "memories/MEMORY.md").read_text()
    assert (home / "skills/safe/markdown").is_dir()
    assert not (home / "skills/note-taking/obsidian-project-curation").exists()
    assert not (home / "skills/productivity/json-canvas").exists()
    assert not (home / ".skills_prompt_snapshot.json").exists()
    assert "invoke_agent_flag" in (home / "scripts/gmail-intake/gmail_intake_collector.py").read_text()
    assert "if False and" in (home / "scripts/gmail-intake/gmail_intake_collector.py").read_text()
    assert not any("pid-docs" in call for call in runner.calls)
    assert receipt["steps"]["honcho"][0]["messages_preserved"] is True

    verified = controller.verify(receipt_id)
    assert verified["source_absent"] is True

    restored = controller.restore(receipt_id)
    assert restored["state"] == "restored"
    assert mod.tree_manifest(home / "obsidian-vault")["tree_hash"] == original["tree_hash"]
    assert (home / "skills/note-taking/obsidian-project-curation").is_dir()
    assert (home / "skills/productivity/json-canvas").is_dir()
    assert "OBSIDIAN_TOKEN" in (home / ".env").read_text()
    assert honcho.restores


def test_archive_device_mismatch_stops_before_mutation(tmp_path, mod, monkeypatch):
    controller, _, _ = make_controller(tmp_path, mod)
    source = controller.paths.home / "obsidian-vault"
    real_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if Path(path) == controller.paths.archive_root.parent:
            values = list(st)
            values[2] = st.st_dev + 1
            return os.stat_result(values)
        return st

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(mod.StopCutover, match="not on the source filesystem"):
        controller.preflight()
    assert source.exists()


def test_archive_preflight_rejects_control_characters_in_payload_paths(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    source = controller.paths.home / "obsidian-vault"
    (source / "bad\tname.md").write_text("content")

    with pytest.raises(mod.StopCutover, match="unsupported archive path"):
        controller.preflight()
    assert source.is_dir()


def test_crash_after_vault_rename_rolls_back(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod, fault="vault-rename")
    source = controller.paths.home / "obsidian-vault"
    original = mod.tree_manifest(source)["tree_hash"]

    with pytest.raises(RuntimeError, match="injected crash"):
        controller.apply(MERGE_SHA, mod.CONFIRMATION)

    assert source.is_dir()
    assert mod.tree_manifest(source)["tree_hash"] == original


@pytest.mark.parametrize("fault", [
    "journal-honcho_card_set-intent",
    "journal-systemctl-intent",
    "journal-file_rewrite-intent",
    "journal-rename-intent",
    "journal-archive_manifest_write-intent",
    "honcho_card_set-mutation",
    "systemctl-gmail-intake-pubsub.service-disable-mutation",
    "invoke-agent-edit-mutation",
    "skill-rename-mutation",
    "skill-config-edit-mutation",
    "skill-snapshot-delete-mutation",
    "memory-edit-mutation",
    "environment-edit-mutation",
    "systemctl-qmd-skills-refresh.service-start-mutation",
    "vault-rename-mutation",
    "archive_manifest_write-mutation",
    "archive_outer_receipt_write-mutation",
    "gateway-drain-marker-mutation",
    "systemctl-hermes-gateway.service-restart-mutation",
    "gateway-drain-marker-clear-mutation",
])
def test_every_mutation_crash_window_restores_exact_pre_state(tmp_path, mod, fault):
    controller, runner, honcho = make_controller(tmp_path, mod, fault=fault)
    snapshot = mutable_snapshot(controller, runner, mod)

    with pytest.raises(RuntimeError, match="injected crash"):
        controller.apply(MERGE_SHA, mod.CONFIRMATION)

    assert_mutable_snapshot(controller, runner, mod, snapshot)
    assert all(card == ["old"] for card in honcho.cards.values())
    assert not (controller.paths.home / ".drain_request.json").exists()


def test_qmd_fallback_crash_is_compensated_after_local_restore(tmp_path, mod):
    controller, runner, _ = make_controller(tmp_path, mod, fault="qmd_update-mutation")
    runner.qmd_service_fails = True
    snapshot = mutable_snapshot(controller, runner, mod)

    with pytest.raises(RuntimeError, match="injected crash"):
        controller.apply(MERGE_SHA, mod.CONFIRMATION)

    assert_mutable_snapshot(controller, runner, mod, snapshot)
    qmd_updates = [call for call in runner.calls if call[:4] == ("qmd", "--index", "skills", "update")]
    assert len(qmd_updates) == 2


def test_preexisting_drain_marker_survives_apply_and_restore(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    marker = controller.paths.home / ".drain_request.json"
    marker.write_text('{"operator":"keep"}\n')
    before = marker.read_bytes()
    before_mode = stat.S_IMODE(marker.stat().st_mode)

    result = controller.apply(MERGE_SHA, mod.CONFIRMATION)

    assert not marker.exists()
    controller.restore(result["receipt_id"])
    assert marker.read_bytes() == before
    assert stat.S_IMODE(marker.stat().st_mode) == before_mode


def test_payload_control_filenames_round_trip_without_collision(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    source = controller.paths.home / "obsidian-vault"
    payloads = {
        "MANIFEST.json": b"payload-manifest",
        "SHA256SUMS": b"payload-sums",
        "FILE_METADATA.tsv": b"payload-metadata",
        "VERIFICATION_RECEIPT.json": b"payload-receipt",
        "TRANSACTION.jsonl": b"payload-journal",
        "RESTORE.jsonl": b"payload-restore-journal",
    }
    for name, content in payloads.items():
        (source / name).write_bytes(content)
    original = mod.tree_manifest(source)

    result = controller.apply(MERGE_SHA, mod.CONFIRMATION)
    receipt_dir = controller.paths.receipt_root / result["receipt_id"]
    receipt = json.loads((receipt_dir / "RECEIPT.json").read_text())
    destination = Path(receipt["steps"]["vault"]["destination"])
    for name, content in payloads.items():
        assert (destination / name).read_bytes() == content
        assert (destination.parent / name).is_file() if name in mod.ARCHIVE_RECEIPT_FILES else True

    controller.restore(result["receipt_id"])

    assert mod.tree_manifest(source) == original
    for name, content in payloads.items():
        assert (source / name).read_bytes() == content
    assert not any((destination.parent / name).exists() for name in mod.ARCHIVE_RECEIPT_FILES)


def _applied_archive(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    result = controller.apply(MERGE_SHA, mod.CONFIRMATION)
    receipt_dir = controller.paths.receipt_root / result["receipt_id"]
    receipt = json.loads((receipt_dir / "RECEIPT.json").read_text())
    destination = Path(receipt["steps"]["vault"]["destination"])
    return controller, result["receipt_id"], receipt_dir, destination


@pytest.mark.parametrize("artifact", [
    "MANIFEST.json", "SHA256SUMS", "FILE_METADATA.tsv", "VERIFICATION_RECEIPT.json",
])
def test_archive_control_artifact_tamper_fails_verify_and_restore(tmp_path, mod, artifact):
    controller, receipt_id, _, destination = _applied_archive(tmp_path, mod)
    path = destination.parent / artifact
    path.write_bytes(path.read_bytes() + b"tamper")

    with pytest.raises(mod.StopCutover):
        controller.verify(receipt_id)
    with pytest.raises(mod.StopCutover):
        controller.restore(receipt_id)
    assert destination.is_dir()
    assert not (controller.paths.home / "obsidian-vault").exists()


def test_archive_payload_bytes_and_metadata_tamper_fail_closed(tmp_path, mod):
    controller, receipt_id, _, destination = _applied_archive(tmp_path, mod)
    payload = destination / "note.md"
    payload.write_text("tampered")

    with pytest.raises(mod.StopCutover, match="archive payload"):
        controller.verify(receipt_id)
    with pytest.raises(mod.StopCutover):
        controller.restore(receipt_id)


def test_outer_receipt_field_tamper_cannot_be_self_rehashed(tmp_path, mod):
    controller, receipt_id, _, destination = _applied_archive(tmp_path, mod)
    outer_path = destination.parent / "VERIFICATION_RECEIPT.json"
    outer = json.loads(outer_path.read_text())
    outer["counts"]["files"] += 1
    outer_path.write_text(json.dumps(outer, indent=2, sort_keys=True) + "\n")

    with pytest.raises(mod.StopCutover, match="journaled write intent"):
        controller.verify(receipt_id)


def test_transaction_journal_hash_and_chain_tamper_fail_closed(tmp_path, mod):
    controller, receipt_id, receipt_dir, destination = _applied_archive(tmp_path, mod)
    journal = receipt_dir / mod.JOURNAL_FILE
    lines = journal.read_text().splitlines()
    record = json.loads(lines[-1])
    record["previous_hash"] = "f" * 64
    lines[-1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    journal.write_text("\n".join(lines) + "\n")

    with pytest.raises(mod.StopCutover, match="journal"):
        controller.verify(receipt_id)
    with pytest.raises(mod.StopCutover, match="journal"):
        controller.restore(receipt_id)
    assert destination.is_dir()


def test_transaction_journal_missing_final_seal_fails_closed(tmp_path, mod):
    controller, receipt_id, receipt_dir, destination = _applied_archive(tmp_path, mod)
    journal = receipt_dir / mod.JOURNAL_FILE
    lines = journal.read_text().splitlines()
    assert json.loads(lines[-1])["phase"] == "seal"
    journal.write_text("\n".join(lines[:-1]) + "\n")

    with pytest.raises(mod.StopCutover, match="not sealed"):
        controller.verify(receipt_id)
    with pytest.raises(mod.StopCutover, match="not sealed"):
        controller.restore(receipt_id)
    assert destination.is_dir()


def test_gateway_idle_gate_rejects_active_turn(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    state = json.loads((controller.paths.home / "gateway_state.json").read_text())
    state["active_agents"] = 1
    (controller.paths.home / "gateway_state.json").write_text(json.dumps(state))

    with pytest.raises(mod.StopCutover, match="gateway work is active"):
        controller.preflight(MERGE_SHA, require_merged=True)


def test_gateway_idle_gate_requires_active_service(tmp_path, mod):
    controller, runner, _ = make_controller(tmp_path, mod)
    runner.units[mod.GATEWAY_UNIT]["active"] = "inactive"

    with pytest.raises(mod.StopCutover, match="gateway service is not active"):
        controller.preflight(MERGE_SHA, require_merged=True)


@pytest.mark.parametrize("active_agents", [True, "bad", -1])
def test_gateway_idle_gate_rejects_malformed_runtime_status(tmp_path, mod, active_agents):
    controller, _, _ = make_controller(tmp_path, mod)
    payload = json.loads((controller.paths.home / "gateway_state.json").read_text())
    payload["active_agents"] = active_agents
    (controller.paths.home / "gateway_state.json").write_text(json.dumps(payload))

    with pytest.raises(mod.StopCutover, match="active_agents status is malformed"):
        controller.preflight(MERGE_SHA, require_merged=True)


def test_gateway_idle_gate_rejects_missing_active_agent_count(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    payload = json.loads((controller.paths.home / "gateway_state.json").read_text())
    del payload["active_agents"]
    (controller.paths.home / "gateway_state.json").write_text(json.dumps(payload))

    with pytest.raises(mod.StopCutover, match="active_agents status is malformed"):
        controller.preflight(MERGE_SHA, require_merged=True)


@pytest.mark.parametrize("field,value,message", [
    ("gateway_state", "starting", "not drainable"),
    ("pid", 9999, "does not match"),
    ("start_time", 9999, "stale process identity"),
])
def test_gateway_idle_gate_rejects_stale_or_nondrainable_status(tmp_path, mod, field, value, message):
    controller, _, _ = make_controller(tmp_path, mod)
    payload = json.loads((controller.paths.home / "gateway_state.json").read_text())
    payload[field] = value
    (controller.paths.home / "gateway_state.json").write_text(json.dumps(payload))

    with pytest.raises(mod.StopCutover, match=message):
        controller.preflight(MERGE_SHA, require_merged=True)


@pytest.mark.parametrize("missing", ["gateway_state.json", "gateway.pid"])
def test_gateway_idle_gate_requires_runtime_evidence(tmp_path, mod, missing):
    controller, _, _ = make_controller(tmp_path, mod)
    (controller.paths.home / missing).unlink()

    with pytest.raises(mod.StopCutover, match="missing or unsafe"):
        controller.preflight(MERGE_SHA, require_merged=True)


@pytest.mark.parametrize("raw", ["not-json", "{}", "true", "-1"])
def test_gateway_idle_gate_rejects_malformed_pid_record(tmp_path, mod, raw):
    controller, _, _ = make_controller(tmp_path, mod)
    (controller.paths.home / "gateway.pid").write_text(raw)

    with pytest.raises(mod.StopCutover, match="PID file is malformed"):
        controller.preflight(MERGE_SHA, require_merged=True)


def test_gateway_process_inspection_error_fails_closed(tmp_path, mod):
    controller, runner, _ = make_controller(tmp_path, mod)
    runner.ps_returncode = 2

    with pytest.raises(mod.StopCutover, match="process inspection failed"):
        controller.preflight(MERGE_SHA, require_merged=True)


def test_gateway_ignores_unrelated_child_but_blocks_owned_worker(tmp_path, mod):
    controller, runner, _ = make_controller(tmp_path, mod)
    runner.ps_stdout = "2222 /usr/bin/sleep 30\n"
    assert controller.preflight(MERGE_SHA, require_merged=True)["gateway_idle"]["owned_workers"] == []

    runner.ps_stdout = "2222 python run_agent.py --session test\n"
    with pytest.raises(mod.StopCutover, match="owned_workers=1"):
        controller.preflight(MERGE_SHA, require_merged=True)


def test_gateway_drain_timeout_leaves_journaled_marker_for_restore(tmp_path, mod, monkeypatch):
    controller, _, _ = make_controller(tmp_path, mod)
    receipt_dir = controller.paths.receipt_root / "manual"
    controller._journal = mod.TransactionJournal(receipt_dir / mod.JOURNAL_FILE)
    state = json.loads((controller.paths.home / "gateway_state.json").read_text())
    state["active_agents"] = 1
    state["gateway_state"] = "draining"
    (controller.paths.home / "gateway_state.json").write_text(json.dumps(state))
    monkeypatch.setattr(mod, "GATEWAY_DRAIN_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(mod.StopCutover, match="drain timed out"):
        controller._drain_and_restart_gateway(controller.paths.home / "obsidian-vault")

    marker = controller.paths.home / ".drain_request.json"
    assert marker.is_file()
    actions = [record["action"] for record in mod.TransactionJournal.read_records(receipt_dir / mod.JOURNAL_FILE)]
    assert actions[:2] == ["gateway_drain_marker_write", "gateway_drain_marker_write"]


def test_gateway_restart_failure_triggers_exact_automatic_rollback(tmp_path, mod):
    controller, runner, _ = make_controller(tmp_path, mod)
    runner.fail_restart = True
    snapshot = mutable_snapshot(controller, runner, mod)

    with pytest.raises(RuntimeError, match="restart failed"):
        controller.apply(MERGE_SHA, mod.CONFIRMATION)

    assert_mutable_snapshot(controller, runner, mod, snapshot)
    assert not (controller.paths.home / ".drain_request.json").exists()


def test_gateway_restart_requires_new_process_identity(tmp_path, mod, monkeypatch):
    controller, runner, _ = make_controller(tmp_path, mod)
    runner.rotate_gateway_identity = False
    snapshot = mutable_snapshot(controller, runner, mod)
    monkeypatch.setattr(mod, "GATEWAY_RESTART_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(mod.StopCutover, match="identity did not become observable"):
        controller.apply(MERGE_SHA, mod.CONFIRMATION)

    assert_mutable_snapshot(controller, runner, mod, snapshot)
    assert not (controller.paths.home / ".drain_request.json").exists()


def test_restore_fails_closed_on_concurrent_drift(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    result = controller.apply(MERGE_SHA, mod.CONFIRMATION)
    env_path = controller.paths.home / ".env"
    env_path.write_text(env_path.read_text() + "DRIFT=1\n")

    with pytest.raises(mod.StopCutover, match="concurrent drift"):
        controller.restore(result["receipt_id"])
    assert not (controller.paths.home / "obsidian-vault").exists()


def test_fsync_failure_restores_source(tmp_path, mod, monkeypatch):
    controller, _, _ = make_controller(tmp_path, mod)
    source = controller.paths.home / "obsidian-vault"
    real_fsync_tree = mod.fsync_tree

    def fail_once(root):
        if Path(root).name == "vault":
            raise OSError("fsync failed")
        return real_fsync_tree(root)

    monkeypatch.setattr(mod, "fsync_tree", fail_once)
    with pytest.raises(OSError, match="fsync failed"):
        controller.apply(MERGE_SHA, mod.CONFIRMATION)
    assert source.is_dir()


def test_honcho_conclusion_inventory_paginates(mod):
    pages = {
        1: [object() for _ in range(100)],
        2: [object() for _ in range(3)],
    }

    class Scope:
        def list(self, page=1, size=100):
            return type("Page", (), {"items": pages[page]})()

    assert len(mod.HonchoBackend._all_conclusions(Scope())) == 103


def _install_fake_honcho_sdk(monkeypatch, *, marker_card=True, marker_conclusion=True):
    class Item:
        def __init__(self, item_id, content=""):
            self.id = item_id
            self.content = content

    class Page:
        def __init__(self, items):
            self.items = items

    class Scope:
        def __init__(self):
            self.items = [Item("historical-1", "Use Obsidian vault sync.")] if marker_conclusion else []
            self.deleted = []
            self.next_id = 1

        def list(self, page=1, size=100):
            return Page(self.items if page == 1 else [])

        def create(self, payloads):
            created = []
            for payload in payloads:
                item = Item(f"created-{self.next_id}", payload["content"])
                self.next_id += 1
                self.items.append(item)
                created.append(item)
            return Page(created)

        def delete(self, item_id):
            self.deleted.append(item_id)
            self.items = [item for item in self.items if item.id != item_id]

    class Peer:
        def __init__(self):
            self.card = ["Use Obsidian vault sync.", "keep"] if marker_card else ["keep"]
            self.scope = Scope()

        def get_card(self, target):
            return list(self.card)

        def set_card(self, values, target):
            self.card = list(values)

        def conclusions_of(self, target):
            return self.scope

        def context(self, target, search_query, search_top_k):
            return "clean"

    class Client:
        def __init__(self):
            self.the_peer = Peer()

        def peers(self, page=1, size=100):
            return Page([Item("assistant")] if page == 1 else [])

        def peer(self, peer_id):
            return self.the_peer

        def search(self, query, limit=100):
            return []

    class Config:
        enabled = True
        workspace_id = "test"

        @classmethod
        def from_global_config(cls, host, config_path):
            return cls()

    import plugins.memory.honcho.client as client_module

    client = Client()
    monkeypatch.setattr(client_module, "HonchoClientConfig", Config)
    monkeypatch.setattr(client_module, "get_honcho_client", lambda config: client)
    monkeypatch.setattr(client_module, "reset_honcho_client", lambda: None)
    return client


def test_honcho_backend_preserves_history_and_deletes_owned_correction_on_restore(tmp_path, mod, monkeypatch):
    client = _install_fake_honcho_sdk(monkeypatch)
    journal = mod.TransactionJournal(tmp_path / "apply.jsonl")
    backend = mod.HonchoBackend()

    snapshot = backend.scrub(tmp_path / "honcho.json", "hermes", journal)

    assert [item.id for item in client.the_peer.scope.items if item.id.startswith("historical-")] == ["historical-1"]
    assert client.the_peer.card == ["keep"]
    correction = snapshot["corrective_conclusions"][0]
    assert correction["matched_conclusion_ids"] == ["historical-1"]
    assert correction["created_ids"] == ["created-1"]

    restore_journal = mod.TransactionJournal(tmp_path / "restore.jsonl")
    backend.assert_unchanged(tmp_path / "honcho.json", "hermes", snapshot)
    backend.restore(tmp_path / "honcho.json", "hermes", snapshot, restore_journal)

    assert client.the_peer.card == ["Use Obsidian vault sync.", "keep"]
    assert client.the_peer.scope.deleted == ["created-1"]
    assert [item.id for item in client.the_peer.scope.items] == ["historical-1"]


def test_honcho_post_create_crash_recovers_owned_correction_by_exact_content(tmp_path, mod, monkeypatch):
    client = _install_fake_honcho_sdk(monkeypatch, marker_card=False)

    def fault(label):
        if label == "honcho_correction_create-mutation":
            raise RuntimeError("injected crash")

    journal = mod.TransactionJournal(tmp_path / "apply.jsonl", fault=fault)
    backend = mod.HonchoBackend()
    with pytest.raises(RuntimeError, match="injected crash"):
        backend.scrub(tmp_path / "honcho.json", "hermes", journal)

    snapshots = mod.CutoverController._honcho_snapshots_from_journal(journal)
    correction = snapshots[0]["corrective_conclusions"][0]
    assert correction["created_ids"] == []
    assert correction["completed"] is False
    assert [item.id for item in client.the_peer.scope.items] == ["historical-1", "created-1"]

    backend.restore(
        tmp_path / "honcho.json",
        "hermes",
        snapshots[0],
        mod.TransactionJournal(tmp_path / "restore.jsonl"),
    )

    assert client.the_peer.scope.deleted == ["created-1"]
    assert [item.id for item in client.the_peer.scope.items] == ["historical-1"]


def test_preflight_fails_closed_on_malformed_honcho_config(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    (controller.paths.home / "honcho.json").write_text("not-json")
    with pytest.raises(mod.StopCutover, match="malformed JSON"):
        controller.preflight()


def test_host_inventory_never_materializes_discovered_profiles(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    config_path = controller.paths.home / "honcho.json"
    config_path.write_text(json.dumps({
        "enabled": True,
        "baseUrl": "http://127.0.0.1:8000",
        "hosts": {
            "hermes": {"enabled": True},
            "hermes.uiux": {"enabled": False},
        },
    }))
    before = config_path.read_bytes()

    inventory = controller._host_inventory()
    controller._ensure_unknown_hosts_closed(tmp_path / "unused")

    assert {item["host"] for item in inventory} == {"hermes", "hermes.uiux"}
    assert next(item for item in inventory if item["host"] == "hermes.uiux")["selected"] is False
    assert config_path.read_bytes() == before


def test_unknown_profile_host_stays_unavailable(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    config_path = controller.paths.home / "honcho.json"
    config_path.write_text(json.dumps({
        "enabled": True,
        "baseUrl": "http://127.0.0.1:8000",
        "hosts": {"hermes": {"enabled": True}},
    }))

    inventory = controller._host_inventory()

    assert [item["host"] for item in inventory] == ["hermes"]


def test_tree_manifest_records_symlink_metadata(tmp_path, mod):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "target.md").write_text("target")
    (root / "alias.md").symlink_to("target.md")
    manifest = mod.tree_manifest(root)
    alias = next(entry for entry in manifest["entries"] if entry["path"] == "alias.md")
    assert alias["kind"] == "symlink"
    assert alias["sha256"] == mod.sha256_bytes(b"target.md")


def test_cli_defaults_to_preflight_and_never_prints_secret(tmp_path):
    home = tmp_path / "home"
    (home / "obsidian-vault").mkdir(parents=True)
    (home / "obsidian-vault/note.md").write_text("private content")
    archive_parent = tmp_path / "archives"
    archive_parent.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--home", str(home),
            "--archive-root", str(archive_parent / "retired"),
            "--receipt-root", str(home / "receipts"),
            "--canonical-repo", str(tmp_path / "repo"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "preflight"
    assert payload["dry_run"] is True
    assert "note.md" not in result.stdout
    assert "private content" not in result.stdout
    assert (home / "obsidian-vault").exists()
    assert not (home / "receipts").exists()

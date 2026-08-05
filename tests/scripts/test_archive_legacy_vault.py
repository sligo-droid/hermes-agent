import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "archive_legacy_vault.py"


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

    def run(self, args, *, timeout=60, check=True):
        self.calls.append(tuple(args))
        if args[:4] == ["git", "-C", str(self.root / "repo"), "merge-base"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args and args[0] in {"pgrep", "git"}:
            return subprocess.CompletedProcess(args, 1 if args[0] == "pgrep" else 0, "", "")
        if args and args[0] == "lsof":
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def git(self, repo, *args, check=True):
        if args == ("rev-parse", "HEAD"):
            return "merged-sha"
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
            if unit == "qmd-skills-refresh.service":
                self.units[unit]["failed"] = "inactive"
                self.units[unit]["active"] = "inactive"
            else:
                self.units.setdefault(unit, {})["active"] = "active"
            return ""
        return ""

    def qmd(self, *args, check=True):
        self.calls.append(("qmd", *args))
        assert "pid-docs" not in args
        return "QMD Status skills healthy"


class FakeHoncho:
    def __init__(self):
        self.restores = []

    def inventory(self, config_path, host):
        return {
            "enabled": True,
            "host": host,
            "workspace": "test",
            "scopes": [{"observer": "assistant", "target": "user"}],
        }

    def scrub(self, config_path, host):
        return {
            "host": host,
            "changed_cards": [],
            "deleted_conclusions": [],
            "context_scopes": [{"observer": "assistant", "target": "user", "marker_present": False}],
            "remaining_search_hits": 1,
            "messages_preserved": True,
            "sessions_preserved": True,
        }

    def restore(self, config_path, host, snapshot):
        self.restores.append((config_path, host, snapshot))

    def assert_unchanged(self, config_path, host, snapshot):
        return None


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
        controller.apply("merged-sha", "no")


def test_apply_verify_archive_and_restore_transactionally(tmp_path, mod):
    controller, runner, honcho = make_controller(tmp_path, mod)
    home = controller.paths.home
    original = mod.tree_manifest(home / "obsidian-vault")
    env_mode = stat.S_IMODE((home / ".env").stat().st_mode)

    result = controller.apply("merged-sha", mod.CONFIRMATION)

    receipt_id = result["receipt_id"]
    receipt_dir = controller.paths.receipt_root / receipt_id
    receipt = json.loads((receipt_dir / "RECEIPT.json").read_text())
    destination = Path(receipt["steps"]["vault"]["destination"])
    assert result["state"] == "verified"
    assert not (home / "obsidian-vault").exists()
    assert all((destination / name).is_file() for name in mod.VAULT_RECEIPT_FILES)
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


def test_crash_after_vault_rename_rolls_back(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod, fault="vault-rename")
    source = controller.paths.home / "obsidian-vault"
    original = mod.tree_manifest(source)["tree_hash"]

    with pytest.raises(RuntimeError, match="injected crash"):
        controller.apply("merged-sha", mod.CONFIRMATION)

    assert source.is_dir()
    assert mod.tree_manifest(source)["tree_hash"] == original


def test_restore_fails_closed_on_concurrent_drift(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    result = controller.apply("merged-sha", mod.CONFIRMATION)
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
        controller.apply("merged-sha", mod.CONFIRMATION)
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


def test_preflight_fails_closed_on_malformed_honcho_config(tmp_path, mod):
    controller, _, _ = make_controller(tmp_path, mod)
    (controller.paths.home / "honcho.json").write_text("not-json")
    with pytest.raises(mod.StopCutover, match="malformed JSON"):
        controller.preflight()


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

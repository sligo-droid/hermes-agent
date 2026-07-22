import subprocess
from types import SimpleNamespace

from gateway.discord_project_mapping import resolve_discord_project_context
from hermes_state import SessionDB


class FakeChannel:
    def __init__(self, *, channel_id, name, guild=None, parent=None, category=None):
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.parent = parent
        self.parent_id = getattr(parent, "id", None)
        self.category = category


def _guild():
    return SimpleNamespace(id="guild-1", name="Sligo Labs")


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


def test_existing_db_mapping_wins_over_workspace_scan(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.upsert_discord_project_mapping(
        guild_id="guild-1",
        channel_id="chan-1",
        channel_name="pid",
        guild_name="Sligo Labs",
        project_key="CanonicalPID",
        project_name="Canonical PID",
        project_path="/canonical/pid",
        github_url="https://github.com/sligo-labs/canonical-pid",
        source="manual",
    )
    workspace = tmp_path / "workspace"
    (workspace / "PID").mkdir(parents=True)

    ctx = resolve_discord_project_context(
        FakeChannel(channel_id="chan-1", name="pid", guild=_guild()),
        session_db=db,
        workspace_root=workspace,
    )

    assert ctx is not None
    assert ctx.resolved is True
    assert ctx.project_path == "/canonical/pid"
    assert ctx.mapping_source == "manual"
    db.close()


def test_existing_mapping_is_dynamically_enriched_without_schema_change(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.upsert_discord_project_mapping(
        guild_id="guild-1",
        channel_id="chan-1",
        channel_name="example",
        guild_name="Sligo Labs",
        project_key="legacy-key",
        project_name="Example",
        project_path="/canonical/example",
        github_url="git@github.com:sligo-labs/example.git",
        source="manual",
    )

    ctx = resolve_discord_project_context(
        FakeChannel(channel_id="chan-1", name="example", guild=_guild()),
        session_db=db,
        workspace_root=tmp_path / "empty",
        config={
            "projects": {
                "example": {
                    "repository": "SLIGO-LABS/EXAMPLE",
                    "inspection": {
                        "development_urls": ["http://localhost:3000"],
                        "production_urls": ["https://example.test"],
                    },
                }
            }
        },
    )

    assert ctx is not None
    assert ctx.project_key == "example"
    assert [candidate.url for candidate in ctx.inspection_candidates] == [
        "http://localhost:3000/",
        "https://example.test/",
    ]
    assert ctx.to_dict()["project_inspection_candidates"][0]["location"] == "local"
    assert ctx.to_dict()["project_key"] == "example"
    persisted = db.get_discord_project_mapping(guild_id="guild-1", channel_id="chan-1")
    assert "inspection_candidates" not in persisted
    db.close()


def test_existing_mapping_reconciles_stale_github_origin(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    project = tmp_path / "PID"
    project.mkdir()
    _git(project, "init")
    _git(project, "remote", "add", "origin", "git@github.com:sligo-labs/PID.git")
    db.upsert_discord_project_mapping(
        guild_id="guild-1",
        channel_id="chan-1",
        channel_name="pid",
        guild_name="Sligo Labs",
        project_key="PID",
        project_name="Pid",
        project_path=str(project),
        github_url="https://github.com/sligo-droid/PID",
        source="deterministic_directory_bootstrap",
    )

    ctx = resolve_discord_project_context(
        FakeChannel(channel_id="chan-1", name="pid", guild=_guild()),
        session_db=db,
    )

    assert ctx is not None
    assert ctx.github_url == "https://github.com/sligo-labs/PID"
    persisted = db.get_discord_project_mapping(
        guild_id="guild-1",
        channel_id="chan-1",
    )
    assert persisted is not None
    assert persisted["github_url"] == "https://github.com/sligo-labs/PID"
    assert persisted["source"] == "deterministic_directory_bootstrap"
    db.close()


def test_bootstraps_unique_workspace_directory(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    workspace = tmp_path / "workspace"
    project = workspace / "PID"
    project.mkdir(parents=True)

    ctx = resolve_discord_project_context(
        FakeChannel(channel_id="chan-1", name="pid", guild=_guild()),
        session_db=db,
        workspace_root=workspace,
    )

    assert ctx is not None
    assert ctx.resolved is True
    assert ctx.project_key == "PID"
    assert ctx.project_path == str(project.resolve())
    assert ctx.mapping_source == "deterministic_directory_bootstrap"

    persisted = db.get_discord_project_mapping(guild_id="guild-1", channel_id="chan-1")
    assert persisted is not None
    assert persisted["project_path"] == str(project.resolve())
    db.close()


def test_thread_inherits_parent_project_channel_mapping(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    workspace = tmp_path / "workspace"
    project = workspace / "PID"
    project.mkdir(parents=True)
    parent = FakeChannel(channel_id="chan-1", name="pid", guild=_guild())
    thread = FakeChannel(channel_id="thread-1", name="feature", guild=_guild(), parent=parent)

    ctx = resolve_discord_project_context(thread, session_db=db, workspace_root=workspace)

    assert ctx is not None
    assert ctx.channel_id == "chan-1"
    assert ctx.project_path == str(project.resolve())
    db.close()


def test_configured_channel_cwd_becomes_project_context(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = tmp_path / "hermes"
    project.mkdir()

    ctx = resolve_discord_project_context(
        FakeChannel(channel_id="dev-chan", name="dev", guild=_guild()),
        session_db=db,
        workspace_root=workspace,
        config={"discord": {"channel_cwds": {"dev-chan": str(project)}}},
    )

    assert ctx is not None
    assert ctx.resolved is True
    assert ctx.channel_id == "dev-chan"
    assert ctx.project_path == str(project.resolve())
    assert ctx.project_key == "hermes"
    assert ctx.mapping_source == "configured_channel_cwd"
    assert db.get_discord_project_mapping(guild_id="guild-1", channel_id="dev-chan") is None
    db.close()


def test_configured_channel_cwd_applies_to_threads(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = tmp_path / "hermes"
    project.mkdir()
    parent = FakeChannel(channel_id="dev-chan", name="dev", guild=_guild())
    thread = FakeChannel(channel_id="thread-1", name="feature", guild=_guild(), parent=parent)

    ctx = resolve_discord_project_context(
        thread,
        session_db=db,
        workspace_root=workspace,
        config={"discord": {"channel_cwds": {"dev-chan": str(project)}}},
    )

    assert ctx is not None
    assert ctx.channel_id == "dev-chan"
    assert ctx.project_path == str(project.resolve())
    assert ctx.mapping_source == "configured_channel_cwd"
    db.close()


def test_configured_channel_and_category_names_are_not_project_mapped(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    guild = _guild()

    assert resolve_discord_project_context(
        FakeChannel(channel_id="admin", name="admin", guild=guild),
        session_db=db,
        workspace_root=workspace,
        config={"discord": {"project_mapping_ignored_channel_names": "admin"}},
    ) is None
    assert resolve_discord_project_context(
        FakeChannel(channel_id="human", name="pid-human", guild=guild),
        session_db=db,
        workspace_root=workspace,
        config={"discord": {"project_mapping_ignored_channel_names": "*human*"}},
    ) is None
    assert resolve_discord_project_context(
        FakeChannel(
            channel_id="infra",
            name="alerts",
            guild=guild,
            category=SimpleNamespace(name="Infra"),
        ),
        session_db=db,
        workspace_root=workspace,
        config={"discord": {"project_mapping_ignored_category_names": "infra"}},
    ) is None
    db.close()


def test_unmapped_project_channel_is_explicitly_unresolved(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    ctx = resolve_discord_project_context(
        FakeChannel(channel_id="chan-1", name="pid", guild=_guild()),
        session_db=db,
        workspace_root=workspace,
    )

    assert ctx is not None
    assert ctx.resolved is False
    assert ctx.project_path is None
    assert db.get_discord_project_mapping(guild_id="guild-1", channel_id="chan-1") is None
    db.close()

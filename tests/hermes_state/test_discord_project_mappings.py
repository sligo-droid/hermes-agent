from hermes_state import SessionDB


def test_discord_project_mapping_upsert_get_and_list(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")

    mapping = db.upsert_discord_project_mapping(
        guild_id="guild-1",
        channel_id="chan-1",
        parent_channel_id=None,
        channel_name="pid",
        guild_name="Sligo Labs",
        project_key="PID",
        project_name="PID",
        project_path="/home/droid/.hermes/workspace/PID",
        github_url="https://github.com/sligo-labs/pid",
        source="manual",
    )

    assert mapping["guild_id"] == "guild-1"
    assert mapping["channel_id"] == "chan-1"
    assert mapping["project_path"] == "/home/droid/.hermes/workspace/PID"

    db.upsert_discord_project_mapping(
        guild_id="guild-1",
        channel_id="chan-1",
        channel_name="pid-renamed",
        guild_name="Sligo Labs",
        project_key="PID",
        project_name="Political Intelligence Dashboard",
        project_path="/home/droid/.hermes/workspace/PID",
        github_url="https://github.com/sligo-labs/pid",
        source="deterministic_directory_bootstrap",
    )

    updated = db.get_discord_project_mapping(guild_id="guild-1", channel_id="chan-1")
    assert updated is not None
    assert updated["channel_name"] == "pid-renamed"
    assert updated["project_name"] == "Political Intelligence Dashboard"
    assert updated["created_at"] == mapping["created_at"]
    assert updated["updated_at"] >= mapping["updated_at"]

    rows = db.list_discord_project_mappings(guild_id="guild-1")
    assert len(rows) == 1
    assert rows[0]["channel_id"] == "chan-1"

    db.close()

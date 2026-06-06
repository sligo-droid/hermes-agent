from hermes_cli import command_center_verification


def test_completed_action_summary_counts_board_revert_and_archive_payloads():
    snapshot = {
        "work_items": [
            {
                "id": "kanban-board:done-board",
                "project": "hermes",
                "status": "shipped",
                "decision": {"needed": False},
                "execution": {
                    "board": "done-board",
                    "archiveable": True,
                    "undo_followup_action": "/api/plugins/kanban/boards/done-board/undo-followup",
                },
            },
            {
                "id": "kanban-board:missing-revert",
                "project": "hermes",
                "status": "done",
                "decision": {"needed": False},
                "execution": {"board": "missing-revert", "archiveable": True},
            },
            {
                "id": "kanban-board:other-project",
                "project": "pid",
                "status": "shipped",
                "execution": {"undo_followup_action": "/unused"},
            },
        ]
    }

    summary = command_center_verification.summarize_completed_actions(snapshot, project="hermes")

    assert summary == {
        "project": "hermes",
        "completed_count": 2,
        "revertable_completed_count": 1,
        "archiveable_completed_count": 2,
        "missing_revert_count": 1,
        "missing_revert_ids": ["kanban-board:missing-revert"],
        "revertable_completed_ids": ["kanban-board:done-board"],
    }


def test_verification_cli_require_revertable_fails_when_completed_rows_lack_revert(tmp_path, capsys):
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        '{"work_items":[{"id":"kanban-board:no-revert","project":"hermes","status":"shipped","execution":{}}]}',
        encoding="utf-8",
    )

    result = command_center_verification.main([str(snapshot_path), "--project", "hermes", "--require-revertable"])

    assert result == 1
    assert '"missing_revert_ids": [' in capsys.readouterr().out

from gateway.work_ledger_budget import (
    compact_json_size,
    enforce_ledger_budget,
    is_quiescent_terminal_item,
)


NOW = 10_000_000.0


def _item(work_id: str, *, status: str = "completed", age: float = 172_800, **extra):
    return {
        "id": work_id,
        "status": status,
        "platform": "discord",
        "message_id": work_id,
        "created_at": NOW - age - 60,
        "updated_at": NOW - age,
        "text": "x" * 4_000,
        **extra,
    }


def test_budget_tombstones_old_quiescent_records_until_target():
    data = {
        "version": 2,
        "items": {f"work-{index}": _item(f"work-{index}") for index in range(8)},
    }
    before = compact_json_size(data)

    result = enforce_ledger_budget(
        data,
        now=NOW,
        target_bytes=8_000,
        hard_bytes=12_000,
        full_record_seconds=86_400,
    )

    assert result.before_bytes == before
    assert result.after_bytes <= 8_000
    assert result.tombstoned > 0
    assert all(
        not item.get("tombstone") or "text" not in item
        for item in data["items"].values()
    )


def test_budget_leaves_old_records_rich_while_below_target():
    data = {"version": 2, "items": {"work": _item("work")}}

    result = enforce_ledger_budget(
        data,
        now=NOW,
        target_bytes=100_000,
        hard_bytes=120_000,
        full_record_seconds=1,
    )

    assert result.tombstoned == 0
    assert "text" in data["items"]["work"]


def test_budget_never_compacts_live_blocked_or_pending_records():
    data = {
        "version": 2,
        "items": {
            "active": _item("active", status="agent_running"),
            "blocked": _item("blocked", status="blocked"),
            "leased": _item("leased", lease_until=NOW + 60),
            "reaction": _item("reaction", discord_thread_reaction_sync_pending=True),
            "closeout": _item("closeout", closeout={"status": "pending"}),
            "delivery": _item(
                "delivery",
                terminal_delivery={"status": "completed", "summary_updated_at": None},
            ),
        },
    }

    result = enforce_ledger_budget(
        data,
        now=NOW,
        target_bytes=1,
        hard_bytes=2,
        full_record_seconds=1,
        emergency_record_seconds=1,
    )

    assert result.tombstoned == 0
    assert result.over_hard_budget is True
    assert all("text" in item for item in data["items"].values())


def test_hard_budget_compacts_recent_but_recoverably_old_terminal_records():
    data = {
        "version": 2,
        "items": {
            "recent": _item("recent", age=3 * 60 * 60),
            "fresh": _item("fresh", age=30 * 60),
        },
    }

    result = enforce_ledger_budget(
        data,
        now=NOW,
        target_bytes=1_000,
        hard_bytes=2_000,
        full_record_seconds=7 * 24 * 60 * 60,
        emergency_record_seconds=2 * 60 * 60,
    )

    assert result.tombstoned == 1
    assert data["items"]["recent"]["tombstone"] is True
    assert data["items"]["fresh"].get("tombstone") is not True


def test_expired_tombstones_are_removed():
    data = {
        "version": 2,
        "items": {
            "old": {
                "id": "old",
                "status": "completed",
                "terminal_at": NOW - 31 * 24 * 60 * 60,
                "tombstone": True,
            },
            "new": {
                "id": "new",
                "status": "completed",
                "terminal_at": NOW - 2 * 24 * 60 * 60,
                "tombstone": True,
            },
        },
    }

    result = enforce_ledger_budget(data, now=NOW)

    assert result.expired_tombstones == 1
    assert set(data["items"]) == {"new"}


def test_quiescent_check_accepts_completed_closeout_and_delivery():
    item = _item(
        "done",
        closeout={"status": "completed"},
        terminal_delivery={"status": "completed", "summary_updated_at": NOW - 5},
    )

    assert is_quiescent_terminal_item(item, now=NOW) is True

from gateway.work_ledger_budget import compact_json_size, enforce_ledger_budget


NOW = 10_000_000.0


def _item(work_id: str, *, status: str = "completed", age: float = 172_800):
    return {
        "id": work_id,
        "status": status,
        "created_at": NOW - age - 60,
        "updated_at": NOW - age,
        "text": "x" * 4_000,
    }


def _is_quiescent(item):
    return item.get("status") == "completed"


def _tombstone(item):
    return {"id": item["id"], "status": item["status"], "tombstone": True}


def test_budget_tombstones_old_quiescent_records_until_target():
    data = {
        "version": 2,
        "items": {f"work-{index}": _item(f"work-{index}") for index in range(8)},
    }
    before = compact_json_size(data)

    result = enforce_ledger_budget(
        data,
        now=NOW,
        is_quiescent=_is_quiescent,
        make_tombstone=_tombstone,
        target_bytes=8_000,
        hard_bytes=12_000,
    )

    assert result.before_bytes == before
    assert result.after_bytes <= 8_000
    assert result.tombstoned > 0
    assert all(
        not item.get("tombstone") or "text" not in item
        for item in data["items"].values()
    )


def test_budget_does_nothing_below_hard_limit():
    data = {"version": 2, "items": {"work": _item("work")}}

    result = enforce_ledger_budget(
        data,
        now=NOW,
        is_quiescent=_is_quiescent,
        make_tombstone=_tombstone,
        target_bytes=1_000,
        hard_bytes=10_000,
    )

    assert result.tombstoned == 0
    assert "text" in data["items"]["work"]


def test_budget_never_compacts_nonquiescent_records():
    data = {
        "version": 2,
        "items": {
            "active": _item("active", status="agent_running"),
            "blocked": _item("blocked", status="blocked"),
        },
    }

    result = enforce_ledger_budget(
        data,
        now=NOW,
        is_quiescent=_is_quiescent,
        make_tombstone=_tombstone,
        target_bytes=1,
        hard_bytes=2,
    )

    assert result.tombstoned == 0
    assert result.over_hard_budget is True


def test_budget_preserves_records_inside_recovery_freshness_window():
    data = {
        "version": 2,
        "items": {
            "recoverably-old": _item("recoverably-old", age=3 * 60 * 60),
            "fresh": _item("fresh", age=30 * 60),
        },
    }

    result = enforce_ledger_budget(
        data,
        now=NOW,
        is_quiescent=_is_quiescent,
        make_tombstone=_tombstone,
        target_bytes=1_000,
        hard_bytes=2_000,
        emergency_record_seconds=2 * 60 * 60,
    )

    assert result.tombstoned == 1
    assert data["items"]["recoverably-old"]["tombstone"] is True
    assert data["items"]["fresh"].get("tombstone") is not True

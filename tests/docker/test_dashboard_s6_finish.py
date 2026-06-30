from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FINISH = ROOT / "docker" / "s6-rc.d" / "dashboard" / "finish"


def _run_finish(exit_code: str, *, enabled: bool) -> subprocess.CompletedProcess[str]:
    env = {"HERMES_DASHBOARD": "1" if enabled else ""}
    return subprocess.run(
        ["sh", str(FINISH), exit_code, "0", "dashboard", "0"],
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=False,
    )


def test_dashboard_s6_finish_bounds_known_port_collision_restart():
    result = _run_finish("98", enabled=True)

    assert result.returncode == 125


def test_dashboard_s6_finish_keeps_other_enabled_crashes_restartable():
    result = _run_finish("1", enabled=True)

    assert result.returncode == 0


def test_dashboard_s6_finish_disabled_dashboard_remains_permanent_down():
    result = _run_finish("0", enabled=False)

    assert result.returncode == 125

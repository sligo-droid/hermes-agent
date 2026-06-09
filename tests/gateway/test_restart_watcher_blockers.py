from gateway.restart import RestartProcessInfo, restart_blocker_evidence


def test_restart_blocker_evidence_ignores_watcher_process_group():
    children = [
        RestartProcessInfo(
            pid=2001,
            ppid=1000,
            pgid=777,
            sid=777,
            cmdline="bash -lc while kill -0 1000; do sleep 0.2; done; hermes gateway restart",
        ),
    ]

    evidence = restart_blocker_evidence(
        1000,
        runtime_state={"active_agents": 0},
        direct_children=children,
        self_pid=2001,
    )

    assert evidence.active_agents == 0
    assert evidence.blockers == ()


def test_restart_blocker_evidence_preserves_real_direct_child_blocker():
    children = [
        RestartProcessInfo(
            pid=2001,
            ppid=1000,
            pgid=777,
            sid=777,
            cmdline="bash -lc while kill -0 1000; do sleep 0.2; done; hermes gateway restart",
        ),
        RestartProcessInfo(
            pid=3001,
            ppid=1000,
            pgid=3001,
            sid=3001,
            cmdline="python run_agent.py --session direct-sensitive",
        ),
    ]

    evidence = restart_blocker_evidence(
        1000,
        runtime_state={"active_agents": 1},
        direct_children=children,
        self_pid=2001,
    )

    assert evidence.active_agents == 1
    assert [proc.pid for proc in evidence.blockers] == [3001]
    assert "pid=3001 cmd=python run_agent.py --session direct-sensitive" == evidence.blocker_summary()

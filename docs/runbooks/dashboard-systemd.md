# Dashboard Systemd Service

Hermes does not currently generate or install a `hermes-dashboard.service` unit. The dashboard repo-owned startup boundary is `hermes dashboard`, which exits with status `98` when its configured bind address is already occupied by a known port collision.

If you run the dashboard under a locally managed user systemd service with `Restart=always`, bound known collision exits with a user drop-in:

```ini
# ~/.config/systemd/user/hermes-dashboard.service.d/10-port-collision.conf
[Service]
RestartPreventExitStatus=98
```

Then reload and restart after resolving any active collision:

```bash
systemctl --user daemon-reload
systemctl --user reset-failed hermes-dashboard.service
systemctl --user restart hermes-dashboard.service
```

Runtime evidence from the Discord Kanban R3 investigation found the active source of truth external to this repository at `/home/droid/.config/systemd/user/hermes-dashboard.service`. That unit had `ExecStart=/home/droid/hermes/.venv/bin/python -m hermes_cli.main dashboard --host 127.0.0.1 --port 9119 --no-open`, `Restart=always`, and no `RestartPreventExitStatus`, so the repo cannot update it directly. The operator-owned drop-in above is the bounded systemd mechanism for this externally managed service.

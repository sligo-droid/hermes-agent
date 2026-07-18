"""Run one externally fired cron job in the selected Hermes profile process."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument("fire_at")
    args = parser.parse_args(argv)

    from hermes_cli.env_loader import load_hermes_dotenv

    load_hermes_dotenv()

    from cron.scheduler_provider import resolve_cron_scheduler

    ran = resolve_cron_scheduler().fire_due(
        args.job_id,
        fire_at=args.fire_at,
        adapters=None,
        loop=None,
    )
    return 0 if ran else 1


if __name__ == "__main__":
    raise SystemExit(main())

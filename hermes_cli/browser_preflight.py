"""Safe browser availability preflights for worker and cron scripts."""

from __future__ import annotations

import argparse

from tools.browser_tool import check_playwright_chromium_preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hermes_cli.browser_preflight",
        description="Check local browser prerequisites without installing anything.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="chromium",
        choices=("chromium",),
        help="Preflight target to check (default: chromium).",
    )
    parser.parse_args(argv)

    ok, message = check_playwright_chromium_preflight()
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

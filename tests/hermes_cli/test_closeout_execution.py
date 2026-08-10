from __future__ import annotations

import pytest

from hermes_cli.closeout_execution import (
    CommandEffect,
    UnsupportedCloseoutCommand,
    classify_closeout_command,
)


def test_vercel_inspect_is_a_bounded_read_only_closeout_command():
    classified = classify_closeout_command(
        [
            "vercel",
            "inspect",
            "https://pid-a1b2c3-sligo-labs.vercel.app",
            "--json",
        ]
    )

    assert classified.effect == CommandEffect.READ_ONLY
    assert classified.operation == "vercel_inspect"


@pytest.mark.parametrize(
    "args",
    [
        ["vercel", "deploy", "--prod"],
        ["vercel", "inspect", "https://example.com", "--json"],
        [
            "vercel",
            "inspect",
            "https://pid-a1b2c3-sligo-labs.vercel.app",
            "--json",
            "--token",
            "secret",
        ],
    ],
)
def test_vercel_closeout_rejects_mutations_and_unbounded_inspection(args):
    with pytest.raises(UnsupportedCloseoutCommand):
        classify_closeout_command(args)

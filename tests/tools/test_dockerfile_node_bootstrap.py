"""Contract tests for Node/npm bootstrap ordering in the Docker image."""

from pathlib import Path


DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def test_pinned_npm_install_runs_after_node_and_npm_are_available() -> None:
    """The runtime stage cannot invoke npm before copying it from node_source."""
    lines = [
        line.strip()
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    node_copy = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("COPY")
        and "--from=node_source" in line
        and "/usr/local/bin/node" in line
    )
    npm_copy = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("COPY")
        and "--from=node_source" in line
        and "/usr/local/lib/node_modules/npm" in line
    )
    npm_link = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("RUN ") and "npm-cli.js /usr/local/bin/npm" in line
    )
    pinned_install = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("RUN npm install -g npm@")
    )

    assert max(node_copy, npm_copy, npm_link) < pinned_install

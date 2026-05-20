from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_repo_docker_compose_is_inert() -> None:
    compose_path = ROOT / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}

    assert compose.get("services") == {}


def test_repo_docker_compose_does_not_define_hermes_containers() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "container_name: hermes" not in text
    assert "container_name: hermes-dashboard" not in text
    assert "~/.hermes:/opt/data" not in text

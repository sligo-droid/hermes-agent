from types import SimpleNamespace


class _Provider:
    def get_config_schema(self):
        return []


def test_available_providers_prioritizes_honcho_and_hides_hindsight(monkeypatch):
    import hermes_cli.memory_setup as memory_setup

    providers = [
        ("mem0", "", True),
        ("hindsight", "", True),
        ("honcho", "", True),
        ("byterover", "", True),
    ]
    monkeypatch.setattr(
        "plugins.memory.discover_memory_providers",
        lambda: providers,
    )
    monkeypatch.setattr(
        "plugins.memory.load_memory_provider",
        lambda name: _Provider(),
    )

    names = [name for name, _, _ in memory_setup._get_available_providers()]

    assert names[0] == "honcho"
    assert "hindsight" not in names


def test_memory_status_lists_honcho_first(monkeypatch, capsys):
    import hermes_cli.memory_setup as memory_setup

    monkeypatch.setattr(
        memory_setup,
        "_get_available_providers",
        lambda: [
            ("honcho", "API key / local", _Provider()),
            ("mem0", "requires API key", _Provider()),
        ],
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": ""}},
    )

    memory_setup.cmd_status(SimpleNamespace())

    out = capsys.readouterr().out
    assert out.index("honcho") < out.index("mem0")

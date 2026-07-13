from types import SimpleNamespace


class _StubCLI:
    provider = "openrouter"
    model = "old-model"
    base_url = ""
    api_key = ""


def test_classic_model_handler_consumes_session_flag_contract(monkeypatch):
    import cli as cli_mod
    import hermes_cli.inventory as inventory
    import hermes_cli.model_switch as model_switch

    seen = {}
    context = SimpleNamespace(
        user_providers={},
        custom_providers={},
        with_overrides=lambda **_kwargs: context,
    )

    def fake_switch_model(**kwargs):
        seen.update(kwargs)
        return model_switch.ModelSwitchResult(success=False, error_message="stop")

    monkeypatch.setattr(inventory, "load_picker_context", lambda: context)
    monkeypatch.setattr(model_switch, "switch_model", fake_switch_model)
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_args, **_kwargs: None)

    cli_mod.HermesCLI._handle_model_switch(_StubCLI(), "/model new-model --session")

    assert seen["raw_input"] == "new-model"
    assert seen["is_global"] is False

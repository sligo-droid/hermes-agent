from __future__ import annotations

from types import SimpleNamespace


def test_feature_summary_embed_renders_time_spent(monkeypatch):
    from gateway.config import Platform, PlatformConfig
    from plugins.platforms.discord import adapter as discord_adapter

    class FakeEmbed:
        def __init__(self, **kwargs):
            self.title = kwargs.get("title")
            self.fields = []

        def add_field(self, *, name, value, inline):
            self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))

        def to_dict(self):
            return {
                "title": self.title,
                "fields": [vars(field) for field in self.fields],
            }

    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter, "discord", SimpleNamespace(Embed=FakeEmbed))

    adapter = discord_adapter.DiscordAdapter(PlatformConfig(enabled=True))
    embed = adapter._build_feature_summary_embed(
        initial_request="Ship it",
        status="Complete",
        outcome="Done",
        title="Feature shipped",
        runtime_breakdown={
            "wall_s": 42,
            "model_s": 26,
            "tools_s": 11,
            "overhead_s": 5,
            "top_tools": [{"name": "terminal", "duration_s": 8}],
        },
    )

    fields = {field.name: field.value for field in embed.fields}
    assert "Time Spent" in fields
    assert fields["Time Spent"].startswith("42s wall · model 26s")
    assert len(fields["Time Spent"]) <= 1024

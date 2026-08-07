from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.client_knowledge_gbrain.honcho_projection import (
    HonchoProjectionApi,
    HonchoProjectionFailure,
    promotable_page,
)


class _Scope:
    def __init__(self, values=None):
        self.values = list(values or [])
        self.created = []
        self.deleted = []

    def list(self, size=500):
        return SimpleNamespace(items=list(self.values[:size]))

    def create(self, payload):
        self.created.append(payload)
        value = SimpleNamespace(id="new-id", content=payload[0]["content"])
        self.values.append(value)
        return [value]

    def delete(self, conclusion_id):
        self.deleted.append(conclusion_id)
        self.values = [value for value in self.values if str(value.id) != str(conclusion_id)]


def test_honcho_create_returns_and_adopt_reuses_exact_id():
    scope = _Scope()
    api = HonchoProjectionApi(scope)
    content = "[PID] Stable claim. Source: gbrain:projects/pid/facts/x [ckp:marker]"
    assert api.adopt("ckp:marker", content) == ""
    assert api.create(content) == "new-id"
    assert api.adopt("ckp:marker", content) == "new-id"
    assert len(scope.created) == 1


def test_honcho_duplicate_marker_fails_closed_and_retracts_by_id():
    content = "[PID] Stable [ckp:marker]"
    api = HonchoProjectionApi(_Scope([
        SimpleNamespace(id="one", content=content), SimpleNamespace(id="two", content=content),
    ]))
    with pytest.raises(HonchoProjectionFailure, match="duplicate_remote_marker"):
        api.adopt("ckp:marker", content)
    scope = _Scope()
    api = HonchoProjectionApi(scope)
    api.delete("old-id")
    assert scope.deleted == ["old-id"]


def test_honcho_marker_first_resolution_replaces_stale_and_detects_truncation():
    stale = SimpleNamespace(id="old", content="stale [ckp:marker]")
    api = HonchoProjectionApi(_Scope([stale]))
    assert api.resolve_marker("ckp:marker", "fresh [ckp:marker]") == ("", "old")
    with pytest.raises(HonchoProjectionFailure, match="remote_set_truncated"):
        HonchoProjectionApi(
            _Scope([SimpleNamespace(id="one", content="x")]),
            max_items=1,
        ).list()


def test_only_stable_current_non_sensitive_allowlisted_pages_promote():
    base = {"frontmatter": {
        "status": "current", "confidence": "high", "sensitivity": "internal",
        "impact": "ordinary", "honcho_projection": "eligible",
        "kind": "fact", "source_refs": ["notion:page:x"],
    }}
    assert promotable_page(base)
    for key, value in (
        ("status", "disputed"), ("status", "tentative"),
        ("confidence", "medium"), ("sensitivity", "confidential"),
        ("impact", "high"), ("honcho_projection", "ineligible"), ("kind", "risk"),
    ):
        page = {"frontmatter": dict(base["frontmatter"])}
        page["frontmatter"][key] = value
        assert not promotable_page(page)

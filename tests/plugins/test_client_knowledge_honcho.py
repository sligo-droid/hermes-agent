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

    def list(self, page=1, size=100):
        values = self.values

        class Page:
            def __init__(self, number):
                self.page = number
                self.total = len(values)
                self.items = list(values[(number - 1) * size:number * size])

            def get_next_page(self):
                return Page(self.page + 1) if self.page * size < self.total else None

        return Page(page)

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


def test_honcho_marker_first_resolution_replaces_stale_and_paginates_with_api_limit():
    stale = SimpleNamespace(id="old", content="stale [ckp:marker]")
    api = HonchoProjectionApi(_Scope([stale]))
    assert api.resolve_marker("ckp:marker", "fresh [ckp:marker]") == ("", "old")
    values = [SimpleNamespace(id=str(index), content=f"value {index}") for index in range(101)]
    assert len(HonchoProjectionApi(_Scope(values), max_items=101).list()) == 101
    with pytest.raises(HonchoProjectionFailure, match="remote_set_truncated"):
        HonchoProjectionApi(_Scope(values), max_items=100).list()


def test_honcho_later_page_failure_is_wrapped():
    class Page:
        total = 2
        items = [SimpleNamespace(id="one", content="one")]

        @staticmethod
        def get_next_page():
            raise ConnectionError("later page failed")

    class Scope:
        @staticmethod
        def list(**_kwargs):
            return Page()

    with pytest.raises(HonchoProjectionFailure, match="projection_list_failed"):
        HonchoProjectionApi(Scope()).list()


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

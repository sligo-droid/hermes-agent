from types import SimpleNamespace

from tools.mcp_tool import _extract_mcp_embedded_resource_text_block


def test_extract_mcp_embedded_text_resource():
    block = SimpleNamespace(resource=SimpleNamespace(text="hello from resource"))

    assert _extract_mcp_embedded_resource_text_block(block) == "hello from resource"


def test_extract_mcp_embedded_resource_ignores_missing_text():
    block = SimpleNamespace(resource=SimpleNamespace(blob=b"not text"))

    assert _extract_mcp_embedded_resource_text_block(block) == ""

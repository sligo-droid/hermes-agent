"""Tests for MCP dynamic tool discovery (notifications/tools/list_changed)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.mcp_tool import MCPServerTask, _register_server_tools
from tools.registry import ToolRegistry
from agent.runtime_capabilities import ToolEffect


def _make_mcp_tool(name: str, desc: str = ""):
    return SimpleNamespace(name=name, description=desc, inputSchema=None)


@pytest.fixture(autouse=True)
def _restore_mcp_tool_provenance():
    """Keep dynamic registration tests from enabling live-agent refresh hooks."""

    from tools import mcp_tool

    with mcp_tool._lock:
        original = dict(mcp_tool._mcp_tool_server_names)
    yield
    with mcp_tool._lock:
        mcp_tool._mcp_tool_server_names.clear()
        mcp_tool._mcp_tool_server_names.update(original)


class TestRegisterServerTools:
    """Tests for the extracted _register_server_tools helper."""

    @pytest.fixture
    def mock_registry(self):
        return ToolRegistry()

    def test_exposes_live_server_aliases(self, mock_registry):
        """Registered MCP tools are reachable via live raw-server aliases."""
        server = MCPServerTask("my_srv")
        server._tools = [_make_mcp_tool("my_tool", "desc")]
        server.session = MagicMock()
        from toolsets import resolve_toolset, validate_toolset

        with patch("tools.registry.registry", mock_registry):
            registered = _register_server_tools("my_srv", server, {})
            assert "mcp__my_srv__my_tool" in registered
            assert "mcp__my_srv__my_tool" in mock_registry.get_all_tool_names()
            assert validate_toolset("my_srv") is True
            assert "mcp__my_srv__my_tool" in resolve_toolset("my_srv")

    def test_arbitrary_mcp_tools_fail_closed_without_operator_read_only_allowlist(
        self,
        mock_registry,
    ):
        server = MCPServerTask("my_srv")
        server._tools = [_make_mcp_tool("inspect"), _make_mcp_tool("mutate")]
        server.session = MagicMock()

        with patch("tools.registry.registry", mock_registry):
            _register_server_tools(
                "my_srv",
                server,
                {"read_only_tools": ["inspect"]},
            )

        assert mock_registry.get_entry("mcp__my_srv__inspect").effect is ToolEffect.READ_ONLY
        assert mock_registry.get_entry("mcp__my_srv__mutate").effect is ToolEffect.UNKNOWN
        assert mock_registry.read_only_block("mcp__my_srv__inspect", {}) is None
        assert "not been proven read-only" in mock_registry.read_only_block(
            "mcp__my_srv__mutate",
            {},
        )

    def test_runtime_modes_are_independent_from_read_only_effect(self, mock_registry):
        server = MCPServerTask("observer")
        server._tools = [
            _make_mcp_tool("inspect"),
            _make_mcp_tool("apply_migration"),
        ]
        server.session = MagicMock()

        with patch("tools.registry.registry", mock_registry):
            registered = _register_server_tools(
                "observer",
                server,
                {
                    "runtime_modes": ["read_only"],
                    "tools": {
                        "include": ["inspect"],
                        "resources": False,
                        "prompts": False,
                    },
                    "read_only_tools": ["inspect"],
                },
            )

        assert registered == ["mcp__observer__inspect"]
        entry = mock_registry.get_entry("mcp__observer__inspect")
        assert entry.effect is ToolEffect.READ_ONLY
        assert entry.runtime_modes == frozenset({"read_only"})
        assert mock_registry.is_exposable_in_runtime(
            "mcp__observer__inspect", "read_only"
        )
        assert not mock_registry.is_exposable_in_runtime(
            "mcp__observer__inspect", "action"
        )
        assert mock_registry.get_entry("mcp__observer__apply_migration") is None

    def test_missing_runtime_modes_preserves_existing_exposure(self, mock_registry):
        server = MCPServerTask("legacy")
        server._tools = [_make_mcp_tool("inspect")]
        server.session = MagicMock()

        with patch("tools.registry.registry", mock_registry):
            _register_server_tools("legacy", server, {})

        entry = mock_registry.get_entry("mcp__legacy__inspect")
        assert entry.runtime_modes is None
        assert mock_registry.is_exposable_in_runtime(
            "mcp__legacy__inspect", "read_only"
        )
        assert mock_registry.is_exposable_in_runtime(
            "mcp__legacy__inspect", "action"
        )

    def test_invalid_runtime_modes_fail_closed(self, mock_registry, caplog):
        server = MCPServerTask("invalid")
        server._tools = [_make_mcp_tool("inspect")]
        server.session = MagicMock()

        with patch("tools.registry.registry", mock_registry):
            _register_server_tools(
                "invalid",
                server,
                {"runtime_modes": ["read_only", "admin"]},
            )

        entry = mock_registry.get_entry("mcp__invalid__inspect")
        assert entry.runtime_modes == frozenset()
        assert not mock_registry.is_exposable_in_runtime(
            "mcp__invalid__inspect", "read_only"
        )
        assert not mock_registry.is_exposable_in_runtime(
            "mcp__invalid__inspect", "action"
        )
        assert "unsupported runtime modes" in caplog.text

    def test_explicit_null_runtime_modes_fail_closed(self, mock_registry, caplog):
        server = MCPServerTask("invalid_null")
        server._tools = [_make_mcp_tool("inspect")]
        server.session = MagicMock()

        with patch("tools.registry.registry", mock_registry):
            _register_server_tools(
                "invalid_null",
                server,
                {"runtime_modes": None},
            )

        entry = mock_registry.get_entry("mcp__invalid_null__inspect")
        assert entry.runtime_modes == frozenset()
        assert not mock_registry.is_exposable_in_runtime(
            "mcp__invalid_null__inspect", "read_only"
        )
        assert not mock_registry.is_exposable_in_runtime(
            "mcp__invalid_null__inspect", "action"
        )
        assert "must not be null" in caplog.text

    def test_runtime_modes_apply_to_host_owned_utilities(self, mock_registry):
        server = MCPServerTask("resources")
        server._tools = []
        server.session = MagicMock()
        server.initialize_result = SimpleNamespace(
            capabilities=SimpleNamespace(
                resources=SimpleNamespace(),
                prompts=None,
            )
        )

        with patch("tools.registry.registry", mock_registry):
            registered = _register_server_tools(
                "resources",
                server,
                {"runtime_modes": ["action"]},
            )

        assert set(registered) == {
            "mcp__resources__list_resources",
            "mcp__resources__read_resource",
        }
        for name in registered:
            entry = mock_registry.get_entry(name)
            assert entry.effect is ToolEffect.READ_ONLY
            assert entry.runtime_modes == frozenset({"action"})


class TestRefreshTools:
    """Tests for MCPServerTask._refresh_tools nuke-and-repave cycle."""

    @pytest.fixture
    def mock_registry(self):
        return ToolRegistry()

    @pytest.mark.asyncio
    async def test_nuke_and_repave(self, mock_registry):
        """Old tools are removed and new tools registered on refresh."""
        server = MCPServerTask("live_srv")
        server._refresh_lock = asyncio.Lock()
        server._config = {}
        from toolsets import resolve_toolset

        # Seed initial state: one old tool registered
        mock_registry.register(
            name="mcp__live_srv__old_tool", toolset="mcp-live_srv", schema={},
            handler=lambda x: x, check_fn=lambda: True, is_async=False,
            description="", emoji="",
        )
        server._registered_tool_names = ["mcp__live_srv__old_tool"]

        # New tool list from server
        new_tool = _make_mcp_tool("new_tool", "new behavior")
        server.session = SimpleNamespace(
            list_tools=AsyncMock(
                return_value=SimpleNamespace(tools=[new_tool])
            )
        )

        with patch("tools.registry.registry", mock_registry):
            await server._refresh_tools()
            assert "mcp__live_srv__old_tool" not in mock_registry.get_all_tool_names()
            assert "mcp__live_srv__old_tool" not in resolve_toolset("live_srv")
            assert "mcp__live_srv__new_tool" in mock_registry.get_all_tool_names()
            assert "mcp__live_srv__new_tool" in resolve_toolset("live_srv")
            assert server._registered_tool_names == ["mcp__live_srv__new_tool"]


class TestMessageHandler:
    """Tests for MCPServerTask._make_message_handler dispatch."""

    @pytest.mark.asyncio
    async def test_dispatches_tool_list_changed(self):
        from tools.mcp_tool import _MCP_NOTIFICATION_TYPES
        if not _MCP_NOTIFICATION_TYPES:
            pytest.skip("MCP SDK ToolListChangedNotification not available")

        from mcp.types import ServerNotification, ToolListChangedNotification

        server = MCPServerTask("notif_srv")
        # Product now schedules the refresh as a background task (see
        # _schedule_tools_refresh in mcp_tool.py ~L918) rather than awaiting
        # it directly, to avoid wedging the stdio JSON-RPC stream. Patch at
        # the scheduler seam so we can still assert dispatch happened without
        # reaching into asyncio.create_task internals.
        with patch.object(MCPServerTask, "_schedule_tools_refresh") as mock_schedule:
            handler = server._make_message_handler()
            notification = ServerNotification(
                root=ToolListChangedNotification(method="notifications/tools/list_changed")
            )
            await handler(notification)
            mock_schedule.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_exceptions_and_other_messages(self):
        server = MCPServerTask("notif_srv")
        with patch.object(MCPServerTask, "_schedule_tools_refresh") as mock_schedule:
            handler = server._make_message_handler()
            # Exceptions should not trigger refresh
            await handler(RuntimeError("connection dead"))
            # Unknown message types should not trigger refresh
            await handler({"jsonrpc": "2.0", "result": "ok"})
            mock_schedule.assert_not_called()


class TestDeregister:
    """Tests for ToolRegistry.deregister."""

    def test_removes_tool(self):
        reg = ToolRegistry()
        reg.register(name="foo", toolset="ts1", schema={}, handler=lambda x: x)
        assert "foo" in reg.get_all_tool_names()
        reg.deregister("foo")
        assert "foo" not in reg.get_all_tool_names()


    def test_noop_for_unknown_tool(self):
        reg = ToolRegistry()
        reg.deregister("nonexistent")  # Should not raise

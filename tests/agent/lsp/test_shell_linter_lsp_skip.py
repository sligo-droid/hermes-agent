"""Regression coverage for LSP-aware per-file shell-linter suppression."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_fops():
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    return ShellFileOperations(LocalEnvironment())


@pytest.mark.parametrize("ext", [".ts", ".go", ".rs"])
def test_shell_linter_skipped_when_active_lsp_handles_file(ext, tmp_path):
    """Stronger project-aware LSP diagnostics replace these shell linters."""
    fops = _make_fops()
    src = tmp_path / f"bad{ext}"
    src.write_text("intentionally invalid content\n")

    def _exec_must_not_run(*args, **kwargs):  # pragma: no cover
        raise AssertionError("shell linter ran despite LSP claiming the file")

    with patch.object(fops, "_lsp_will_handle", return_value=True), \
         patch.object(fops, "_exec", side_effect=_exec_must_not_run), \
         patch.object(fops, "_has_command", return_value=True):
        result = fops._check_lint(str(src))

    assert result.skipped is True
    assert "LSP" in (result.message or "")


@pytest.mark.parametrize("ext", [".ts", ".go", ".rs"])
def test_shell_linter_runs_when_lsp_is_not_active_for_file(ext, tmp_path):
    """No capable active LSP preserves the pre-existing safety check."""
    fops = _make_fops()
    src = tmp_path / f"clean{ext}"
    src.write_text("// content\n")
    command_result = MagicMock(exit_code=0, stdout="")

    with patch.object(fops, "_lsp_will_handle", return_value=False), \
         patch.object(fops, "_exec", return_value=command_result) as exec_mock, \
         patch.object(fops, "_has_command", return_value=True):
        result = fops._check_lint(str(src))

    assert exec_mock.called
    assert result.success is True


@pytest.mark.parametrize("ext", [".py", ".js"])
def test_lsp_never_suppresses_python_or_javascript_safety_checks(ext, tmp_path):
    """The file-local Python and JavaScript syntax checks stay unconditional."""
    fops = _make_fops()
    src = tmp_path / f"clean{ext}"
    src.write_text("# valid\n" if ext == ".py" else "// valid\n")
    command_result = MagicMock(exit_code=0, stdout="")

    with patch.object(fops, "_lsp_will_handle", return_value=True), \
         patch.object(fops, "_exec", return_value=command_result) as exec_mock, \
         patch.object(fops, "_has_command", return_value=True):
        result = fops._check_lint(str(src))

    assert exec_mock.called
    assert result.success is True


def test_lsp_will_handle_requires_service(tmp_path):
    """An uninitialized LSP service must not suppress the shell fallback."""
    fops = _make_fops()
    src = tmp_path / "foo.ts"
    src.write_text("const x = 1\n")

    with patch.object(fops, "_lsp_local_only", return_value=True), \
         patch("agent.lsp.get_service", return_value=None):
        assert fops._lsp_will_handle(str(src)) is False


def test_lsp_will_handle_short_circuits_remote_backends(tmp_path):
    """Host-side LSP diagnostics cannot safely claim remote-backend files."""
    fops = _make_fops()
    src = tmp_path / "foo.ts"
    src.write_text("const x = 1\n")

    with patch.object(fops, "_lsp_local_only", return_value=False), \
         patch("agent.lsp.get_service") as get_service:
        assert fops._lsp_will_handle(str(src)) is False

    assert not get_service.called


def test_lsp_will_handle_falls_back_on_enabled_for_error(tmp_path):
    """A failing service probe retains the shell linter rather than masking it."""
    fops = _make_fops()
    src = tmp_path / "foo.ts"
    src.write_text("const x = 1\n")
    service = MagicMock()
    service.enabled_for.side_effect = RuntimeError("server crashed")

    with patch.object(fops, "_lsp_local_only", return_value=True), \
         patch("agent.lsp.get_service", return_value=service):
        assert fops._lsp_will_handle(str(src)) is False


def test_tsx_retains_existing_no_shell_linter_default(tmp_path):
    """Do not add a broken single-file tsc invocation for .tsx files."""
    from tools.file_operations import LINTERS, _SHELL_LINTER_LSP_REDUNDANT

    assert ".tsx" not in LINTERS
    assert ".tsx" not in _SHELL_LINTER_LSP_REDUNDANT

    fops = _make_fops()
    src = tmp_path / "foo.tsx"
    src.write_text("export const X = () => <div/>\n")
    with patch.object(fops, "_lsp_will_handle", return_value=True), \
         patch.object(fops, "_exec") as exec_mock:
        result = fops._check_lint(str(src))

    assert result.skipped is True
    assert not exec_mock.called

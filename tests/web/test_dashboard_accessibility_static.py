from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_issue_pulse_uses_native_button_keyboard_activation():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")

    assert "function IssuePulseChart" in source
    issue_pulse_source = source.split("function IssuePulseChart", 1)[1].split("function SourceBadge", 1)[0]
    assert "<button" in issue_pulse_source
    assert "type=\"button\"" in issue_pulse_source
    assert "aria-label" in issue_pulse_source
    assert "onClick={() => onSelect(point.status)}" in issue_pulse_source


def test_modal_behavior_traps_focus_and_restores_safely():
    source = (ROOT / "web/src/hooks/useModalBehavior.ts").read_text(encoding="utf-8")

    assert "const modalStack" in source
    assert "topModal()?.id !== id" in source
    assert "e.key !== \"Tab\"" in source
    assert "e.key === \"Escape\"" in source
    assert "requestAnimationFrame" in source
    assert "[data-autofocus]" in source
    assert "isFocusableElement(entry.restoreTarget)" in source


def test_first_party_custom_modals_use_focus_behavior():
    expected = {
        "web/src/pages/CronPage.tsx": "data-autofocus",
        "web/src/pages/ProfilesPage.tsx": "data-autofocus",
        "web/src/pages/ModelsPage.tsx": "useModalBehavior({ open: true, onClose })",
        "web/src/components/ModelPickerDialog.tsx": "useModalBehavior({ open: true, onClose })",
        "web/src/components/OAuthLoginModal.tsx": "useModalBehavior({ open: true, onClose: handleClose })",
    }

    for relative, marker in expected.items():
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "useModalBehavior" in source
        assert marker in source

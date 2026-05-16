from hermes_cli.terminal_notifications import (
    BEL,
    ESC,
    emit_completion_notification,
    osc777_notify,
    wrap_for_multiplexer,
)


class FakeStream:
    def __init__(self, is_tty=True):
        self.is_tty = is_tty
        self.value = ""
        self.flushed = False

    def isatty(self):
        return self.is_tty

    def write(self, data):
        self.value += data

    def flush(self):
        self.flushed = True


def test_osc777_notify_builds_completion_notification():
    assert osc777_notify() == f"{ESC}]777;notify;Hermes;Response complete{BEL}"


def test_osc777_notify_sanitizes_fields():
    assert osc777_notify("Her;mes\x07", "Done\x1b now") == f"{ESC}]777;notify;Her,mes;Done  now{BEL}"


def test_wrap_for_multiplexer_wraps_tmux():
    wrapped = wrap_for_multiplexer(f"{ESC}]777;notify;Hermes;Done{BEL}", {"TMUX": "/tmp/tmux"})

    assert wrapped.startswith(f"{ESC}Ptmux;")
    assert f"{ESC}{ESC}]777" in wrapped
    assert wrapped.endswith(f"{ESC}\\")


def test_emit_completion_notification_respects_disabled_flag():
    stream = FakeStream()

    assert emit_completion_notification(stream, enabled=False) is False
    assert stream.value == ""
    assert stream.flushed is False


def test_emit_completion_notification_skips_non_tty():
    stream = FakeStream(is_tty=False)

    assert emit_completion_notification(stream) is False
    assert stream.value == ""
    assert stream.flushed is False


def test_emit_completion_notification_writes_to_tty():
    stream = FakeStream()

    assert emit_completion_notification(stream, env={}) is True
    assert stream.value == osc777_notify()
    assert stream.flushed is True

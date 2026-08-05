import base64
import http.client
import threading

import pytest

from plugins.traces.hermes_traces_plugin.config import Config
from plugins.traces.hermes_traces_plugin.publisher import Publisher
from plugins.traces.hermes_traces_plugin.resolver import serve
from plugins.traces.hermes_traces_plugin.state import State


class FakePublisher:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, key):
        self.enqueued.append(key)


def request(server, path, auth=None, method="GET", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = dict(headers or {})
    if auth:
        token = base64.b64encode(auth.encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    output = {
        "status": response.status,
        "headers": dict(response.getheaders()),
        "body": body,
    }
    connection.close()
    return output


@pytest.fixture
def resolver(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TRACES_AUTH_USERNAME", "operator")
    monkeypatch.setenv("HERMES_TRACES_AUTH_PASSWORD", "secret")
    config = Config(tmp_path)
    state = State(config.index_path)
    publisher = FakePublisher()
    server = serve(
        port=0,
        config=config,
        state=state,
        publisher=publisher,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, state, publisher
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_resolver_requires_auth_and_sets_security_headers(resolver):
    server, _state, _publisher = resolver

    response = request(server, "/healthz")

    assert response["status"] == 401
    assert response["headers"]["WWW-Authenticate"] == 'Basic realm="Hermes Traces"'
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["Pragma"] == "no-cache"
    assert response["headers"]["Referrer-Policy"] == "no-referrer"
    assert response["headers"]["X-Robots-Tag"] == "noindex, nofollow"
    assert "default-src 'none'" in response["headers"]["Content-Security-Policy"]


def test_health_supports_get_and_head(resolver):
    server, _state, _publisher = resolver

    get_response = request(server, "/healthz", "operator:secret")
    head_response = request(
        server,
        "/healthz",
        "operator:secret",
        method="HEAD",
    )

    assert get_response["status"] == 200
    assert get_response["body"] == b"ok\n"
    assert head_response["status"] == 200
    assert head_response["body"] == b""
    assert head_response["headers"]["Content-Length"] == "3"


def test_pending_record_requeues_and_ready_record_redirects(resolver):
    server, state, publisher = resolver
    record = state.create("id")

    pending = request(server, "/traces/" + record["slug"], "operator:secret")
    state.update(
        record["key"],
        status="ready",
        shared_url="https://www.traces.com/x",
        visibility="private",
    )
    ready = request(server, "/traces/" + record["slug"], "operator:secret")

    assert pending["status"] == 200
    assert b"publication is still in progress" in pending["body"]
    assert publisher.enqueued == [record["key"]]
    assert ready["status"] == 303
    assert ready["headers"]["Location"] == "https://www.traces.com/x/full"
    assert ready["body"] == b""

    state.update(
        record["key"],
        status="ready",
        shared_url="https://www.traces.com/x/full",
        visibility="private",
    )
    already_full = request(server, "/traces/" + record["slug"], "operator:secret")
    assert already_full["headers"]["Location"] == "https://www.traces.com/x/full"


def test_unknown_malformed_and_query_paths_are_generic_404(resolver):
    server, state, _publisher = resolver
    record = state.create("id")

    for path in (
        "/traces/unknown-slug-value-123",
        "/traces/",
        "/traces/../healthz",
        "/traces/" + record["slug"] + "?target=https://evil.example",
        "/other",
    ):
        response = request(server, path, "operator:secret")
        assert response["status"] == 404
        assert response["body"] == b"Not found.\n"


def test_error_or_invalid_ready_destination_returns_generic_503(resolver):
    server, state, _publisher = resolver
    failed = state.create("failed")
    invalid = state.create("invalid")
    state.update(failed["key"], status="error", error="command_failed")
    state.update(
        invalid["key"],
        status="ready",
        shared_url="https://evil.example/x",
        visibility="private",
    )

    for record in (failed, invalid):
        response = request(
            server,
            "/traces/" + record["slug"],
            "operator:secret",
        )
        assert response["status"] == 503
        assert response["body"] == b"Trace temporarily unavailable.\n"
        assert b"command_failed" not in response["body"]
        assert b"evil.example" not in response["body"]


def test_dashboard_credentials_are_an_explicit_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TRACES_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("HERMES_TRACES_AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "dashboard")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "password")
    config = Config(tmp_path)
    state = State(config.index_path)
    publisher = FakePublisher()
    server = serve(port=0, config=config, state=state, publisher=publisher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(server, "/healthz", "dashboard:password")["status"] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_missing_or_partial_credentials_fail_closed(monkeypatch, tmp_path):
    for name in (
        "HERMES_TRACES_AUTH_USERNAME",
        "HERMES_TRACES_AUTH_PASSWORD",
        "HERMES_DASHBOARD_USERNAME",
        "HERMES_DASHBOARD_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "dashboard")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "fallback-password")
    monkeypatch.setenv("HERMES_TRACES_AUTH_USERNAME", "operator")
    config = Config(tmp_path)
    state = State(config.index_path)
    server = serve(
        port=0,
        config=config,
        state=state,
        publisher=FakePublisher(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(server, "/healthz", "operator:anything")["status"] == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cloudflare_access_mode_requires_edge_identity_without_basic_challenge(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_TRACES_AUTH_MODE", "cloudflare-access")
    config = Config(tmp_path)
    state = State(config.index_path)
    server = serve(
        port=0,
        config=config,
        state=state,
        publisher=FakePublisher(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        denied = request(server, "/healthz", "operator:secret")
        allowed = request(
            server,
            "/healthz",
            headers={
                "Cf-Access-Jwt-Assertion": "signed-edge-assertion",
                "Cf-Access-Authenticated-User-Email": "operator@example.test",
            },
        )

        assert denied["status"] == 403
        assert "WWW-Authenticate" not in denied["headers"]
        assert allowed["status"] == 200
        assert allowed["body"] == b"ok\n"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cloudflare_access_mode_requires_assertion_and_email(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TRACES_AUTH_MODE", "cloudflare_access")
    server = serve(
        port=0,
        config=Config(tmp_path),
        publisher=FakePublisher(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for headers in (
            {"Cf-Access-Jwt-Assertion": "signed-edge-assertion"},
            {"Cf-Access-Authenticated-User-Email": "operator@example.test"},
        ):
            assert request(server, "/healthz", headers=headers)["status"] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_unsupported_auth_mode_fails_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TRACES_AUTH_MODE", "unknown")

    with pytest.raises(ValueError, match="unsupported HERMES_TRACES_AUTH_MODE"):
        serve(port=0, config=Config(tmp_path), publisher=FakePublisher())


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "localhost"])
def test_resolver_rejects_non_loopback_or_hostname(tmp_path, host):
    with pytest.raises(ValueError, match="loopback"):
        serve(host=host, config=Config(tmp_path), publisher=FakePublisher())

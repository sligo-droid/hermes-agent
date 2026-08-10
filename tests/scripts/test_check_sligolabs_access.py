from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

from scripts import check_sligolabs_access as checker


HUMAN_POLICY = {
    "id": "policy-id-must-not-print",
    "name": "operators",
    "decision": "allow",
    "precedence": 1,
    "include": [{"email": {"email": "private-person@example.test"}}],
    "require": [{"login_method": {"id": "identity-provider-id-must-not-print"}}],
}


def _human_app(domain: str, *, policy=None) -> dict:
    return {
        "id": f"application-id-for-{domain}",
        "account_id": "account-id-must-not-print",
        "name": domain,
        "domain": domain,
        "session_duration": "24h",
        "allowed_idps": ["identity-provider-id-must-not-print"],
        "policies": [copy.deepcopy(policy or HUMAN_POLICY)],
    }


def _bypass_app(domain: str) -> dict:
    return {
        "id": f"application-id-for-{domain}",
        "domain": domain,
        "policies": [
            {
                "id": "bypass-policy-id-must-not-print",
                "decision": "bypass",
                "include": [{"everyone": {}}],
            }
        ],
    }


def _applications() -> list[dict]:
    return [
        _human_app("*.sligolabs.com"),
        _human_app("claw.sligolabs.com"),
        _bypass_app("sligo.sligolabs.com/webhooks/*"),
        _bypass_app("hermes.sligolabs.com/api/status"),
        _bypass_app("hermes.sligolabs.com/api/cron/fire"),
        _human_app("sligo.sligolabs.com/traces*"),
    ]


def _tunnel() -> dict:
    return {
        "tunnel": "tunnel-id-must-not-print",
        "credentials-file": "/secret/credentials-file-must-not-print.json",
        "ingress": [
            {
                "hostname": "sligo.sligolabs.com",
                "path": "/webhooks/*",
                "service": "http://127.0.0.1:8644",
            },
            {
                "hostname": "sligo.sligolabs.com",
                "path": "^/traces(?:/.*)?$",
                "service": "http://127.0.0.1:8788",
            },
            {"hostname": "sligo.sligolabs.com", "service": "http://127.0.0.1:9119"},
            {"hostname": "claw.sligolabs.com", "service": "http://127.0.0.1:8720"},
            {"service": "http_status:404"},
        ],
    }


def _write_inputs(tmp_path: Path, apps, *, wrapper=False, tunnel=None):
    access_path = tmp_path / "access.json"
    tunnel_path = tmp_path / "tunnel.yml"
    payload = {"result": apps} if wrapper else apps
    access_path.write_text(json.dumps(payload), encoding="utf-8")
    tunnel_path.write_text(yaml.safe_dump(tunnel or _tunnel(), sort_keys=False), encoding="utf-8")
    return access_path, tunnel_path


def _run(tmp_path: Path, apps, capsys, *, wrapper=False, tunnel=None, extra=None):
    access_path, tunnel_path = _write_inputs(
        tmp_path,
        apps,
        wrapper=wrapper,
        tunnel=tunnel,
    )
    argv = ["--access-export", str(access_path), "--tunnel-config", str(tunnel_path)]
    if extra:
        argv.extend(extra)
    result = checker.main(argv)
    output = capsys.readouterr()
    return result, output.out, output.err


def test_conforming_topology_succeeds_and_reports_remote_tunnel_unverified(tmp_path, capsys):
    result, stdout, stderr = _run(tmp_path, _applications(), capsys)

    assert result == 0
    assert stderr == ""
    assert "RESULT conforming" in stdout
    assert "remote_hermes] unverified" in stdout


def test_cloudflare_result_wrapper_is_accepted(tmp_path, capsys):
    result, stdout, _stderr = _run(tmp_path, _applications(), capsys, wrapper=True)

    assert result == 0
    assert "RESULT conforming" in stdout


def test_cloudflare_policy_uids_are_ignored_as_metadata(tmp_path, capsys):
    apps = _applications()
    for index, app in enumerate(apps):
        for policy in app["policies"]:
            policy["uid"] = f"cloudflare-generated-policy-uid-{index}"

    result, stdout, _stderr = _run(tmp_path, apps, capsys)

    assert result == 0
    assert "RESULT conforming" in stdout


def test_destination_only_access_export_is_accepted(tmp_path, capsys):
    apps = _applications()
    for app in apps:
        domain = app.pop("domain")
        app["destinations"] = [{"type": "public", "uri": domain}]

    result, stdout, _stderr = _run(tmp_path, apps, capsys)

    assert result == 0
    assert "RESULT conforming" in stdout


def test_destinations_override_stale_legacy_domain(tmp_path, capsys):
    apps = _applications()
    wildcard = next(app for app in apps if app["domain"] == "*.sligolabs.com")
    wildcard["destinations"] = [
        {"type": "public", "uri": "wrong.example.test"}
    ]

    result, stdout, _stderr = _run(tmp_path, apps, capsys)

    assert result == 1
    assert "required application missing: *.sligolabs.com" in stdout


def test_missing_machine_exception_is_drift(tmp_path, capsys):
    apps = [
        app
        for app in _applications()
        if app["domain"] != "sligo.sligolabs.com/webhooks/*"
    ]

    result, stdout, _stderr = _run(tmp_path, apps, capsys)

    assert result == 1
    assert "access.missing" in stdout
    assert "sligo.sligolabs.com/webhooks/*" in stdout


def test_bypass_with_human_selector_is_drift(tmp_path, capsys):
    apps = _applications()
    cron = next(app for app in apps if app["domain"].endswith("/api/cron/fire"))
    cron["policies"][0]["include"] = [
        {"email": {"email": "operator@example.test"}}
    ]

    result, stdout, _stderr = _run(tmp_path, apps, capsys)

    assert result == 1
    assert "access.bypass_selector" in stdout


def test_wrong_machine_action_is_drift(tmp_path, capsys):
    apps = _applications()
    webhook = next(app for app in apps if app["domain"].endswith("/webhooks/*"))
    webhook["policies"][0]["decision"] = "allow"

    result, stdout, _stderr = _run(tmp_path, apps, capsys)

    assert result == 1
    assert "access.action" in stdout
    assert "expected bypass" in stdout


def test_missing_wildcard_is_drift(tmp_path, capsys):
    apps = [app for app in _applications() if app["domain"] != "*.sligolabs.com"]

    result, stdout, _stderr = _run(tmp_path, apps, capsys)

    assert result == 1
    assert "required application missing: *.sligolabs.com" in stdout


def test_unapproved_exact_override_and_dev_namespace_are_drift(tmp_path, capsys):
    apps = _applications()
    apps.append(_human_app("pid.sligolabs.com"))
    apps.append(_bypass_app("other.sligolabs.com"))
    apps.append(_human_app("*.dev.sligolabs.com"))

    result, stdout, _stderr = _run(tmp_path, apps, capsys)

    assert result == 1
    assert "access.overlap" in stdout
    assert "access.excluded" in stdout


def test_local_tunnel_route_order_is_enforced(tmp_path, capsys):
    tunnel = _tunnel()
    tunnel["ingress"][0], tunnel["ingress"][2] = tunnel["ingress"][2], tunnel["ingress"][0]

    result, stdout, _stderr = _run(tmp_path, _applications(), capsys, tunnel=tunnel)

    assert result == 1
    assert "tunnel.order" in stdout
    assert "local ingress rule 1" in stdout


def test_non_loopback_origin_on_expected_port_is_drift(tmp_path, capsys):
    tunnel = _tunnel()
    tunnel["ingress"][2]["service"] = "http://0.0.0.0:9119"

    result, stdout, _stderr = _run(tmp_path, _applications(), capsys, tunnel=tunnel)

    assert result == 1
    assert "tunnel.order" in stdout
    assert "local ingress rule 3" in stdout


def test_additional_deny_policy_is_drift(tmp_path, capsys):
    apps = _applications()
    wildcard = next(app for app in apps if app["domain"] == "*.sligolabs.com")
    wildcard["policies"].append(
        {
            "name": "deny-other-users",
            "decision": "deny",
            "precedence": 2,
            "include": [{"everyone": {}}],
        }
    )

    result, stdout, _stderr = _run(tmp_path, apps, capsys)

    assert result == 1
    assert "access.action" in stdout
    assert "access.policy" in stdout


def test_changed_policy_precedence_is_drift(tmp_path, capsys):
    apps = _applications()
    wildcard = next(app for app in apps if app["domain"] == "*.sligolabs.com")
    wildcard["policies"][0]["precedence"] = 2

    result, stdout, _stderr = _run(tmp_path, apps, capsys)

    assert result == 1
    assert "access.policy" in stdout


def test_missing_declared_policy_reference_is_drift():
    expected = checker.load_expected(checker.DEFAULT_EXPECTED)
    expected["access"]["wildcard_human"]["same_human_policy_as"] = (
        "reference.sligolabs.com"
    )

    findings = checker.validate_access(expected, _applications())

    assert any(finding.code == "access.policy_reference" for finding in findings)


def test_declared_policy_reference_is_honored():
    expected = checker.load_expected(checker.DEFAULT_EXPECTED)
    expected["access"]["wildcard_human"].pop("same_human_policy_as")
    traces = expected["access"]["path_applications"][-1]
    traces["same_human_policy_as"] = "*.sligolabs.com"

    apps = _applications()
    alternate = copy.deepcopy(HUMAN_POLICY)
    alternate["include"] = [{"email_domain": {"domain": "operators.example"}}]
    next(app for app in apps if app["domain"] == "*.sligolabs.com")["policies"] = [
        copy.deepcopy(alternate)
    ]
    next(app for app in apps if app["domain"].endswith("/traces*"))["policies"] = [
        copy.deepcopy(alternate)
    ]

    findings = checker.validate_access(expected, apps)

    assert not [finding for finding in findings if finding.code == "access.policy"]


def test_sanitized_remote_hermes_route_can_be_verified(tmp_path, capsys):
    remote_path = tmp_path / "remote.yaml"
    remote_path.write_text(
        yaml.safe_dump(
            {
                "ingress": [
                    {
                        "hostname": "hermes.sligolabs.com",
                        "service": "http://127.0.0.1:9119",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result, stdout, _stderr = _run(
        tmp_path,
        _applications(),
        capsys,
        extra=["--hermes-tunnel-export", str(remote_path)],
    )

    assert result == 0
    assert "sanitized route export conforms" in stdout
    assert "unverified" not in stdout


def test_invalid_access_input_returns_exit_two(tmp_path, capsys):
    access_path = tmp_path / "access.json"
    tunnel_path = tmp_path / "tunnel.yml"
    access_path.write_text('{"result": {"secret": "must-not-print"}}', encoding="utf-8")
    tunnel_path.write_text(yaml.safe_dump(_tunnel()), encoding="utf-8")

    result = checker.main(
        ["--access-export", str(access_path), "--tunnel-config", str(tunnel_path)]
    )
    output = capsys.readouterr()

    assert result == 2
    assert "INVALID:" in output.err
    assert "must-not-print" not in output.err


def test_output_redacts_ids_identities_rules_credentials_and_secrets(tmp_path, capsys):
    apps = _applications()
    wildcard = next(app for app in apps if app["domain"] == "*.sligolabs.com")
    wildcard["policies"][0]["include"] = [
        {"email": {"email": "different-private-person@example.test"}},
        {"secret": "raw-policy-secret-must-not-print"},
    ]
    tunnel = _tunnel()
    tunnel["credentials-file"] = "/private/credential-path-must-not-print.json"
    tunnel["token"] = "tunnel-secret-must-not-print"

    result, stdout, stderr = _run(tmp_path, apps, capsys, tunnel=tunnel)
    combined = stdout + stderr

    assert result == 1
    assert "access.policy" in combined
    for forbidden in (
        "application-id-for-",
        "account-id-must-not-print",
        "policy-id-must-not-print",
        "private-person@example.test",
        "different-private-person@example.test",
        "identity-provider-id-must-not-print",
        "raw-policy-secret-must-not-print",
        "credential-path-must-not-print",
        "tunnel-secret-must-not-print",
        "credentials-file",
    ):
        assert forbidden not in combined

#!/usr/bin/env python3
"""Validate the secret-free Sligo Labs Cloudflare Access rollout contract.

The validator is deliberately offline and read-only. It consumes operator-made
exports, compares their semantics, and prints only public host/path information
and bounded drift codes. It never prints application IDs, account IDs, policy
rules, identities, credential paths, or secret-shaped values.
"""

from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPECTED = ROOT / "docs" / "runbooks" / "sligolabs-cloudflare-access.expected.yaml"
VALID_ACTIONS = {"allow", "bypass", "service_auth"}
_POLICY_METADATA_KEYS = {
    "id",
    "name",
    "created_at",
    "updated_at",
    "reusable",
}
_APP_POLICY_SETTING_KEYS = (
    "session_duration",
    "allowed_idps",
    "auto_redirect_to_identity",
    "enable_binding_cookie",
    "http_only_cookie_attribute",
    "same_site_cookie_attribute",
)


class InvalidInput(ValueError):
    """An input could not be parsed or does not satisfy the public schema."""


@dataclass(frozen=True)
class Finding:
    code: str
    message: str


def _load_yaml(path: Path, label: str) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InvalidInput(f"{label} is unreadable or invalid YAML") from exc


def load_expected(path: Path) -> dict[str, Any]:
    payload = _load_yaml(path, "expected manifest")
    if not isinstance(payload, dict):
        raise InvalidInput("expected manifest must be a mapping")
    if payload.get("contract_version") != 1:
        raise InvalidInput("expected manifest has an unsupported contract_version")

    access = payload.get("access")
    tunnels = payload.get("tunnels")
    if not isinstance(access, dict) or not isinstance(tunnels, dict):
        raise InvalidInput("expected manifest must contain access and tunnels mappings")

    wildcard = access.get("wildcard_human")
    exact = access.get("exact_applications")
    paths = access.get("path_applications")
    excluded = access.get("excluded_host_patterns")
    local = tunnels.get("local")
    remote = tunnels.get("remote_hermes")
    if not isinstance(wildcard, dict):
        raise InvalidInput("access.wildcard_human must be a mapping")
    if not isinstance(exact, list) or not all(isinstance(item, dict) for item in exact):
        raise InvalidInput("access.exact_applications must be a list of mappings")
    if not isinstance(paths, list) or not all(isinstance(item, dict) for item in paths):
        raise InvalidInput("access.path_applications must be a list of mappings")
    if not isinstance(excluded, list) or not all(isinstance(item, str) for item in excluded):
        raise InvalidInput("access.excluded_host_patterns must be a list of strings")
    if not isinstance(local, dict) or not isinstance(local.get("ingress"), list):
        raise InvalidInput("tunnels.local.ingress must be a list")
    if not all(isinstance(item, dict) for item in local["ingress"]):
        raise InvalidInput("tunnels.local.ingress entries must be mappings")
    if not isinstance(remote, dict):
        raise InvalidInput("tunnels.remote_hermes must be a mapping")

    for item in [wildcard, *exact, *paths]:
        domain = item.get("domain")
        action = _normalize_action(item.get("action"))
        if not isinstance(domain, str) or not domain.strip() or action not in VALID_ACTIONS:
            raise InvalidInput("each expected Access application needs a domain and valid action")
        reference = item.get("same_human_policy_as")
        if reference is not None and (
            not isinstance(reference, str) or not reference.strip()
        ):
            raise InvalidInput("same_human_policy_as must name an application domain")
    for item in local["ingress"]:
        if not isinstance(item.get("service"), str):
            raise InvalidInput("each expected local ingress rule needs a service")
    if not isinstance(remote.get("hostname"), str) or not isinstance(remote.get("service"), str):
        raise InvalidInput("tunnels.remote_hermes needs hostname and service")
    return payload


def load_access_export(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidInput("Access export is unreadable or invalid JSON") from exc

    if isinstance(payload, dict):
        if "result" not in payload:
            raise InvalidInput("Access export wrapper must contain result")
        payload = payload["result"]
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise InvalidInput("Access export must be an application array or a result wrapper")
    for app in payload:
        if not _application_domains(app):
            raise InvalidInput(
                "every Access application must contain a domain or destinations"
            )
        policies = app.get("policies")
        if policies is not None and (
            not isinstance(policies, list) or not all(isinstance(policy, dict) for policy in policies)
        ):
            raise InvalidInput("application policies must be a list when present")
    return payload


def load_tunnel_config(path: Path) -> dict[str, Any]:
    payload = _load_yaml(path, "local tunnel config")
    if not isinstance(payload, dict):
        raise InvalidInput("local tunnel config must be a mapping")
    ingress = payload.get("ingress")
    if not isinstance(ingress, list) or not all(isinstance(rule, dict) for rule in ingress):
        raise InvalidInput("local tunnel config must contain an ingress rule list")
    return payload


def load_route_export(path: Path) -> list[dict[str, Any]]:
    payload = _load_yaml(path, "sanitized Hermes route export")
    if isinstance(payload, dict):
        if isinstance(payload.get("ingress"), list):
            payload = payload["ingress"]
        elif isinstance(payload.get("result"), list):
            payload = payload["result"]
        else:
            raise InvalidInput("sanitized Hermes route export needs ingress or result")
    if not isinstance(payload, list) or not all(isinstance(rule, dict) for rule in payload):
        raise InvalidInput("sanitized Hermes route export must contain a route list")
    return payload


def _normalize_action(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _normalize_domain(value: str) -> str:
    raw = value.strip()
    if "://" in raw:
        parsed = urlsplit(raw)
    else:
        parsed = urlsplit(f"//{raw}")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or ""
    if path == "/":
        path = ""
    return f"{host}{path}"


def _application_domains(app: dict[str, Any]) -> list[str]:
    """Return authoritative normalized destinations for an Access application."""
    destinations = app.get("destinations")
    if destinations is not None:
        if not isinstance(destinations, list) or not all(
            isinstance(item, dict) for item in destinations
        ):
            raise InvalidInput("application destinations must be a list of mappings")
        domains = []
        for destination in destinations:
            uri = destination.get("uri")
            if isinstance(uri, str) and uri.strip():
                domains.append(_normalize_domain(uri))
        return list(dict.fromkeys(domains))

    domain = app.get("domain")
    if isinstance(domain, str) and domain.strip():
        return [_normalize_domain(domain)]
    return []


def _split_domain(value: str) -> tuple[str, str]:
    normalized = _normalize_domain(value)
    if "/" not in normalized:
        return normalized, ""
    host, path = normalized.split("/", 1)
    return host, f"/{path}"


def _app_actions(app: dict[str, Any]) -> set[str]:
    actions: set[str] = set()
    for key in ("action", "decision"):
        action = _normalize_action(app.get(key))
        if action:
            actions.add(action)
    policies = app.get("policies")
    if isinstance(policies, list):
        for policy in policies:
            for key in ("action", "decision"):
                action = _normalize_action(policy.get(key))
                if action:
                    actions.add(action)
    return actions


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        canonical = [_canonical(item) for item in value]
        return sorted(canonical, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return value


def _human_policy_semantics(app: dict[str, Any]) -> Any | None:
    """Return comparable ordered policy semantics without rendering them."""

    if "policy_semantics" in app:
        return app["policy_semantics"]

    policies = app.get("policies")
    if not isinstance(policies, list) or not policies:
        return None

    ordered = sorted(
        policies,
        key=lambda item: (
            item.get("precedence") if isinstance(item.get("precedence"), int) else 1_000_000,
            str(item.get("name") or ""),
        ),
    )
    normalized_policies: list[dict[str, Any]] = []
    for policy in ordered:
        decision = _normalize_action(policy.get("decision") or policy.get("action"))
        normalized = {
            key: _canonical(value)
            for key, value in policy.items()
            if key not in _POLICY_METADATA_KEYS
        }
        normalized["decision"] = decision
        normalized.pop("action", None)
        normalized_policies.append(normalized)

    app_settings = {
        key: _canonical(app[key])
        for key in _APP_POLICY_SETTING_KEYS
        if key in app
    }
    return {"app": app_settings, "policies": normalized_policies}


def _contains_service_token_selector(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {
                "service_token",
                "service_tokens",
                "any_valid_service_token",
                "access_service_token",
            }:
                return True
            if _contains_service_token_selector(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_service_token_selector(item) for item in value)
    return False


def _has_unconditional_bypass_policy(app: dict[str, Any]) -> bool:
    policies = app.get("policies")
    if not isinstance(policies, list):
        return False
    for policy in policies:
        decision = _normalize_action(policy.get("decision") or policy.get("action"))
        include = policy.get("include")
        if decision != "bypass" or not isinstance(include, list):
            continue
        includes_everyone = any(
            isinstance(selector, dict) and "everyone" in selector
            for selector in include
        )
        if includes_everyone and not policy.get("exclude") and not policy.get("require"):
            return True
    return False


def _host_matches_wildcard(host: str, wildcard: str) -> bool:
    if not wildcard.startswith("*."):
        return host == wildcard
    suffix = wildcard[1:]
    if not host.endswith(suffix):
        return False
    prefix = host[: -len(suffix)]
    return bool(prefix) and "." not in prefix


def _expected_applications(expected: dict[str, Any]) -> list[dict[str, Any]]:
    access = expected["access"]
    return [
        access["wildcard_human"],
        *access["exact_applications"],
        *access["path_applications"],
    ]


def validate_access(expected: dict[str, Any], apps: list[dict[str, Any]]) -> list[Finding]:
    access = expected["access"]
    expected_apps = _expected_applications(expected)
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for app in apps:
        for domain in _application_domains(app):
            by_domain.setdefault(domain, []).append(app)

    findings: list[Finding] = []
    for _domain, matching in sorted(by_domain.items()):
        if len(matching) > 1:
            findings.append(Finding("access.duplicate", "ambiguous duplicate Access application match"))

    for wanted in expected_apps:
        domain = _normalize_domain(wanted["domain"])
        matching = by_domain.get(domain, [])
        if not matching:
            findings.append(Finding("access.missing", f"required application missing: {domain}"))
            continue
        actual = matching[0]
        expected_action = _normalize_action(wanted["action"])
        actions = _app_actions(actual)
        if actions != {expected_action}:
            findings.append(
                Finding(
                    "access.action",
                    f"required action mismatch: {domain} expected {expected_action}",
                )
            )
        if expected_action == "service_auth" and not _contains_service_token_selector(actual.get("policies", [])):
            findings.append(
                Finding(
                    "access.service_token",
                    f"service-auth application lacks a service-token selector: {domain}",
                )
            )
        if expected_action == "bypass" and not _has_unconditional_bypass_policy(actual):
            findings.append(
                Finding(
                    "access.bypass_selector",
                    f"bypass application is not unconditional: {domain}",
                )
            )

    wildcard_domain = _normalize_domain(access["wildcard_human"]["domain"])
    for wanted in expected_apps:
        reference = wanted.get("same_human_policy_as")
        if not isinstance(reference, str) or not reference.strip():
            continue
        domain = _normalize_domain(wanted["domain"])
        reference_domain = _normalize_domain(reference)
        matching = by_domain.get(domain, [])
        reference_apps = by_domain.get(reference_domain, [])
        if not matching:
            continue
        if not reference_apps:
            findings.append(
                Finding(
                    "access.policy_reference",
                    f"declared human policy reference is missing: {reference_domain}",
                )
            )
            continue
        actual_semantics = _human_policy_semantics(matching[0])
        reference_semantics = _human_policy_semantics(reference_apps[0])
        if actual_semantics is None or reference_semantics is None:
            findings.append(
                Finding(
                    "access.policy_unverifiable",
                    f"human policy semantics are not present for comparison: {domain}",
                )
            )
        elif actual_semantics != reference_semantics:
            findings.append(
                Finding(
                    "access.policy",
                    f"human policy semantics differ from declared reference: {domain}",
                )
            )

    for pattern in access["excluded_host_patterns"]:
        normalized_pattern = pattern.strip().lower().rstrip(".")
        for domain in by_domain:
            host, _path = _split_domain(domain)
            if fnmatch.fnmatchcase(host, normalized_pattern):
                findings.append(
                    Finding("access.excluded", "excluded development namespace has an application")
                )

    approved = {_normalize_domain(item["domain"]) for item in expected_apps}
    wildcard_host, _wildcard_path = _split_domain(wildcard_domain)
    for domain in sorted(by_domain):
        if domain in approved:
            continue
        host, _path = _split_domain(domain)
        if _host_matches_wildcard(host, wildcard_host):
            findings.append(
                Finding(
                    "access.overlap",
                    "unapproved application overrides wildcard precedence",
                )
            )

    return findings


def _service_matches(actual: Any, expected: str) -> bool:
    if not isinstance(actual, str):
        return False
    actual_value = actual.strip().lower()
    expected_value = expected.strip().lower()
    if expected_value.startswith("port:"):
        try:
            expected_port = int(expected_value.split(":", 1)[1])
            parsed = urlsplit(actual_value)
            host = (parsed.hostname or "").rstrip(".")
            is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
            return (
                parsed.scheme in {"http", "https"}
                and is_loopback
                and parsed.port == expected_port
            )
        except (ValueError, TypeError):
            return False
    return actual_value == expected_value


def _route_matches(actual: dict[str, Any], wanted: dict[str, Any]) -> bool:
    expected_host = wanted.get("hostname")
    expected_path = wanted.get("path")
    if expected_host is not None:
        if str(actual.get("hostname") or "").strip().lower() != str(expected_host).strip().lower():
            return False
    elif actual.get("hostname") not in (None, ""):
        return False
    if expected_path is not None:
        if str(actual.get("path") or "").strip() != str(expected_path).strip():
            return False
    elif actual.get("path") not in (None, ""):
        return False
    return _service_matches(actual.get("service"), str(wanted["service"]))


def _safe_route_label(rule: dict[str, Any]) -> str:
    hostname = str(rule.get("hostname") or "terminal fallback")
    path = str(rule.get("path") or "")
    service = str(rule.get("service") or "")
    if service.startswith("port:"):
        destination = service
    elif service.startswith("http_status:"):
        destination = service
    else:
        destination = "configured origin"
    return f"{hostname}{path} -> {destination}"


def validate_local_tunnel(expected: dict[str, Any], tunnel: dict[str, Any]) -> list[Finding]:
    wanted_rules = expected["tunnels"]["local"]["ingress"]
    actual_rules = tunnel["ingress"]
    findings: list[Finding] = []

    if len(actual_rules) != len(wanted_rules):
        findings.append(
            Finding(
                "tunnel.rule_count",
                f"local ingress rule count differs: expected {len(wanted_rules)}",
            )
        )

    for index, wanted in enumerate(wanted_rules):
        if index >= len(actual_rules) or not _route_matches(actual_rules[index], wanted):
            findings.append(
                Finding(
                    "tunnel.order",
                    f"local ingress rule {index + 1} must be {_safe_route_label(wanted)}",
                )
            )
    return findings


def validate_remote_tunnel(expected: dict[str, Any], routes: list[dict[str, Any]]) -> list[Finding]:
    wanted = expected["tunnels"]["remote_hermes"]
    if any(_route_matches(route, wanted) for route in routes):
        return []
    return [
        Finding(
            "tunnel.remote_hermes",
            f"sanitized Hermes route export is missing {wanted['hostname']} -> {wanted['service']}",
        )
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED)
    parser.add_argument("--access-export", type=Path, required=True)
    parser.add_argument("--tunnel-config", type=Path, required=True)
    parser.add_argument(
        "--hermes-tunnel-export",
        "--remote-tunnel-export",
        dest="hermes_tunnel_export",
        type=Path,
        help="Optional sanitized route list for the separately managed Hermes tunnel",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        expected = load_expected(args.expected)
        apps = load_access_export(args.access_export)
        tunnel = load_tunnel_config(args.tunnel_config)
        remote_routes = (
            load_route_export(args.hermes_tunnel_export)
            if args.hermes_tunnel_export is not None
            else None
        )
    except InvalidInput as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2

    findings = validate_access(expected, apps)
    findings.extend(validate_local_tunnel(expected, tunnel))
    if remote_routes is None:
        print("INFO [tunnel.remote_hermes] unverified: no sanitized route export supplied")
    else:
        findings.extend(validate_remote_tunnel(expected, remote_routes))
        if not any(finding.code == "tunnel.remote_hermes" for finding in findings):
            print("OK [tunnel.remote_hermes] sanitized route export conforms")

    if findings:
        for finding in findings:
            print(f"DRIFT [{finding.code}] {finding.message}")
        print(f"RESULT drift ({len(findings)} finding(s))")
        return 1

    print("RESULT conforming")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

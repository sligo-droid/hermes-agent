from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "prove_gbrain_client_knowledge.py"
    spec = importlib.util.spec_from_file_location("prove_gbrain_client_knowledge", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lane_b_network_audit_ignores_destinationless_socket_creation():
    mod = _load_module()
    trace = "\n".join(
        [
            "41 socket(AF_INET, SOCK_STREAM|SOCK_CLOEXEC, IPPROTO_IP) = 62",
            '41 connect(62, {sa_family=AF_INET, sin_port=htons(18765), '
            'sin_addr=inet_addr("127.0.0.1")}, 16) = -1 EINPROGRESS',
            '41 sendto(62, "POST /v1/messages", 17, MSG_DONTWAIT, NULL, 0) = 17',
        ]
    )

    loopback, non_loopback, dns = mod.lane_b_network_attempts(trace)

    assert len(loopback) == 1
    assert non_loopback == []
    assert dns == []


def test_lane_b_network_audit_rejects_external_destination():
    mod = _load_module()
    trace = (
        '41 connect(62, {sa_family=AF_INET, sin_port=htons(443), '
        'sin_addr=inet_addr("203.0.113.9")}, 16) = -1 ENETUNREACH'
    )

    loopback, non_loopback, dns = mod.lane_b_network_attempts(trace)

    assert loopback == []
    assert non_loopback == [trace]
    assert dns == []


def test_lane_b_network_audit_identifies_dns_destination():
    mod = _load_module()
    trace = (
        '41 sendto(62, "query", 5, 0, {sa_family=AF_INET, sin_port=htons(53), '
        'sin_addr=inet_addr("127.0.0.11")}, 16) = 5'
    )

    loopback, non_loopback, dns = mod.lane_b_network_attempts(trace)

    assert loopback == []
    assert non_loopback == [trace]
    assert dns == [trace]


def test_lane_b_rejects_same_project_citation_absent_from_request():
    mod = _load_module()
    source = Path(__file__).resolve().parents[1] / "fixtures" / "client_knowledge_gbrain" / "source"
    request = {
        "body": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                '<pages><page slug="projects/pid/project" rank="1">'
                                "PID project evidence</page></pages>"
                            ),
                        }
                    ],
                }
            ]
        }
    }
    answer = {
        "answer": (
            "The boundary applies "
            "[projects/pid/decisions/project-knowledge-boundary]."
        ),
        "citations": [
            {
                "page_slug": "projects/pid/decisions/project-knowledge-boundary",
                "row_num": None,
            }
        ],
        "gaps": [],
    }

    with pytest.raises(mod.ProofError, match="not present in the exact model request"):
        mod.validate_lane_b_citations(answer, request, source)

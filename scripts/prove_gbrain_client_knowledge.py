#!/usr/bin/env python3
"""Run the isolated, deterministic Hermes client-knowledge GBrain proof.

GBrain is cloned and built only below --output. Lane A runs every scored
GBrain command in an unshared network namespace under a network syscall audit.
Lane B runs in a --network none container with only the locked loopback server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "client_knowledge_gbrain"
PINNED_TAG = "v0.42.73.1"
PINNED_COMMIT = "aecb33e795cc4806f760446c55ab1c350194ddc8"
PINNED_VERSION = "0.42.73.1"
SOURCE_ID = "client-knowledge"
LOOPBACK_MODEL = "anthropic:claude-haiku-4-5-20251001"
LOOPBACK_PORT = 18765
LANE_B_IMAGE = "hermes-gbrain-proof-runtime:0.42.73.1"
FORBIDDEN_OPERATIONS = frozenset(
    {
        "query", "ask", "think", "agent", "capture", "autopilot", "dream",
        "brainstorm", "enrich", "embed", "retrieval-upgrade", "providers-test",
        "multimodal", "image-query",
    }
)
PROVIDER_CONFIG_FIELDS = frozenset(
    {
        "embedding_model", "embedding_dimensions", "expansion_model", "chat_model",
        "chat_fallback_chain", "provider_base_urls", "provider_chat_options",
        "embedding_multimodal_model", "embedding_image_ocr_model", "embedding_columns",
        "search_embedding_column", "openai_api_key", "anthropic_api_key",
        "zeroentropy_api_key", "openrouter_api_key", "voyage_api_key",
        "dashscope_api_key", "google_api_key", "azure_openai_endpoint",
        "azure_openai_deployment", "azure_openai_use_entra", "remote_mcp",
    }
)
INET_RE = re.compile(r"AF_INET6?\b")
DESTINATION_SYSCALL_RE = re.compile(r"\b(?:connect|sendto|sendmsg|sendmmsg)\(")


class ProofError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def lane_b_network_attempts(trace_text: str) -> tuple[list[str], list[str], list[str]]:
    """Classify destination-bearing internet syscalls from the GBrain trace.

    Creating an AF_INET socket does not identify a destination and is therefore
    not itself evidence of an external attempt. The subsequent connect/send
    syscall carries the sockaddr that the Lane B policy must evaluate.
    """
    destination_attempts = [
        line
        for line in trace_text.splitlines()
        if INET_RE.search(line) and DESTINATION_SYSCALL_RE.search(line)
    ]
    loopback = [
        line
        for line in destination_attempts
        if 'inet_addr("127.0.0.1")' in line and f"htons({LOOPBACK_PORT})" in line
    ]
    non_loopback = [line for line in destination_attempts if line not in loopback]
    dns = [line for line in destination_attempts if "htons(53)" in line]
    return loopback, non_loopback, dns


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def require_binary(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise ProofError(f"required executable not found: {name}")
    return resolved


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise ProofError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )
    return result


def git_env(timestamp: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Hermes GBrain Proof",
        "GIT_AUTHOR_EMAIL": "proof@example.invalid",
        "GIT_COMMITTER_NAME": "Hermes GBrain Proof",
        "GIT_COMMITTER_EMAIL": "proof@example.invalid",
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }


def commit_all(repo: Path, message: str, timestamp: str) -> str:
    env = git_env(timestamp)
    run(["git", "add", "-A"], cwd=repo, env=env)
    run(["git", "-c", "commit.gpgSign=false", "commit", "-m", message], cwd=repo, env=env)
    return run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def install_pinned_gbrain(output: Path) -> dict[str, Any]:
    install = output / "upstream"
    require_binary("git")
    bun = require_binary("bun")
    if install.exists():
        shutil.rmtree(install)
    run(
        [
            "git", "clone", "--filter=blob:none", "--branch", PINNED_TAG,
            "--single-branch", "https://github.com/garrytan/gbrain.git", str(install),
        ],
        cwd=output,
        timeout=600,
    )
    commit = run(["git", "rev-parse", "HEAD"], cwd=install).stdout.strip()
    if commit != PINNED_COMMIT:
        raise ProofError(f"pinned tag resolved to {commit}, expected {PINNED_COMMIT}")
    package = json.loads((install / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != PINNED_VERSION:
        raise ProofError("pinned package version mismatch")
    run([bun, "install", "--frozen-lockfile"], cwd=install, timeout=900)
    run([bun, "run", "build"], cwd=install, timeout=900)
    binary = install / "bin" / "gbrain"
    version = run([str(binary), "--version"], cwd=install).stdout.strip()
    if version != f"gbrain {PINNED_VERSION}":
        raise ProofError(f"built binary reports unexpected version: {version}")
    return {
        "root": install,
        "binary": binary,
        "launcher": [bun, str(install / "src" / "cli.ts")],
        "commit": commit,
        "tag": PINNED_TAG,
        "version": PINNED_VERSION,
        "binary_sha256": sha256_file(binary),
        "package_sha256": sha256_file(install / "package.json"),
        "lock_sha256": sha256_file(install / "bun.lock"),
        "bun_version": run([bun, "--version"], cwd=install).stdout.strip(),
    }


def clean_env(home_root: Path, launcher: list[str], *, lane_b: bool = False) -> dict[str, str]:
    env = {
        "HOME": str(home_root),
        # GBrain appends /.gbrain to this parent root.
        "GBRAIN_HOME": str(home_root),
        "PATH": f"{Path(launcher[0]).parent}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "NODE_ENV": "test",
        "GBRAIN_SKIP_STARTUP_HOOKS": "1",
        "GBRAIN_NO_BANNER": "1",
    }
    if lane_b:
        env.update(
            {
                "ANTHROPIC_API_KEY": "synthetic-loopback-key",
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{LOOPBACK_PORT}",
            }
        )
    return env


def config_path(home_root: Path) -> Path:
    return home_root / ".gbrain" / "config.json"


def sanitize_file_config(home_root: Path, *, lane_b: bool = False) -> dict[str, Any]:
    path = config_path(home_root)
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in PROVIDER_CONFIG_FIELDS:
        config.pop(key, None)
    config["embedding_disabled"] = True
    config["eval"] = {"capture": True, "scrub_pii": True}
    config["self_upgrade"] = {"mode": "off"}
    config["embedding_multimodal"] = False
    config["embedding_image_ocr"] = False
    if lane_b:
        config["anthropic_api_key"] = "synthetic-loopback-key"
        config["provider_base_urls"] = {"anthropic": f"http://127.0.0.1:{LOOPBACK_PORT}"}
        config["chat_model"] = LOOPBACK_MODEL
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config


class AuditedGBrain:
    def __init__(self, launcher: list[str], root: Path, home_root: Path, ledger: list[dict[str, Any]]):
        self.launcher = launcher
        self.root = root.resolve()
        self.home_root = home_root.resolve()
        self.ledger = ledger
        self.traces = self.root / "traces"
        self.traces.mkdir(parents=True, exist_ok=True)
        self.strace = require_binary("strace")
        self.bwrap = require_binary("bwrap")

    def call(
        self,
        operation: str,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[str]:
        if operation in FORBIDDEN_OPERATIONS:
            raise ProofError(f"forbidden provider-free operation requested: {operation}")
        trace = self.traces / f"{len(self.ledger):03d}-{operation}.trace"
        command = [
            self.strace, "-f", "-qq", "-e", "trace=network", "-o", str(trace),
            self.bwrap, "--unshare-net", "--ro-bind", "/", "/",
            "--bind", str(self.root), str(self.root),
            "--dev-bind", "/dev", "/dev", "--proc", "/proc", "--",
            *self.launcher, *args,
        ]
        started = time.monotonic()
        result = run(
            command,
            cwd=(cwd or self.root),
            env=clean_env(self.home_root, self.launcher),
            timeout=timeout,
            check=False,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        trace_text = trace.read_text(encoding="utf-8", errors="replace") if trace.exists() else ""
        inet_attempts = [line for line in trace_text.splitlines() if INET_RE.search(line)]
        entry = {
            "operation": operation,
            "args": args,
            "returncode": result.returncode,
            "latency_ms": elapsed_ms,
            "stdout_sha256": sha256_bytes(result.stdout.encode()),
            "stderr_sha256": sha256_bytes(result.stderr.encode()),
            "trace": str(trace),
            "trace_sha256": sha256_file(trace),
            "inet_attempts": inet_attempts,
        }
        self.ledger.append(entry)
        if inet_attempts:
            raise ProofError(f"provider-free GBrain attempted network access during {operation}")
        if result.returncode != 0:
            raise ProofError(
                f"audited GBrain operation failed: {operation}\n{result.stderr[-1500:]}"
            )
        return result


def network_canaries(root: Path) -> dict[str, Any]:
    trace = root / "network-canary.trace"
    code = (
        "import socket, json; out={}; "
        "s=socket.socket(); out['loopback']=s.connect_ex(('127.0.0.1',9)); s.close(); "
        "s=socket.socket(); out['external']=s.connect_ex(('192.0.2.1',443)); s.close(); "
        "\ntry: socket.getaddrinfo('provider.invalid',443); out['dns']='resolved'\n"
        "except Exception as e: out['dns']=type(e).__name__\n"
        "print(json.dumps(out,sort_keys=True))"
    )
    result = run(
        [
            require_binary("strace"), "-f", "-qq", "-e", "trace=network", "-o", str(trace),
            require_binary("bwrap"), "--unshare-net", "--ro-bind", "/", "/",
            "--bind", str(root), str(root), "--dev-bind", "/dev", "/dev",
            "--proc", "/proc", "--", sys.executable, "-c", code,
        ],
        cwd=root,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
    )
    data = json.loads(result.stdout)
    trace_text = trace.read_text(encoding="utf-8", errors="replace")
    if not INET_RE.search(trace_text):
        raise ProofError("network canary audit did not observe its deliberate attempts")
    if data["loopback"] == 0 or data["external"] == 0 or data["dns"] == "resolved":
        raise ProofError("deny-all network canary unexpectedly reached a target")
    return {**data, "trace_sha256": sha256_file(trace), "positive_control_passed": True}


def prepare_source(root: Path) -> tuple[Path, str]:
    source = root / "source"
    shutil.copytree(FIXTURES / "source", source)
    run(["git", "init", "-b", "main"], cwd=source)
    initial = commit_all(source, "fixture: initial synthetic corpus", "2026-08-04T10:00:00Z")
    return source, initial


def render_requirement(stage: str) -> tuple[str, str | None]:
    data = {
        "add": ("current", "PID expects one concise status report each week.", ["synthetic-pid-report-001"], []),
        "confirm": ("current", "PID expects one concise status report each week.", ["synthetic-pid-report-001", "synthetic-pid-report-002"], []),
        "refine": ("current", "PID expects a concise cited status report every Monday at 09:00 UTC.", ["synthetic-pid-report-001", "synthetic-pid-report-002", "synthetic-pid-report-003"], []),
        "contradict": ("disputed", "Reporting cadence is disputed: one source requires Monday at 09:00 UTC while another says Friday.", ["synthetic-pid-report-003", "synthetic-pid-report-004"], []),
        "supersede": ("current", "PID's resolved current requirement is a concise cited status report every Monday at 09:00 UTC.", ["synthetic-pid-report-003", "synthetic-pid-report-005"], ["projects/pid/requirements/reporting-cadence-prior"]),
    }[stage]
    status, claim, refs, supersedes = data
    yaml_refs = "\n".join(f"  - notion:page:{ref}" for ref in refs)
    yaml_supersedes = "[]" if not supersedes else "\n" + "\n".join(f"  - {slug}" for slug in supersedes)
    content = f"""---
type: project
project: pid
status: {status}
kind: requirement
effective_at: 2026-08-04
updated_at: 2026-08-04T12:00:00Z
source_refs:
{yaml_refs}
supersedes: {yaml_supersedes}
confidence: high
sensitivity: internal
---

# Reporting cadence

{claim}

---

## Timeline

- **2026-08-04** | Assimilation stage: {stage}.
  [Source: notion:page:{refs[-1]}]
"""
    prior = None
    if stage == "supersede":
        prior = content.replace("status: current", "status: superseded", 1).replace(
            "# Reporting cadence", "# Reporting cadence prior state", 1
        ).replace("supersedes: \n  - projects/pid/requirements/reporting-cadence-prior", "supersedes: []")
    return content, prior


def initialize_brain(audit: AuditedGBrain, home_root: Path, source: Path, *, lane_b: bool = False) -> None:
    home_root.mkdir(parents=True, exist_ok=True)
    audit.call("init", ["init", "--pglite", "--no-embedding", "--non-interactive", "--json"], timeout=600)
    sanitize_file_config(home_root, lane_b=lane_b)
    settings = {
        "search.mcp_keyword_only": "true",
        "search.expansion": "false",
        "search.reranker.enabled": "false",
        "search.unified_multimodal": "false",
        "search.unified_multimodal_only": "false",
        "search.cross_modal.llm_intent": "false",
        "embedding_multimodal": "false",
        "embedding_image_ocr": "false",
        "self_upgrade.mode": "off",
    }
    for key, value in settings.items():
        audit.call("config-set", ["config", "set", key, value])
    readback = audit.call(
        "config-get-keyword-only", ["config", "get", "search.mcp_keyword_only"]
    )
    if readback.stdout.strip().lower() != "true" or "source: db plane" not in readback.stderr:
        raise ProofError("search.mcp_keyword_only did not read back true from DB plane")
    audit.call(
        "sources-add",
        ["sources", "add", SOURCE_ID, "--path", str(source), "--no-federated", "--force"],
    )
    audit.call(
        "sync",
        ["sync", "--source", SOURCE_ID, "--no-pull", "--no-embed", "--yes", "--json"],
        timeout=600,
    )


def apply_assimilation(audit: AuditedGBrain, source: Path) -> list[dict[str, Any]]:
    operations = []
    target = source / "projects" / "pid" / "requirements" / "reporting-cadence.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    for index, stage in enumerate(("add", "confirm", "refine", "contradict", "supersede"), start=1):
        content, prior = render_requirement(stage)
        target.write_text(content, encoding="utf-8")
        if prior is not None:
            (target.parent / "reporting-cadence-prior.md").write_text(prior, encoding="utf-8")
        commit = commit_all(
            source,
            f"knowledge: {stage} reporting cadence",
            f"2026-08-04T1{index}:00:00Z",
        )
        audit.call(
            f"sync-{stage}",
            ["sync", "--source", SOURCE_ID, "--no-pull", "--no-embed", "--yes", "--json"],
            timeout=600,
        )
        operations.append(
            {"operation": stage, "commit": commit, "file_sha256": sha256_file(target)}
        )
    return operations


def collect_semantics(audit: AuditedGBrain) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    searches = []
    visible_blob = ""
    for query in expected["queries"]:
        payload = json.dumps({"query": query, "limit": 20}, separators=(",", ":"))
        result = audit.call(
            "search", ["call", "--source", SOURCE_ID, "search", payload]
        )
        rows = json.loads(result.stdout)
        visible = [
            {"source_id": row.get("source_id"), "slug": row.get("slug"), "title": row.get("title"), "chunk_text": row.get("chunk_text")}
            for row in rows
            if row.get("source_id") == SOURCE_ID and str(row.get("slug", "")).startswith("projects/pid/")
        ]
        visible_blob += json.dumps(visible, sort_keys=True)
        if not visible:
            raise ProofError(f"project-scoped keyword retrieval returned no PID result for: {query}")
        searches.append({"query": query, "visible": visible, "raw_count": len(rows)})
    pages = []
    for slug in expected["required_slugs"]:
        payload = json.dumps({"slug": slug, "fuzzy": False}, separators=(",", ":"))
        result = audit.call(
            "get-page", ["call", "--source", SOURCE_ID, "get_page", payload]
        )
        page = json.loads(result.stdout)
        if page.get("source_id") != SOURCE_ID or page.get("slug") != slug:
            raise ProofError(f"get_page identity mismatch for {slug}")
        if page.get("frontmatter", {}).get("project") != "pid":
            raise ProofError(f"frontmatter project mismatch for {slug}")
        refs = page.get("frontmatter", {}).get("source_refs")
        if not isinstance(refs, list) or not refs or not all(str(ref).startswith("notion:page:") for ref in refs):
            raise ProofError(f"citation validation failed for {slug}")
        pages.append(
            {
                "slug": slug,
                "frontmatter": page.get("frontmatter"),
                "compiled_truth_sha256": sha256_bytes(str(page.get("compiled_truth", "")).encode()),
                "timeline_sha256": sha256_bytes(str(page.get("timeline", "")).encode()),
            }
        )
    if expected["forbidden_visible_text"] in visible_blob:
        raise ProofError("decoy disclosure canary reached project-visible search output")
    exported = audit.call("eval-export", ["eval", "export", "--tool", "search", "--limit", "100"])
    eval_rows = [json.loads(line) for line in exported.stdout.splitlines() if line.strip()]
    scored = [row for row in eval_rows if row.get("query") in expected["queries"]]
    if len(scored) < len(expected["queries"]):
        raise ProofError("missing pinned eval-capture evidence for provider-free searches")
    for row in scored:
        if row.get("vector_enabled") is not False or row.get("expansion_applied") is not False:
            raise ProofError("provider-free search runtime did not report vector_enabled:false")
    semantics = {"searches": searches, "pages": pages}
    return semantics, scored


def provider_preflight(launcher: list[str], root: Path) -> dict[str, Any]:
    home = root / "provider-preflight-home"
    home.mkdir(parents=True, exist_ok=True)
    env = clean_env(home, launcher)
    listed = run([*launcher, "providers", "list"], cwd=root, env=env)
    explained = run([*launcher, "providers", "explain", "--json"], cwd=root, env=env)
    matrix = json.loads(explained.stdout)
    env_names = sorted((matrix.get("env_detected") or {}).keys())
    present = [name for name in env_names if env.get(name)]
    if present:
        raise ProofError(f"provider credential variables survived allowlist: {present}")
    return {
        "provider_list_sha256": sha256_bytes(listed.stdout.encode()),
        "provider_matrix_sha256": sha256_bytes(explained.stdout.encode()),
        "registry_credential_names": env_names,
        "present": present,
        "note": "preflight is unscored because pinned explain probes local provider ports",
    }


def lane_a(output: Path, upstream: dict[str, Any]) -> dict[str, Any]:
    root = output / "provider-free"
    root.mkdir(parents=True, exist_ok=True)
    canaries = network_canaries(root)
    source, initial_commit = prepare_source(root)
    ledger: list[dict[str, Any]] = []
    home = root / "home-root"
    audit = AuditedGBrain(upstream["launcher"], root, home, ledger)
    initialize_brain(audit, home, source)
    file_config = json.loads(config_path(home).read_text(encoding="utf-8"))
    present_provider_config = sorted(
        key for key in PROVIDER_CONFIG_FIELDS if file_config.get(key) not in (None, "", [], {})
    )
    if present_provider_config:
        raise ProofError(f"provider config survived Lane A sanitization: {present_provider_config}")
    operations = apply_assimilation(audit, source)
    semantics, eval_rows = collect_semantics(audit)
    semantic_hash = sha256_bytes(canonical_json(semantics))

    bundle = root / "client-knowledge.bundle"
    run(["git", "bundle", "create", str(bundle), "--all"], cwd=source)
    restored_source = root / "restored-source"
    run(["git", "clone", str(bundle), str(restored_source)], cwd=root)
    restored_root = root / "restore"
    restored_root.mkdir()
    restored_ledger: list[dict[str, Any]] = []
    restored_home = restored_root / "home-root"
    restored_audit = AuditedGBrain(upstream["launcher"], restored_root, restored_home, restored_ledger)
    initialize_brain(restored_audit, restored_home, restored_source)
    restored_semantics, restored_eval = collect_semantics(restored_audit)
    restored_hash = sha256_bytes(canonical_json(restored_semantics))
    if restored_hash != semantic_hash:
        raise ProofError("Git bundle/PGLite restore did not reproduce semantic retrieval")

    forbidden_invoked = sorted(
        {entry["operation"] for entry in [*ledger, *restored_ledger]} & FORBIDDEN_OPERATIONS
    )
    if forbidden_invoked:
        raise ProofError(f"forbidden Lane A operations invoked: {forbidden_invoked}")
    receipt = {
        "schema_version": 1,
        "lane": "provider_free",
        "gbrain": {key: upstream[key] for key in ("tag", "commit", "version", "binary_sha256", "bun_version")},
        "initialization": {"command": ["init", "--pglite", "--no-embedding"], "embedding_disabled": file_config.get("embedding_disabled") is True},
        "keyword_only": {"readback": True, "readback_plane": "db"},
        "environment": {"policy": "allowlist", "allowed_names": sorted(clean_env(home, upstream["launcher"]).keys()), "provider_credential_names_present": []},
        "config_audit": {"provider_fields_present": present_provider_config, "expansion_enabled": False, "reranker_enabled": False, "multimodal_enabled": False, "self_upgrade_enabled": False},
        "network": {"policy": "deny_all_including_loopback", **canaries, "gbrain_network_attempts": 0, "provider_requests": 0},
        "runtime": {"forbidden_operations_invoked": forbidden_invoked, "searches": [{"query": row.get("query"), "vector_enabled": False, "expansion_applied": False} for row in eval_rows]},
        "assimilation": operations,
        "project_isolation": {"decoy_canary_visible": False, "source_id": SOURCE_ID, "prefix": "projects/pid/"},
        "backup_restore": {"bundle_sha256": sha256_file(bundle), "initial_commit": initial_commit, "final_commit": run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip(), "restored_head": run(["git", "rev-parse", "HEAD"], cwd=restored_source).stdout.strip(), "semantic_parity": True, "restored_eval_rows": len(restored_eval)},
        "metrics": {"query_latency_ms": [entry["latency_ms"] for entry in ledger if entry["operation"] == "search"], "external_provider_cost_usd": 0},
        "command_ledger": ledger,
        "restore_command_ledger": restored_ledger,
        "normalized_result_sha256": semantic_hash,
        "verdict": "pass",
    }
    receipt_path = root / "provider-free-receipt.json"
    write_json(receipt_path, receipt)
    receipt["receipt_path"] = str(receipt_path)
    receipt["receipt_sha256"] = sha256_file(receipt_path)
    return receipt


def build_lane_b_image(output: Path) -> str:
    docker = require_binary("docker")
    build = output / "lane-b-image"
    build.mkdir(parents=True, exist_ok=True)
    dockerfile = build / "Dockerfile"
    dockerfile.write_text(
        "FROM ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea\n"
        "RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 strace git ca-certificates && rm -rf /var/lib/apt/lists/*\n",
        encoding="utf-8",
    )
    run([docker, "build", "-t", LANE_B_IMAGE, str(build)], cwd=output, timeout=900)
    return LANE_B_IMAGE


def prepare_lane_b_brain(output: Path, upstream: dict[str, Any], lane_a_receipt: dict[str, Any]) -> tuple[Path, Path, list[dict[str, Any]]]:
    root = output / "loopback-synthesis"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source"
    bundle = Path(lane_a_receipt["receipt_path"]).parent / "client-knowledge.bundle"
    run(["git", "clone", str(bundle), str(source)], cwd=root)
    home = root / "home-root"
    ledger: list[dict[str, Any]] = []
    audit = AuditedGBrain(upstream["launcher"], root, home, ledger)
    initialize_brain(audit, home, source, lane_b=True)
    return root, home, ledger


def run_lane_b_container(root: Path, home: Path, upstream: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    request_log = root / "loopback-requests.json"
    trace = root / "gbrain-think-network.trace"
    think_output = root / "think-output.json"
    server = ROOT / "scripts" / "gbrain_proof_loopback_server.py"
    response = FIXTURES / "loopback-response.json.txt"
    bun = Path(upstream["launcher"][0]).resolve()
    source_cli = Path(upstream["launcher"][1])
    def quote(value: Any) -> str:
        return shlex.quote(str(value))

    command = (
        f"python3 {quote(server)} --port {LOOPBACK_PORT} --log {quote(request_log)} "
        f"--response {quote(response)} & "
        "server_pid=$!; sleep 0.3; "
        f"strace -f -qq -e trace=network -o {quote(trace)} {quote(bun)} "
        f"{quote(source_cli)} think "
        "'What are PID current reporting and citation requirements?' "
        f"--model {quote(LOOPBACK_MODEL)} --json > {quote(think_output)}; status=$?; "
        "wait $server_pid; exit $status"
    )
    env_flags = [
        "-e", f"GBRAIN_HOME={home}", "-e", f"HOME={home}",
        "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8", "-e", "TZ=UTC",
        "-e", "NODE_ENV=test", "-e", "GBRAIN_SKIP_STARTUP_HOOKS=1",
        "-e", "GBRAIN_NO_BANNER=1", "-e", "ANTHROPIC_API_KEY=synthetic-loopback-key",
        "-e", f"ANTHROPIC_BASE_URL=http://127.0.0.1:{LOOPBACK_PORT}",
    ]
    result = run(
        [
            require_binary("docker"), "run", "--rm", "--network", "none",
            "--entrypoint", "/bin/sh", "-v", f"{root}:{root}",
            "-v", f"{ROOT}:{ROOT}:ro", "-v", f"{upstream['root']}:{upstream['root']}:ro",
            "-v", f"{bun}:{bun}:ro",
            *env_flags, LANE_B_IMAGE, "-c", command,
        ],
        cwd=root,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise ProofError(f"Lane B container failed: {result.stderr[-2000:]}")
    requests = json.loads(request_log.read_text(encoding="utf-8"))
    answer = json.loads(think_output.read_text(encoding="utf-8"))
    trace_text = trace.read_text(encoding="utf-8", errors="replace")
    loopback, non_loopback, dns = lane_b_network_attempts(trace_text)
    if non_loopback:
        raise ProofError("Lane B made a non-loopback network attempt")
    if dns:
        raise ProofError("Lane B made a DNS request")
    if len(loopback) != 1:
        raise ProofError(f"Lane B expected exactly one loopback network attempt, observed {len(loopback)}")
    return answer, requests, sha256_file(trace)


def request_summary(request: dict[str, Any]) -> dict[str, Any]:
    body = request.get("body") or {}
    return {
        "ordinal": request.get("ordinal"),
        "method": request.get("method"),
        "path": request.get("path"),
        "required_header_names": ["anthropic-version", "content-type", "x-api-key"],
        "forbidden_header_names": ["authorization"],
        "body_sha256": request.get("body_sha256"),
        "model": body.get("model"),
    }


def lane_b(
    output: Path,
    upstream: dict[str, Any],
    lane_a_receipt: dict[str, Any],
    *,
    characterize: bool,
) -> dict[str, Any]:
    build_lane_b_image(output)
    root, home, setup_ledger = prepare_lane_b_brain(output, upstream, lane_a_receipt)
    answer, requests, trace_sha = run_lane_b_container(root, home, upstream)
    observed_manifest = {
        "schema_version": 1,
        "model": LOOPBACK_MODEL,
        "expected_request_count": len(requests),
        "requests": [request_summary(request) for request in requests],
    }
    observed_path = root / "observed-loopback-requests.json"
    write_json(observed_path, observed_manifest)
    if characterize:
        return {
            "schema_version": 1,
            "lane": "loopback_synthesis_characterization",
            "observed_manifest": str(observed_path),
            "observed_manifest_sha256": sha256_file(observed_path),
            "verdict": "characterized_not_scored",
        }
    expected_path = FIXTURES / "expected-loopback-requests.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    if any(item.get("body_sha256") == "CHARACTERIZE_BEFORE_SCORING" for item in expected["requests"]):
        raise ProofError("Lane B expected request manifest still contains characterization placeholder")
    if observed_manifest != expected:
        raise ProofError(
            "Lane B request contract drifted from the locked manifest; inspect " + str(observed_path)
        )
    if len(requests) != 1:
        raise ProofError("Lane B expected exactly one loopback request")
    request = requests[0]
    header_names = set(request.get("header_names") or [])
    expected_request = expected["requests"][0]
    if not set(expected_request["required_header_names"]).issubset(header_names):
        raise ProofError("Lane B request is missing required headers")
    if set(expected_request["forbidden_header_names"]) & header_names:
        raise ProofError("Lane B request contained a forbidden header")
    answer_blob = json.dumps(answer, sort_keys=True)
    if "ORANGE-NEBULA-7319" in answer_blob:
        raise ProofError("Lane B synthesized answer disclosed decoy project data")
    citations = answer.get("citations") or []
    if not citations or not all(str(item.get("page_slug", "")).startswith("projects/pid/") for item in citations):
        raise ProofError("Lane B answer lacked project-scoped citations")
    receipt = {
        "schema_version": 1,
        "lane": "loopback_synthesis",
        "expected_request_manifest_sha256": sha256_file(expected_path),
        "actual_request_log_sha256": sha256_file(root / "loopback-requests.json"),
        "expected_request_count": 1,
        "actual_request_count": len(requests),
        "unexpected_loopback_requests": 0,
        "non_loopback_attempts": 0,
        "dns_attempts": 0,
        "embedding_requests": 0,
        "reranker_requests": 0,
        "external_provider_cost_usd": 0,
        "network_trace_sha256": trace_sha,
        "setup_command_ledger": setup_ledger,
        "normalized_answer_sha256": sha256_bytes(canonical_json(answer)),
        "verdict": "pass",
    }
    path = root / "loopback-synthesis-receipt.json"
    write_json(path, receipt)
    receipt["receipt_path"] = str(path)
    receipt["receipt_sha256"] = sha256_file(path)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gbrain-ref", default=PINNED_COMMIT)
    parser.add_argument("--lane", choices=("provider-free", "loopback-synthesis", "all"), default="all")
    parser.add_argument("--characterize-loopback", action="store_true")
    parser.add_argument("--network-isolation", choices=("required",), default="required")
    parser.add_argument("--network-audit", choices=("required",), default="required")
    args = parser.parse_args()
    if args.gbrain_ref != PINNED_COMMIT:
        raise ProofError(f"only approved GBrain commit {PINNED_COMMIT} is accepted")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ProofError("--output must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)
    upstream = install_pinned_gbrain(output)
    preflight = provider_preflight(upstream["launcher"], output)
    write_json(output / "provider-preflight.json", preflight)

    provider_receipt = None
    if args.lane in {"provider-free", "all", "loopback-synthesis"}:
        provider_receipt = lane_a(output, upstream)
    if args.lane == "provider-free":
        print(json.dumps({"provider_free": provider_receipt}, indent=2, sort_keys=True))
        return 0
    loopback_receipt = lane_b(
        output,
        upstream,
        provider_receipt,
        characterize=args.characterize_loopback,
    )
    if args.characterize_loopback:
        print(json.dumps({"provider_free": provider_receipt, "loopback": loopback_receipt}, indent=2, sort_keys=True))
        return 0
    overall = {
        "schema_version": 1,
        "provider_free_receipt_sha256": provider_receipt["receipt_sha256"],
        "loopback_synthesis_receipt_sha256": loopback_receipt["receipt_sha256"],
        "verdict": "pass",
    }
    overall_path = output / "overall-receipt.json"
    write_json(overall_path, overall)
    print(
        json.dumps(
            {
                "provider_free": provider_receipt,
                "loopback_synthesis": loopback_receipt,
                "overall_receipt": str(overall_path),
                "overall_receipt_sha256": sha256_file(overall_path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProofError as exc:
        print(f"proof failed: {exc}", file=sys.stderr)
        raise SystemExit(2)

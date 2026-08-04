# GBrain Client-Knowledge Proof

This runbook documents the current opt-in proof surface for project-scoped,
read-only client-knowledge retrieval through pinned GBrain `v0.42.73.1` at
commit `aecb33e795cc4806f760446c55ab1c350194ddc8`.

## Current Behavior

- The bundled standalone plugin `client-knowledge-gbrain` registers only
  `client_knowledge_search` and `client_knowledge_get` in the
  `client_knowledge` toolset.
- Both tools require the requested lowercase `project_key` to match the
  gateway's trusted `HERMES_PROJECT_KEY` session binding. Unmapped sessions
  fail closed.
- Retrieval is fixed to source `client-knowledge` and slugs below
  `projects/<project-key>/`. Exact page reads disable fuzzy matching.
- Search results are filtered by source and project prefix. Page reads also
  validate project frontmatter, state/kind/confidence/sensitivity values,
  same-project supersession references, and `notion:page:<id>` citations.
- Tool responses are allowlisted and bounded. The plugin does not expose raw
  GBrain commands, synthesis, ingestion, sync, provider calls, or write tools.
- Delegated children lose the `client_knowledge` toolset after inheritance and
  composite expansion. OpenCode workers also lose GBrain paths and
  `HERMES_CLIENT_KNOWLEDGE_*` environment variables.
- Prompt guidance asks for explicit, on-demand retrieval only. Hermes does not
  automatically query client knowledge before every turn.

## Runtime Configuration

The plugin is disabled until an operator explicitly enables it and provides an
isolated pinned GBrain installation:

```bash
hermes plugins enable client-knowledge-gbrain
hermes tools enable client_knowledge
```

Set the non-secret configuration in `$HERMES_HOME/config.yaml`:

```yaml
client_knowledge:
  gbrain:
    executable: /absolute/path/to/bun
    args:
      - /absolute/path/to/gbrain/src/cli.ts
    home: /absolute/path/to/isolated/gbrain-root
    checkout: /absolute/path/to/gbrain
    source_id: client-knowledge
    timeout_seconds: 30
    max_context_chars: 8000
```

The plugin verifies that `checkout` has the approved Git HEAD, no modified or
non-ignored untracked files, and package version `0.42.73.1`. The sole launcher
argument must be that checkout's `src/cli.ts`; generated compiled binaries are
not accepted because their provenance cannot be proven from Git state. The
configured executable must report Bun `1.3.14`, and the source launcher must
report exactly `gbrain 0.42.73.1`.

On the proof host, the compiled pinned binary built and reported the correct
version but failed while loading PGLite's Bun-embedded WASM data
(`/$bunfs/root/pglite.data`). The proof therefore invokes the exact pinned
source launcher through Bun. This is an upstream runtime limitation, not
permission to use an unpinned or modified checkout.

GBrain treats `GBRAIN_HOME` as a parent root and creates `.gbrain` below it.
The configured `home` must therefore be a dedicated root, not an existing
general-purpose home directory. No provider credential belongs in this proof
configuration.

## Reproduce the Proof

Prerequisites: Bun, Git, `bwrap`, `strace`, Docker, and outbound access only for
the initial pinned clone/dependency install and Docker build. Use a new output
path for every run:

```bash
python scripts/prove_gbrain_client_knowledge.py \
  --lane all \
  --gbrain-ref aecb33e795cc4806f760446c55ab1c350194ddc8 \
  --network-isolation required \
  --network-audit required \
  --output /tmp/hermes-gbrain-proof
```

The script refuses a non-empty output directory or any other GBrain ref.

### Lane A: provider-free retrieval

Lane A initializes PGLite with embeddings disabled, reads back
`search.mcp_keyword_only=true` from the DB plane, disables expansion,
reranking, multimodal paths, and self-upgrade, and runs every scored GBrain
operation under `bwrap --unshare-net` plus `strace`. Positive controls verify
that loopback, external, and DNS targets are unreachable in that namespace.

It then:

1. syncs a synthetic PID corpus and a decoy project;
2. applies deterministic add, confirm, refine, contradict, and supersede Git
   commits;
3. checks three project-scoped keyword searches and exact page reads;
4. verifies the decoy canary `ORANGE-NEBULA-7319` is not visible;
5. verifies captured searches report `vector_enabled:false` and
   `expansion_applied:false`; and
6. creates a Git bundle, restores into a fresh PGLite brain, and verifies
   normalized semantic parity.

### Lane B: deterministic loopback synthesis

Lane B runs in Docker `--network none`, starts a deterministic Anthropic-shaped
server on container loopback, and traces GBrain's network syscalls. The locked
manifest requires exactly one `POST /v1/messages` request to
`127.0.0.1:18765`, exact canonical body hash and model, required headers, and no
authorization header. Any request drift, DNS/non-loopback destination,
embedding request, reranker request, decoy disclosure, or foreign citation
fails the proof.

Lane B is proof-only. The repository does not configure or call a live model or
client system.

## Accepted Evidence

A passing run writes:

- `provider-free/provider-free-receipt.json`
- `loopback-synthesis/loopback-synthesis-receipt.json`
- `overall-receipt.json`
- per-command network traces, request logs, and the deterministic Git bundle

The proof is accepted only when both lane receipts and the overall receipt say
`"verdict": "pass"`. Paths and latency-dependent hashes are expected to differ
between output roots; the semantic result, fixture commits, bundle hash, locked
request body, and normalized synthesis answer are deterministic.

## Security Boundary and Stop Conditions

The implemented boundary covers tool exposure, trusted session-to-project
authorization, source/slug/frontmatter validation, child-tool denial, worker
environment stripping, bounded output, provider-free retrieval, and audited
loopback-only synthesis.

It does **not** protect client files from a hostile process running as the same
OS user. A same-UID worker may be able to open another process's readable files
regardless of Hermes tool/config filtering. If untrusted or adversarial workers
are in scope, rollout must stop until the brain and client corpus run under a
separate UID or container/mount namespace with filesystem permissions that
deny worker access. Tool isolation alone is not a filesystem security boundary.

Also stop rollout if any of these become necessary:

- a GBrain ref other than the pinned commit;
- a live provider credential or external client request;
- automatic pre-turn retrieval;
- delegated-worker access to the brain;
- cross-project/fuzzy/raw GBrain passthrough; or
- an unreviewed change to the locked Lane B request manifest.

## Non-Goals

- No live Notion ingestion or client content is included.
- No production provider synthesis is enabled.
- No automatic memory replacement or global GBrain installation is performed.
- No worker-facing client-knowledge capability is provided.
- No claim is made that same-UID filesystem isolation is solved.

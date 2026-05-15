# sligo-labs-vm Local Honcho Memory

This directory captures the private local-memory setup for this machine.
It is intentionally opinionated: Hermes uses local Honcho, local
llama.cpp embeddings, and the Hermes OpenAI-compatible proxy for GPT-5.5.
Hindsight is not part of this setup.

Restore steps:

```bash
mkdir -p ~/.hermes
cp configs/sligo-labs-vm/honcho.json ~/.hermes/honcho.json
hermes config set memory.provider honcho
hermes config set skills.external_dirs '["/home/droid/hermes/configs/sligo-labs-vm/skills"]'
hermes honcho env > /path/to/local-honcho/.env
# Edit DB_CONNECTION_URI in that file for local Postgres with pgvector.
hermes honcho embeddings install
# Use --docker on machines without llama-server on PATH.
hermes honcho embeddings start --docker
hermes honcho embeddings status
hermes proxy start --provider openai-codex --port 8645
# Start local Honcho using the generated env, then verify:
hermes honcho status
```

Local Honcho itself is expected to run at `http://127.0.0.1:8000` with
Postgres + pgvector. The generated env is server-side Honcho configuration;
`~/.hermes/honcho.json` is Hermes-side client configuration.

Embedding defaults:

- Model: `Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0`
- Dimensions: `1024`
- Context: `32768`
- Port: `8080`
- Base URL: `http://127.0.0.1:8080/v1`

The default is the 0.6B Q8_0 Qwen3 embedding model because memory recall
is on Hermes' read path. The 8B Qwen3 embedding model is a higher-recall
option, but it is 4096-dimensional and heavier to run; it should only be
used after rebuilding Honcho's embedding storage on an empty database.

Honcho LLM inference should use the local Hermes proxy at
`http://127.0.0.1:8645/v1`, not raw OAuth tokens in Honcho config files.
Use `hermes honcho embeddings tune` for llama.cpp resource knobs; keep the
model and dimensions fixed after Honcho has stored data.

The `skills/` directory contains local Hermes operating procedures for this
machine. Configure it with `skills.external_dirs` so Hermes can load those
skills without copying them into `~/.hermes/skills`.

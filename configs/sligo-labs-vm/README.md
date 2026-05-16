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
# On this VM, use the helper below so embeddings start with Docker GPU access.
hermes honcho embeddings status
hermes proxy start --provider openai-codex --port 8645
# Start local Honcho using the generated env, then verify:
hermes honcho status
```

On this VM, use the checked-in helper so future sessions do not rediscover
the startup order or the local Docker build override:

```bash
configs/sligo-labs-vm/start-honcho-stack.sh
configs/sligo-labs-vm/status-honcho-stack.sh
```

Local Honcho itself is expected to run at `http://127.0.0.1:8000` with
Postgres + pgvector. The generated env is server-side Honcho configuration;
`~/.hermes/honcho.json` is Hermes-side client configuration.

Embedding defaults:

- Model: `Qwen/Qwen3-Embedding-8B-GGUF:Q8_0`
- Raw model dimensions: `4096`
- Stored/search dimensions: `2000`
- Context: `40960`
- Port: `8080`
- Base URL: `http://127.0.0.1:8080/v1`
- Runtime: Docker with `ghcr.io/ggml-org/llama.cpp:server-cuda`,
  `--gpus all`, and `--n-gpu-layers 999`

GPU container prerequisites:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu24.04 nvidia-smi
```

Honcho runs in Docker on this VM, so the compose override replaces Docker-side
service URLs while preserving the host-facing ports:

- Honcho containers call embeddings at `http://hermes-honcho-embeddings:8080/v1`
  after the helper connects that container to `honcho_default`.
- Honcho containers call the Hermes proxy at `http://host.docker.internal:8645/v1`;
  the helper starts a second proxy listener on the Docker bridge when needed.
- Host tools still verify embeddings at `http://127.0.0.1:8080/v1` and proxy
  health at `http://127.0.0.1:8645/health`.

This VM uses the 8B Q8_0 Qwen3 embedding model because the RTX 4090 has
enough VRAM for it. Qwen3 embeddings support Matryoshka-style reduced
dimensions, but the local llama.cpp endpoint currently ignores the OpenAI
`dimensions` request parameter. Honcho therefore requests 2000 dimensions and
defensively truncates longer embedding responses in its embedding client before
validation/storage/search.

pgvector HNSW indexes cannot be created above 2000 dimensions for normal
`vector` columns. The helper keeps Honcho's embedding tables at `vector(2000)`
so the normal HNSW indexes remain available as the memory database grows.
Changing the stored dimension requires rebuilding Honcho's embedding storage on
an empty database.

Honcho LLM inference should use the local Hermes proxy at
`http://127.0.0.1:8645/v1`, not raw OAuth tokens in Honcho config files.
Use `hermes honcho embeddings tune` for llama.cpp resource knobs; keep the
model and dimensions fixed after Honcho has stored data.

The Honcho checkout lives at `/home/droid/honcho`. Its upstream Dockerfile
currently spends several minutes recursively chowning the in-image virtualenv
on this machine, so this directory includes `honcho.local.Dockerfile` and
`honcho-compose.override.yml` for local startup. The override keeps the
upstream compose services, ports, env file, Postgres/pgvector, and Redis, but
builds `api` and `deriver` with the local-only image `honcho-local-sligo`.

The `skills/` directory contains local Hermes operating procedures for this
machine. Configure it with `skills.external_dirs` so Hermes can load those
skills without copying them into `~/.hermes/skills`.

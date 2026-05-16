#!/usr/bin/env bash
set -euo pipefail

HERMES_REPO="${HERMES_REPO:-/home/droid/hermes}"
HONCHO_REPO="${HONCHO_REPO:-/home/droid/honcho}"
HERMES_PY="${HERMES_PY:-$HERMES_REPO/.venv/bin/python}"
PROXY_LOG="${PROXY_LOG:-/home/droid/.hermes/logs/honcho-hermes-proxy.log}"
PROXY_DOCKER_LOG="${PROXY_DOCKER_LOG:-/home/droid/.hermes/logs/honcho-hermes-proxy-docker.log}"
HONCHO_NETWORK="${HONCHO_NETWORK:-honcho_default}"
EMBEDDINGS_CONTAINER="${EMBEDDINGS_CONTAINER:-hermes-honcho-embeddings}"
EMBEDDINGS_IMAGE="${EMBEDDINGS_IMAGE:-ghcr.io/ggml-org/llama.cpp:server-cuda}"
EMBEDDINGS_MODEL="${EMBEDDINGS_MODEL:-Qwen/Qwen3-Embedding-8B-GGUF:Q8_0}"
EMBEDDINGS_DIMENSIONS="${EMBEDDINGS_DIMENSIONS:-4096}"
HONCHO_EMBEDDINGS_DIMENSIONS="${HONCHO_EMBEDDINGS_DIMENSIONS:-2000}"
EMBEDDINGS_CONTEXT="${EMBEDDINGS_CONTEXT:-40960}"

mkdir -p "$(dirname "$PROXY_LOG")"

ensure_gpu_embeddings() {
  if ! docker run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu24.04 nvidia-smi >/dev/null 2>&1; then
    echo "Docker cannot access the NVIDIA GPU. Install/configure nvidia-container-toolkit first." >&2
    exit 1
  fi

  local running_image
  local device_requests
  running_image="$(docker inspect "$EMBEDDINGS_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || true)"
  device_requests="$(docker inspect "$EMBEDDINGS_CONTAINER" --format '{{json .HostConfig.DeviceRequests}}' 2>/dev/null || true)"

  if docker ps --format '{{.Names}}' | grep -Fxq "$EMBEDDINGS_CONTAINER" \
    && [ "$running_image" = "$EMBEDDINGS_IMAGE" ] \
    && [ "$device_requests" != "null" ] \
    && curl -fsS "http://127.0.0.1:8080/v1/models" | grep -Fq "$EMBEDDINGS_MODEL"; then
    return
  fi

  docker rm -f "$EMBEDDINGS_CONTAINER" >/dev/null 2>&1 || true
  docker run \
    --detach \
    --gpus all \
    --name "$EMBEDDINGS_CONTAINER" \
    --publish 127.0.0.1:8080:8080 \
    "$EMBEDDINGS_IMAGE" \
    --embedding \
    --host 0.0.0.0 \
    --port 8080 \
    -c "$EMBEDDINGS_CONTEXT" \
    --n-gpu-layers 999 \
    -hf "$EMBEDDINGS_MODEL" >/dev/null
}

wait_for_embeddings() {
  for _ in $(seq 1 180); do
    if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
      return
    fi
    sleep 2
  done
  echo "Timed out waiting for embedding server on 127.0.0.1:8080" >&2
  exit 1
}

honcho_compose() {
  docker compose \
    -f "$HONCHO_REPO/docker-compose.yml" \
    -f "$HERMES_REPO/configs/sligo-labs-vm/honcho-compose.override.yml" \
    "$@"
}

if ! curl -fsS http://127.0.0.1:8645/health >/dev/null 2>&1; then
  setsid "$HERMES_PY" -m hermes_cli.main proxy start \
    --provider openai-codex \
    --host 127.0.0.1 \
    --port 8645 \
    >"$PROXY_LOG" 2>&1 < /dev/null &
fi

ensure_gpu_embeddings
wait_for_embeddings
"$HERMES_PY" -m hermes_cli.main honcho embeddings status \
  --model "$EMBEDDINGS_MODEL" \
  --dimensions "$EMBEDDINGS_DIMENSIONS" \
  --ctx "$EMBEDDINGS_CONTEXT" || true
curl -fsS http://127.0.0.1:8080/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$EMBEDDINGS_MODEL\",\"input\":\"Hermes Honcho startup embedding dimension check\"}" \
  | "$HERMES_PY" -c "import json,sys; data=json.load(sys.stdin); dim=len(data['data'][0]['embedding']); expected=$EMBEDDINGS_DIMENSIONS; print(f'host embeddings dimensions: {dim}'); raise SystemExit(0 if dim == expected else 1)"

honcho_compose up -d --build database redis
honcho_compose run --rm --no-deps \
  --entrypoint /app/.venv/bin/python \
  api \
  scripts/provision_db.py
honcho_compose run --rm --no-deps \
  --entrypoint /app/.venv/bin/python \
  api \
  scripts/configure_embeddings.py --yes
honcho_compose up -d --build api deriver

if docker ps --format '{{.Names}}' | grep -Fxq "$EMBEDDINGS_CONTAINER"; then
  docker network connect "$HONCHO_NETWORK" "$EMBEDDINGS_CONTAINER" >/dev/null 2>&1 || true
fi

DOCKER_BRIDGE_IP="$(ip -4 addr show docker0 2>/dev/null | awk '/inet / { sub(/\/.*/, "", $2); print $2; exit }')"
if [ -n "$DOCKER_BRIDGE_IP" ] && ! curl -fsS "http://$DOCKER_BRIDGE_IP:8645/health" >/dev/null 2>&1; then
  setsid "$HERMES_PY" -m hermes_cli.main proxy start \
    --provider openai-codex \
    --host "$DOCKER_BRIDGE_IP" \
    --port 8645 \
    >"$PROXY_DOCKER_LOG" 2>&1 < /dev/null &
fi

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

"$HERMES_PY" -m hermes_cli.main honcho status

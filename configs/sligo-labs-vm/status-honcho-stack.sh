#!/usr/bin/env bash
set -euo pipefail

HERMES_REPO="${HERMES_REPO:-/home/droid/hermes}"
HONCHO_REPO="${HONCHO_REPO:-/home/droid/honcho}"
HERMES_PY="${HERMES_PY:-$HERMES_REPO/.venv/bin/python}"
HONCHO_NETWORK="${HONCHO_NETWORK:-honcho_default}"
EMBEDDINGS_CONTAINER="${EMBEDDINGS_CONTAINER:-hermes-honcho-embeddings}"
EMBEDDINGS_MODEL="${EMBEDDINGS_MODEL:-Qwen/Qwen3-Embedding-8B-GGUF:Q8_0}"
EMBEDDINGS_DIMENSIONS="${EMBEDDINGS_DIMENSIONS:-4096}"
HONCHO_EMBEDDINGS_DIMENSIONS="${HONCHO_EMBEDDINGS_DIMENSIONS:-2000}"
EMBEDDINGS_CONTEXT="${EMBEDDINGS_CONTEXT:-40960}"

"$HERMES_PY" -m hermes_cli.main honcho embeddings status \
  --model "$EMBEDDINGS_MODEL" \
  --dimensions "$EMBEDDINGS_DIMENSIONS" \
  --ctx "$EMBEDDINGS_CONTEXT" || true
curl -fsS http://127.0.0.1:8080/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$EMBEDDINGS_MODEL\",\"input\":\"Hermes Honcho embedding status check\"}" \
  | "$HERMES_PY" -c "import json,sys; data=json.load(sys.stdin); dim=len(data['data'][0]['embedding']); expected=$EMBEDDINGS_DIMENSIONS; print(f'host embeddings dimensions: {dim}'); raise SystemExit(0 if dim == expected else 1)"
docker inspect "$EMBEDDINGS_CONTAINER" \
  --format 'embedding container: image={{.Config.Image}} device_requests={{json .HostConfig.DeviceRequests}} state={{.State.Status}}'
nvidia-smi
curl -fsS http://127.0.0.1:8645/health
printf '\n'
DOCKER_BRIDGE_IP="$(ip -4 addr show docker0 2>/dev/null | awk '/inet / { sub(/\/.*/, "", $2); print $2; exit }')"
if [ -n "$DOCKER_BRIDGE_IP" ]; then
  curl -fsS "http://$DOCKER_BRIDGE_IP:8645/health"
  printf '\n'
fi
docker compose \
  -f "$HONCHO_REPO/docker-compose.yml" \
  -f "$HERMES_REPO/configs/sligo-labs-vm/honcho-compose.override.yml" \
  ps
if docker network inspect "$HONCHO_NETWORK" >/dev/null 2>&1; then
  docker network inspect "$HONCHO_NETWORK" --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}' | grep -Fx "$EMBEDDINGS_CONTAINER" >/dev/null
  docker compose \
    -f "$HONCHO_REPO/docker-compose.yml" \
    -f "$HERMES_REPO/configs/sligo-labs-vm/honcho-compose.override.yml" \
    exec -T api /app/.venv/bin/python -c "import json, urllib.request; payload=json.dumps({'model':'$EMBEDDINGS_MODEL','input':'Hermes Honcho container embedding status check'}).encode(); req=urllib.request.Request('http://hermes-honcho-embeddings:8080/v1/embeddings', data=payload, headers={'Content-Type':'application/json'}, method='POST'); data=json.loads(urllib.request.urlopen(req, timeout=30).read().decode()); dim=len(data['data'][0]['embedding']); print('container raw embeddings dimensions:', dim); raise SystemExit(0 if dim == $EMBEDDINGS_DIMENSIONS else 1)"
  docker compose \
    -f "$HONCHO_REPO/docker-compose.yml" \
    -f "$HERMES_REPO/configs/sligo-labs-vm/honcho-compose.override.yml" \
    exec -T api /app/.venv/bin/python - <<PY
import asyncio
from src.embedding_client import embedding_client

async def main():
    emb = await embedding_client.embed("Hermes Honcho truncated embedding status check")
    dim = len(emb)
    print("honcho embedding client dimensions:", dim)
    raise SystemExit(0 if dim == $HONCHO_EMBEDDINGS_DIMENSIONS else 1)

asyncio.run(main())
PY
  docker compose \
    -f "$HONCHO_REPO/docker-compose.yml" \
    -f "$HERMES_REPO/configs/sligo-labs-vm/honcho-compose.override.yml" \
    exec -T api /app/.venv/bin/python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:8645/health', timeout=5).read().decode())"
fi
"$HERMES_PY" -m hermes_cli.main honcho status

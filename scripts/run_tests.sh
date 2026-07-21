#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * A fork-owned smoke suite by default (use --full for inherited upstream coverage)
#   * -n 4 xdist workers (CI has 4 cores; -n auto diverges locally)
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Credential env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running `pytest` outside of
#     our conftest path — e.g. calling pytest on a single file)
#   * Proper venv activation
#
# Usage:
#   scripts/run_tests.sh                     # fork smoke suite
#   scripts/run_tests.sh --full              # inherited non-integration suite
#   scripts/run_tests.sh --smoke -q          # explicit smoke suite + pytest flags
#   scripts/run_tests.sh tests/agent/        # one directory
#   scripts/run_tests.sh tests/agent/test_foo.py::TestClass::test_method
#   scripts/run_tests.sh --tb=long -v        # smoke suite with pytest flags

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
# Works whether this is the main checkout or a worktree.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MAIN_REPO_ROOT=""
if GIT_COMMON_DIR="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"; then
  if [ "$(basename "$GIT_COMMON_DIR")" = ".git" ]; then
    MAIN_REPO_ROOT="$(dirname "$GIT_COMMON_DIR")"
  fi
fi

# ── Activate venv ───────────────────────────────────────────────────────────
# Prefer a .venv in the current tree, fall back to the main checkout's venv
# (useful for worktrees where we don't always duplicate the venv).
VENV=""
CANDIDATE_VENVS=("$REPO_ROOT/.venv" "$REPO_ROOT/venv")
if [ -n "$MAIN_REPO_ROOT" ] && [ "$MAIN_REPO_ROOT" != "$REPO_ROOT" ]; then
  CANDIDATE_VENVS+=("$MAIN_REPO_ROOT/.venv" "$MAIN_REPO_ROOT/venv")
fi
CANDIDATE_VENVS+=("$HOME/.hermes/hermes-agent/venv")
for candidate in "${CANDIDATE_VENVS[@]}"; do
  if [ -f "$candidate/bin/activate" ]; then
    VENV="$candidate"
    break
  fi
done

if [ -n "$VENV" ]; then
  PYTHON="$VENV/bin/python"
elif [ -n "${HERMES_PYTHON:-}" ] && [ -x "$HERMES_PYTHON" ] \
    && "$HERMES_PYTHON" -c 'import pytest' 2>/dev/null; then
  PYTHON="$HERMES_PYTHON"
  echo "▶ no local venv — using HERMES_PYTHON: $PYTHON"
else
  echo "error: no virtualenv found. Checked:" >&2
  printf '  %s\n' "${CANDIDATE_VENVS[@]}" >&2
  echo "  HERMES_PYTHON was unset or did not provide pytest" >&2
  exit 1
fi

# ── Hermetic environment ────────────────────────────────────────────────────
# Mirror what CI does in .github/workflows/tests.yml + what conftest.py does.
# Unset every credential-shaped var currently in the environment.
while IFS='=' read -r name _; do
  case "$name" in
    *_API_KEY|*_TOKEN|*_SECRET|*_PASSWORD|*_CREDENTIALS|*_ACCESS_KEY| \
    *_SECRET_ACCESS_KEY|*_PRIVATE_KEY|*_OAUTH_TOKEN|*_WEBHOOK_SECRET| \
    *_ENCRYPT_KEY|*_APP_SECRET|*_CLIENT_SECRET|*_CORP_SECRET|*_AES_KEY| \
    AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|FAL_KEY| \
    GH_TOKEN|GITHUB_TOKEN)
      unset "$name"
      ;;
  esac
done < <(env)

# Unset HERMES_* behavioral vars too.
unset HERMES_YOLO_MODE HERMES_INTERACTIVE HERMES_QUIET HERMES_TOOL_PROGRESS \
      HERMES_TOOL_PROGRESS_MODE HERMES_MAX_ITERATIONS HERMES_SESSION_PLATFORM \
      HERMES_SESSION_CHAT_ID HERMES_SESSION_CHAT_NAME HERMES_SESSION_THREAD_ID \
      HERMES_SESSION_SOURCE HERMES_SESSION_KEY HERMES_GATEWAY_SESSION \
      HERMES_PROJECT_PATH HERMES_PROJECT_NAME HERMES_PROJECT_GITHUB_URL \
      HERMES_PROJECT_CHANNEL_ID \
      HERMES_CRON_SESSION \
      HERMES_PLATFORM HERMES_INFERENCE_PROVIDER HERMES_MANAGED HERMES_DEV \
      HERMES_CONTAINER HERMES_EPHEMERAL_SYSTEM_PROMPT HERMES_TIMEZONE \
      HERMES_REDACT_SECRETS HERMES_BACKGROUND_NOTIFICATIONS HERMES_EXEC_ASK \
      HERMES_HOME_MODE 2>/dev/null || true

# Pin deterministic runtime.
export TZ=UTC
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONHASHSEED=0

# ── Live-gateway test guard (developer machines) ────────────────────────────
# If a system-wide hermes pytest_live_guard plugin is installed at
# $HOME/.hermes/pytest_live_guard.py, force-load it here so every test run
# from this script gets the protection regardless of which worktree is
# checked out (in-tree tests/conftest.py guard may be missing on stale
# branches). Harmless on CI / fresh machines that don't have the file.
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  case ":${PYTHONPATH:-}:" in
    *":$HOME/.hermes:"*) ;;
    *) export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$HOME/.hermes" ;;
  esac
  if [[ ",${PYTEST_PLUGINS:-}," != *,pytest_live_guard,* ]]; then
    export PYTEST_PLUGINS="${PYTEST_PLUGINS:+$PYTEST_PLUGINS,}pytest_live_guard"
  fi
fi

# ── Worker count ────────────────────────────────────────────────────────────
# CI uses `-n auto` on ubuntu-latest which gives 4 workers. A 20-core
# workstation with `-n auto` gets 20 workers and exposes test-ordering
# flakes that CI will never see. Pin to 4 so local matches CI.
WORKERS="${HERMES_TEST_WORKERS:-4}"

# ── Run pytest ──────────────────────────────────────────────────────────────
cd "$REPO_ROOT"

# Mode selection:
#   no args / flags only  -> fork-owned smoke suite
#   --smoke              -> fork-owned smoke suite
#   --full               -> inherited non-integration suite
#   test paths/nodeids    -> targeted run
MODE="custom"
if [ "$#" -eq 0 ]; then
  MODE="smoke"
elif [ "${1:-}" = "--smoke" ]; then
  MODE="smoke"
  shift
elif [ "${1:-}" = "--full" ]; then
  MODE="full"
  shift
elif [[ "${1:-}" == -* ]]; then
  MODE="smoke"
  for arg in "$@"; do
    case "$arg" in
      tests/*|*::test_*|*.py)
        MODE="custom"
        break
        ;;
    esac
  done
fi

ARGS=("$@")
PYTEST_ARGS=(
  -o "addopts="
  -n "$WORKERS"
  --ignore=tests/integration
  --ignore=tests/e2e
  -m "not integration"
)

if [ "$MODE" = "smoke" ]; then
  SMOKE_SUITE_FILE="$REPO_ROOT/scripts/test_suites/smoke.txt"
  if [ ! -f "$SMOKE_SUITE_FILE" ]; then
    echo "error: smoke suite file not found: $SMOKE_SUITE_FILE" >&2
    exit 1
  fi

  SMOKE_TARGETS=()
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ""|\#*) continue ;;
      *) SMOKE_TARGETS+=("$line") ;;
    esac
  done < "$SMOKE_SUITE_FILE"

  if [ "${#SMOKE_TARGETS[@]}" -eq 0 ]; then
    echo "error: smoke suite file is empty: $SMOKE_SUITE_FILE" >&2
    exit 1
  fi

  PYTEST_ARGS+=("${SMOKE_TARGETS[@]}")
elif [ "$MODE" = "custom" ]; then
  PYTEST_ARGS+=("${ARGS[@]}")
elif [ "$MODE" != "full" ]; then
  echo "error: unknown test mode: $MODE" >&2
  exit 1
fi

if [ "$MODE" = "full" ]; then
  PYTEST_ARGS+=("${ARGS[@]}")
elif [ "$MODE" = "smoke" ]; then
  PYTEST_ARGS+=("${ARGS[@]}")
fi

echo "▶ running $MODE pytest suite with $WORKERS workers, hermetic env, in $REPO_ROOT"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; all credential env vars unset)"
if [ "$MODE" = "full" ]; then
  echo "  full mode includes inherited upstream non-integration tests"
elif [ "$MODE" = "smoke" ]; then
  echo "  smoke suite: scripts/test_suites/smoke.txt"
fi

# -o "addopts=" clears pyproject.toml's `-n auto` so our -n wins.
exec "$PYTHON" -m pytest "${PYTEST_ARGS[@]}"

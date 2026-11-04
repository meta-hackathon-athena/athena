#!/usr/bin/env bash
# Install the whole Vixy backend from this checkout (all components live in components/):
# the Python environment (uv workspace, one uv.lock), .env, FalkorDB, then a smoke test.
#
#   ./scripts/bootstrap.sh               # core backend
#   --perception   also install the MP4 extra (YOLO/Whisper; needs ffmpeg) and smoke-test MP4 replay
#   --no-docker    no FalkorDB server; use the embedded single-process graph
#   --no-smoke     skip the integration smoke test
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PERCEPTION=0 DOCKER=1 SMOKE=1
for arg in "$@"; do
  case "$arg" in
    --perception) PERCEPTION=1 ;;
    --no-docker) DOCKER=0 ;;
    --no-smoke) SMOKE=0 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option $arg (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'bootstrap: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "$1 not found on PATH ($2)"; }

need uv "brew install uv, or https://docs.astral.sh/uv/getting-started/installation/"
if [ $PERCEPTION = 1 ]; then need ffmpeg "brew install ffmpeg"; need ffprobe "brew install ffmpeg"; fi

cd "$ROOT"
EXTRA=()
[ $PERCEPTION = 1 ] && EXTRA=(--extra perception)
say "python environment (uv sync --frozen ${EXTRA[*]:-}): runtime + components/context-engine + components/dreamer-model"
uv sync --frozen ${EXTRA[@]+"${EXTRA[@]}"}

if [ ! -f .env ]; then
  cp .env.example .env
  echo "  created .env from .env.example (placeholders only; it is gitignored, keep real keys out of git)"
fi

if [ $DOCKER = 1 ]; then
  need docker "https://docs.docker.com/get-docker/, or rerun with --no-docker"
  say "FalkorDB (docker compose, localhost:${VIXY_FALKORDB_PORT:-6390})"
  docker compose up -d --wait falkordb
  export FALKORDB_URL="redis://localhost:${VIXY_FALKORDB_PORT:-6390}"
else
  export FALKORDB_URL=""            # an explicit empty value wins over .env: embedded graph
  # later `uv run python -m vixy ...` commands read .env, so point it at the embedded graph too
  if grep -q '^FALKORDB_URL=redis://localhost:6390$' .env; then
    sed -i.bak 's|^FALKORDB_URL=redis://localhost:6390$|FALKORDB_URL=|' .env && rm -f .env.bak
    echo "  .env: FALKORDB_URL emptied (embedded graph, no Docker)"
  fi
fi

if [ $SMOKE = 1 ]; then
  say "integration smoke test"
  if [ $PERCEPTION = 1 ]; then
    uv run --frozen --extra perception python scripts/smoke_test.py --mp4
  else
    uv run --frozen python scripts/smoke_test.py
  fi
fi

say "ready ($(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo 'no git'))"
echo "  serve the API:   make serve VIXY_USER=app_demo   (see docs/FRONTEND_INTEGRATION.md)"
echo "  run the tests:   make test | make test-integration | make smoke-av"

#!/usr/bin/env bash
# Test levels for the whole monorepo (also used by CI). Offline: model keys are blanked, no paid calls.
#
#   scripts/test.sh unit         Context Engine, Dreamer and runtime/agent/memory suites (embedded graph)
#   scripts/test.sh integration  shared-FalkorDB variants, agent scenarios, M1 + M2 demos, HTTP smoke test
#   scripts/test.sh av           M3 audiovisual: generated MP4 -> YOLO + Whisper -> replay acceptance
#                                (needs `uv sync --extra perception` and ffmpeg; downloads public models once)
#   scripts/test.sh all          unit + integration + av
#
# Integration and av use $VIXY_TEST_FALKORDB_URL (default redis://localhost:6390, `docker compose up -d`).
# Every user they create is new and disposable (it_*, smoke_*, m3_av_*); their graphs are deleted at the
# end, so vixy_demo and other graphs are never touched. Output is also written to logs/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LEVEL=${1:-unit}
case "$LEVEL" in unit|integration|av|all) ;; *) sed -n '2,13p' "$0"; exit 2 ;; esac

mkdir -p logs
LOG="logs/test-$LEVEL-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee "$LOG") 2>&1

export MODEL_API_KEY="" BASETEN_API_KEY="" VIXY_LLM_MODEL=""
SERVER=${VIXY_TEST_FALKORDB_URL:-redis://localhost:6390}
RAND=$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 8 || true)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/vixy-test.XXXXXX")
USERS=()
FAILED=()

cleanup() {
  if [ ${#USERS[@]} -gt 0 ]; then
    uv run --frozen python - "$SERVER" "${USERS[@]}" <<'EOF' || true
import sys
from falkordb import FalkorDB
db = FalkorDB.from_url(sys.argv[1])
existing = set(db.list_graphs())
for user in sys.argv[2:]:
    assert user.startswith(("it_", "m3_av_")), user
    if f"vixy_{user}" in existing:
        db.select_graph(f"vixy_{user}").delete()
EOF
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

step() {  # <name> <command...>
  local name=$1; shift
  printf '\n==> %s\n' "$name"
  if "$@"; then echo "--- PASS  $name"; else echo "--- FAIL  $name"; FAILED+=("$name"); fi
}

server_up() {
  uv run --frozen python -c "import sys; from falkordb import FalkorDB; FalkorDB.from_url(sys.argv[1]).list_graphs()" \
    "$SERVER" 2>/dev/null || { echo "FalkorDB not reachable at $SERVER (docker compose up -d --wait falkordb)"; return 1; }
}

unit() {
  step "context-engine tests" env FALKORDB_URL= uv run --frozen pytest -q components/context-engine
  step "dreamer-model tests" env FALKORDB_URL= uv run --frozen pytest -q components/dreamer-model
  step "runtime, agent and memory tests (embedded)" env FALKORDB_URL= uv run --frozen pytest -q
}

integration() {
  server_up || { FAILED+=("FalkorDB server"); return; }
  local m1="it_m1_$RAND" m2="it_m2_$RAND"
  USERS+=("$m1" "$m2")
  step "runtime tests incl. shared-server variants" env FALKORDB_URL= VIXY_TEST_FALKORDB_URL="$SERVER" uv run --frozen pytest -q
  step "active agent scenarios (14)" uv run --frozen python -m agent.cli eval
  step "M1 demo (disposable user $m1)" env FALKORDB_URL="$SERVER" \
    uv run --frozen python -m vixy --user "$m1" --state-dir "$TMP/$m1" demo --fresh
  step "M2 demo (disposable user $m2)" env FALKORDB_URL="$SERVER" \
    uv run --frozen python -m vixy --user "$m2" --state-dir "$TMP/$m2" demo-m2 --fresh
  step "HTTP /v1 smoke test" env FALKORDB_URL="$SERVER" uv run --frozen python scripts/smoke_test.py
}

av() {
  command -v ffmpeg >/dev/null || { echo "ffmpeg not on PATH"; FAILED+=("ffmpeg"); return; }
  local user="m3_av_$RAND" fixture="$TMP/fixture" output="$TMP/output" server=""
  server_up && server="$SERVER" || echo "(server variant skipped)"
  USERS+=("$user")
  mkdir -p demo/outputs/.smoke-models "$output"
  ln -s "$ROOT/demo/outputs/.smoke-models" "$output/models"      # model weights cached across runs
  step "generate synthetic MP4 fixture" uv run --frozen --extra perception python -m vixy fixture \
    --directory "$fixture" --fixture-user "$user"
  step "process MP4 with local YOLO + Whisper" uv run --frozen --extra perception python -m vixy process \
    --scenario "$fixture/scenario.json" --output "$output"
  step "M3 acceptance, media and perception tests" env FALKORDB_URL= VIXY_TEST_FALKORDB_URL="$server" \
    VIXY_M3_SCENARIO="$fixture/scenario.json" VIXY_M3_OUTPUT="$output" VIXY_M3_MODEL_DIR="$output/models" \
    uv run --frozen --extra perception pytest -q tests/test_m3.py tests/test_m3_acceptance.py \
    tests/test_m3_media.py tests/test_perception_intervals.py
  step "MP4 replay smoke test" env FALKORDB_URL="$server" \
    uv run --frozen --extra perception python scripts/smoke_test.py --mp4
}

case "$LEVEL" in
  unit) unit ;;
  integration) integration ;;
  av) av ;;
  all) unit; integration; av ;;
esac

echo
if [ ${#FAILED[@]} -gt 0 ]; then
  printf 'FAILED (%d): %s\nlog: %s\n' "${#FAILED[@]}" "$(IFS=';'; echo "${FAILED[*]}")" "$LOG"
  exit 1
fi
echo "all $LEVEL checks passed; log: $LOG"

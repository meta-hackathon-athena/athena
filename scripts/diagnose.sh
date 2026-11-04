#!/usr/bin/env bash
# Environment diagnostics for bug reports: versions, FalkorDB reachability and graph names, which
# .env keys are set (names only, never values). Written to logs/diagnose-<time>.txt.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
OUT="logs/diagnose-$(date +%Y%m%d-%H%M%S).txt"
exec > >(tee "$OUT") 2>&1

echo "## repository"
git rev-parse HEAD 2>/dev/null && git status --short | head -20
echo "## tools"
for t in uv docker ffmpeg ffprobe; do printf '%-8s %s\n' "$t" "$(command -v "$t" >/dev/null && "$t" --version 2>&1 | head -1 || echo missing)"; done
echo "## python environment"
uv run --frozen python - <<'EOF'
import importlib.metadata as md, sys
print("python", sys.version.split()[0])
for p in ("vixy-context-engine", "prod-dreamer-model", "falkordb", "falkordblite", "ultralytics", "openai-whisper", "torch"):
    try:
        print(f"{p:20} {md.version(p)}")
    except md.PackageNotFoundError:
        print(f"{p:20} not installed")
EOF
echo "## .env (key names only)"
if [ -f .env ]; then sed -nE 's/^([A-Z_][A-Z0-9_]*)=.+$/\1 (set)/p' .env; else echo "no .env"; fi
echo "## FalkorDB ${FALKORDB_URL:-redis://localhost:6390}"
docker compose ps falkordb 2>/dev/null
uv run --frozen python -c "
import sys
from falkordb import FalkorDB
print('graphs:', ', '.join(sorted(FalkorDB.from_url(sys.argv[1]).list_graphs())) or '(none)')
" "${FALKORDB_URL:-redis://localhost:6390}" 2>&1 | tail -1
echo "## local data (sizes)"
du -sh data demo/outputs logs 2>/dev/null
echo
echo "written to $OUT; per-replay monitors are demo/outputs/<user>/replay.jsonl and summary.json"

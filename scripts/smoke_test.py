"""Integration smoke test for a fresh Vixy checkout (no paid model calls, disposable state only).

    uv run python scripts/smoke_test.py            # imports + CE -> Episode Writer -> Agent -> graph -> Dreamer -> /v1 HTTP
    uv run --extra perception python scripts/smoke_test.py --mp4   # also MP4 -> YOLO/Whisper -> replay (local models)

Uses $FALKORDB_URL when set (shared server), else an embedded graph in a temp directory. Every
user it creates is new and random (smoke_* / m3_smoke_*); its graphs are deleted at the end, so
vixy_demo and other graphs are never touched. Model weights for --mp4 are cached in
demo/outputs/.smoke-models (ignored by git).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
import uuid
from pathlib import Path

# Explicit empty values win over every .env file: the smoke test never calls a paid model.
os.environ.update(MODEL_API_KEY="", BASETEN_API_KEY="", VIXY_LLM_MODEL="")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))                           # this repo is not an installed package (like pytest)
import vixy.config  # noqa: E402,F401  (loads .env into os.environ, keeping the empty keys above)

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f' ({detail})' if detail else ''}", flush=True)
    return bool(ok)


def imports() -> None:
    import agent, context_engine, memory, prod_dreamer_model, vixy  # noqa: E401,F401
    for mod, component in ((context_engine, "context-engine"), (prod_dreamer_model, "dreamer-model")):
        path = Path(mod.__file__).resolve()
        check(f"{mod.__name__} imports from components/{component}",
              (ROOT / "components" / component) in path.parents, os.path.relpath(path, ROOT))
    check("agent, memory and vixy import", True)


def http(base: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(base + path, method=method, headers={"Content-Type": "application/json"},
                                 data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def backend(tmp: Path) -> str:
    """demo-m2 (two days, Dreamer, restart) for a new user, then the real HTTP server over its state."""
    from vixy.api import serve
    from vixy.cli import main as vixy
    from vixy.config import VixyConfig
    from vixy.runtime import VixyRuntime

    user, state = f"smoke_{uuid.uuid4().hex[:8]}", tmp / "state"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = vixy(["--user", user, "--state-dir", str(state), "demo-m2", "--fresh"])
    summary = [line for line in out.getvalue().splitlines() if "checks passed" in line]
    check("demo-m2 pipeline: CE -> Episode Writer -> Agent -> graph -> Dreamer -> ask", code == 0,
          summary[-1].strip() if summary else "no summary")
    if code:
        print("\n".join(l for l in out.getvalue().splitlines() if "FAIL" in l))

    with VixyRuntime(VixyConfig.from_env(user_id=user).under(state)) as rt:
        server = serve(rt, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}/v1"
        try:
            h = http(base, "GET", "/health")
            check("GET /v1/health", h.get("ok") is True and h.get("graph") == f"vixy_{user}", h.get("graph_schema", ""))
            notes = http(base, "GET", "/decisions?action=NOTIFY&delivered=1")["decisions"]
            check("GET /v1/decisions?action=NOTIFY&delivered=1 returns notifications", len(notes) > 0, f"{len(notes)}")
            with_memory = [d for d in notes if d.get("supporting_memory_ids")]
            if check("a notification is backed by a long-term memory", bool(with_memory)):
                d = http(base, "GET", f"/decisions/{with_memory[0]['id']}")["decision"]
                check("GET /v1/decisions/{id} explains it with evidence", bool(d["memories"][0]["evidence"]))
                m = http(base, "GET", f"/memories/{with_memory[0]['supporting_memory_ids'][0]}")["memory"]
                check("GET /v1/memories/{id} evidence matches the original CE event",
                      any((e.get("original") or {}).get("matches") for e in m["evidence"]))
            a = http(base, "POST", "/ask", {"question": "What do you remember about Alex?"})["result"]
            check("POST /v1/ask answers with verified sources",
                  a["status"] == "answered" and any(s.get("verified") for s in a["sources"]), a["answered_by"])
            t = http(base, "GET", "/timeline?date=2026-11-02")
            check("GET /v1/timeline", len(t["items"]) > 0, f"{len(t['items'])} items")
        finally:
            server.shutdown()
            server.server_close()
    return user


def mp4(tmp: Path) -> str:
    """Generated fixture -> local YOLO + Whisper -> cache -> replay through the real backend."""
    user = f"m3_smoke_{uuid.uuid4().hex[:8]}"
    inputs, output = tmp / "inputs", tmp / "outputs"
    models = ROOT / "demo/outputs/.smoke-models"
    models.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True)
    (output / "models").symlink_to(models, target_is_directory=True)
    manifest = inputs / "scenario.json"

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "vixy", *args], cwd=ROOT, capture_output=True, text=True)

    steps = [("fixture", "--directory", str(inputs), "--fixture-user", user),
             ("process", "--scenario", str(manifest), "--output", str(output)),
             ("replay", "--scenario", str(manifest), "--output", str(output), "--fresh",
              "--question", "What do you remember about Alex?")]
    for step in steps:
        p = run(*step)
        if not check(f"vixy {step[0]}", p.returncode == 0,
                     "" if p.returncode == 0 else (p.stderr.strip().splitlines() or [""])[-1]):
            return user
    s = json.loads((output / "summary.json").read_text())
    check("replay: every MP4 observation processed", set(s["outcomes"]) == {"processed"}, json.dumps(s["outcomes"]))
    check("replay: Dreamer consolidated a memory", len(s["memories"]) > 0, f"{len(s['memories'])}")
    check("replay: ask answered from the replayed graph", (s["answer"] or {}).get("status") == "answered")
    check("replay: zero paid API calls", s["paid_api_calls"] == 0)
    return user


def cleanup(users: list[str]) -> None:
    url = os.getenv("FALKORDB_URL")
    if not url:
        return                                          # embedded graphs live in the deleted temp dir
    from falkordb import FalkorDB
    db = FalkorDB.from_url(url)
    for user in users:
        assert user.startswith(("smoke_", "m3_smoke_"))
        if f"vixy_{user}" in db.list_graphs():
            db.select_graph(f"vixy_{user}").delete()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mp4", action="store_true", help="also run the MP4 perception path (needs --extra perception, ffmpeg)")
    a = ap.parse_args()
    print(f"Vixy smoke test — storage: {os.getenv('FALKORDB_URL') or 'embedded (temp dir)'}")
    users: list[str] = []
    with tempfile.TemporaryDirectory(prefix="vixy-smoke-") as d:
        try:
            imports()
            users.append(backend(Path(d) / "backend"))
            if a.mp4:
                users.append(mp4(Path(d) / "mp4"))
        except Exception as e:                          # report, still clean up
            check("smoke test ran to completion", False, f"{type(e).__name__}: {e}")
        finally:
            cleanup(users)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} smoke checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""FalkorDB connection: a real server if FALKORDB_URL (or legacy FALKORDB_HOST) is set,
else embedded (falkordblite).

Graphs are named ``<prefix>_<user_id>`` with prefix ``vixy`` (VIXY_GRAPH_PREFIX).
Set VIXY_GRAPH_PREFIX=veyra to open graphs written before the rename. The
embedded file is VIXY_DB_PATH (legacy VEYRA_DB_PATH, default data/vixy.rdb).
Embedded mode is one process only; runtime and Dreamer share state through a
server (docker compose up -d falkordb).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@lru_cache(maxsize=None)
def _client(db_path: str | None = None):
    url = os.getenv("FALKORDB_URL")
    if url:
        from falkordb import FalkorDB
        return FalkorDB.from_url(url)
    host = os.getenv("FALKORDB_HOST")
    if host:
        from falkordb import FalkorDB
        return FalkorDB(host=host, port=int(os.getenv("FALKORDB_PORT", "6379")),
                        password=os.getenv("FALKORDB_PASSWORD") or None)
    from redislite.falkordb_client import FalkorDB as EmbeddedFalkorDB
    path = Path(db_path or os.getenv("VIXY_DB_PATH") or os.getenv("VEYRA_DB_PATH") or "data/vixy.rdb")
    path.parent.mkdir(parents=True, exist_ok=True)
    return EmbeddedFalkorDB(str(path))


def graph_name(user_id: str, prefix: str | None = None) -> str:
    """'demo' -> 'vixy_demo'. Only [A-Za-z0-9_-] survive, so ids can't address other graphs."""
    safe = "".join(ch for ch in user_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError("invalid user_id")
    return f"{prefix or os.getenv('VIXY_GRAPH_PREFIX') or 'vixy'}_{safe}"


def graph_for(user_id: str, db_path: str | None = None):
    """One graph per user: queries can never reach another user's memory."""
    return _client(db_path).select_graph(graph_name(user_id))

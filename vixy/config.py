"""One configuration for the integrated runtime and the Dreamer job.

Identity (see docs/CONTRACTS.md):
  VIXY_USER_ID       the single user of this deployment (default 'demo'); graph = vixy_<user_id>
  VIXY_SESSION_IDS   context-engine session ids owned by that user, comma separated
                     (default: the user id itself). Events from any other session are rejected
                     before they reach Context Engine, so they can never write this user's graph.
Storage:
  FALKORDB_URL       shared FalkorDB server, e.g. redis://localhost:6390 (docker compose).
                     Unset = embedded falkordblite file VIXY_DB_PATH (one process only).
  VIXY_EVENTS_DB     Context Engine SQLite (canonical raw event store)
  VIXY_RUNTIME_DB    runtime processing ledger (which events have had their side effects)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from prod_dreamer_model.config import _load_env_files

_load_env_files()

from memory.db import graph_name as _graph_name

DEFAULT_USER = "demo"


def _env(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v is not None and v.strip():
            return v.strip()
    return None


@dataclass(frozen=True)
class VixyConfig:
    user_id: str = DEFAULT_USER
    session_ids: tuple[str, ...] = ()
    tz: str = "Asia/Singapore"
    falkordb_url: str | None = None
    db_path: str = "data/vixy.rdb"
    events_db: str = "data/vixy_events.sqlite3"
    runtime_db: str = "data/vixy_runtime.sqlite3"
    graph_prefix: str = "vixy"
    # Context window the agent receives (ContextSnapshot)
    context_window_s: float = 300.0
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _graph_name(self.user_id, self.graph_prefix)              # validates the id
        if not self.session_ids:
            object.__setattr__(self, "session_ids", (self.user_id,))

    @classmethod
    def from_env(cls, **overrides: Any) -> "VixyConfig":
        _load_env_files()
        sessions = _env("VIXY_SESSION_IDS")
        env: dict[str, Any] = {
            "user_id": _env("VIXY_USER_ID") or DEFAULT_USER,
            "session_ids": tuple(s.strip() for s in sessions.split(",") if s.strip()) if sessions else (),
            "tz": _env("VIXY_TZ", "VEYRA_TZ") or "Asia/Singapore",
            "falkordb_url": _env("FALKORDB_URL"),
            "db_path": _env("VIXY_DB_PATH", "VEYRA_DB_PATH") or "data/vixy.rdb",
            "events_db": _env("VIXY_EVENTS_DB") or "data/vixy_events.sqlite3",
            "runtime_db": _env("VIXY_RUNTIME_DB") or "data/vixy_runtime.sqlite3",
            "graph_prefix": _env("VIXY_GRAPH_PREFIX") or "vixy",
            "context_window_s": float(_env("VIXY_CONTEXT_WINDOW_S") or 300.0),
        }
        env.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**env)

    def under(self, state_dir: str | Path) -> "VixyConfig":
        """Same identity, all local state files inside state_dir (demo / tests)."""
        d = Path(state_dir)
        return replace(self, db_path=str(d / "vixy.rdb"), events_db=str(d / "vixy_events.sqlite3"),
                       runtime_db=str(d / "vixy_runtime.sqlite3"))

    @property
    def graph_name(self) -> str:
        return _graph_name(self.user_id, self.graph_prefix)

    @property
    def default_session(self) -> str:
        return self.session_ids[0]

    def owns_session(self, session_id: str) -> bool:
        return session_id in self.session_ids

    @property
    def storage(self) -> str:
        return f"server {self.falkordb_url}" if self.falkordb_url else f"embedded {self.db_path}"


def connect_graph(config: VixyConfig):
    """The user's graph on the shared server, or in the embedded file.

    Embedded clients are cached per file (memory.db._client), so the runtime and an
    in-process Dreamer job get the same FalkorDB instance. Across processes only the
    server is shared storage.
    """
    if config.falkordb_url:
        from falkordb import FalkorDB
        return FalkorDB.from_url(config.falkordb_url).select_graph(config.graph_name)
    path = Path(config.db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return _embedded(str(path)).select_graph(config.graph_name)


@lru_cache(maxsize=None)
def _embedded(path: str):
    # not memory.db._client: that one prefers $FALKORDB_URL, and an explicit
    # embedded config must never silently land on a server
    from redislite.falkordb_client import FalkorDB as EmbeddedFalkorDB
    return EmbeddedFalkorDB(path)

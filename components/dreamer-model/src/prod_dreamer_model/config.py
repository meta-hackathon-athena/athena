"""Runtime configuration for the production dreamer.

Values come from the environment (the repository ``.env`` then a package-local
``.env``), and can be overridden per run by the CLI. Nothing here performs IO
beyond reading those files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PKG_DIR = Path(__file__).resolve().parent
# Works in both layouts: installed/src (<repo>/src/prod_dreamer_model) and flat
# (<repo>/prod_dreamer_model).
REPO_ROOT = PKG_DIR.parents[1] if PKG_DIR.parent.name == "src" else PKG_DIR.parent
ROOT = REPO_ROOT  # alias kept for older imports
# In the Vixy monorepo this package lives at <vixy>/components/dreamer-model.
MONOREPO_ROOT = REPO_ROOT.parents[1] if REPO_ROOT.parent.name == "components" else None


def _load_env_files() -> None:
    """First file wins; load_dotenv never overrides an existing variable."""
    candidates = [
        os.getenv("VIXY_ENV_FILE"),
        os.getenv("DREAMER_ENV_FILE"),
        # Shared monorepo configuration, including standalone Dreamer invocations.
        MONOREPO_ROOT / ".env" if MONOREPO_ROOT else None,
        PKG_DIR / ".env",
        REPO_ROOT / ".env",
        # Monorepo convenience: when this package sits inside a larger checkout
        # (e.g. metahackathon/prod_dreamer_model) reuse that checkout's .env.
        # A standalone clone simply won't have this file.
        REPO_ROOT.parent / ".env",
    ]
    for candidate in candidates:
        if candidate:
            load_dotenv(candidate)


_load_env_files()

DEFAULT_GRAPH = "vixy_dreamer"          # legacy standalone graph (graph_layout="legacy")
DEFAULT_SESSION = "wearer-01"
# Non-contributor by default: per Meta's model docs, "-contributor" models let
# Meta use submitted content for training. Real wearable data (which includes
# other people's speech) must never be sent there; see allow_contributor_model.
DEFAULT_MODEL = "muse-spark-1.3"
DEFAULT_MUSE_URL = "https://api.meta.ai/v1/responses"
DEFAULT_TZ = "Asia/Singapore"
GRAPH_LAYOUTS = ("legacy", "unified")


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def unified_graph_name(user_id: str) -> str:
    """The shared per-user Vixy graph: ``vixy_<user_id>`` (same rule as master-repo)."""
    safe = "".join(ch for ch in user_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError("invalid user_id")
    return f"vixy_{safe}"


def is_contributor_model(model: str) -> bool:
    return model.strip().lower().endswith("-contributor")


@dataclass(frozen=True)
class DreamerConfig:
    """Everything the dreamer needs; no globals are read after construction."""

    # FalkorDB
    graph_name: str = DEFAULT_GRAPH
    falkordb_url: str | None = None
    embedded_path: str = "data/prod_dreamer.rdb"

    # context-engine / identity
    session_id: str = DEFAULT_SESSION
    user_id: str = DEFAULT_SESSION
    events_db: str = "data/prod_dreamer_events.sqlite3"
    tz: str = DEFAULT_TZ

    # Muse (Meta Responses API)
    model_api_key: str | None = None
    muse_url: str = DEFAULT_MUSE_URL
    muse_model: str = DEFAULT_MODEL
    # Tool-call chaining with previous_response_id requires store=true.
    store_responses: bool = True
    # Refuse "-contributor" models unless explicitly allowed (synthetic data only).
    allow_contributor_model: bool = False
    max_turns_per_round: int = 10
    max_rounds: int = 4
    reasoning_effort: str = "low"
    max_output_tokens: int = 8192
    request_timeout_s: int = 180

    # Cypher tool limits
    row_cap: int = 200
    query_timeout_ms: int = 15_000

    # "legacy": the standalone Day/Frame/Class graph this package was built on.
    # "unified": the shared per-user Vixy graph (Visit/Note/Place/Tag/Person +
    # agent nodes), where the dreamer never writes Frame nodes or resets.
    graph_layout: str = "legacy"
    # backfill_evidence() links beliefs to frames/days matched from the belief's
    # own claimed window, which is inferred, not recorded, provenance. Off by
    # default; beliefs left without evidence are marked "unconfirmed" instead.
    allow_inferred_evidence: bool = False

    @classmethod
    def from_env(cls, **overrides: Any) -> "DreamerConfig":
        """Build a config from the environment, then apply non-None overrides."""
        _load_env_files()
        layout = (os.getenv("DREAMER_GRAPH_LAYOUT") or "legacy").strip().lower()
        user_id = os.getenv("DREAMER_USER_ID") or os.getenv("VIXY_USER_ID") or DEFAULT_SESSION
        default_graph = unified_graph_name(user_id) if layout == "unified" else DEFAULT_GRAPH
        env: dict[str, Any] = {
            "graph_name": os.getenv("DREAMER_GRAPH_NAME") or default_graph,
            "falkordb_url": os.getenv("FALKORDB_URL") or None,
            "embedded_path": os.getenv("DREAMER_DB_PATH") or "data/prod_dreamer.rdb",
            "session_id": os.getenv("DREAMER_SESSION_ID") or DEFAULT_SESSION,
            "user_id": user_id,
            "events_db": os.getenv("DREAMER_EVENTS_DB") or "data/prod_dreamer_events.sqlite3",
            "tz": os.getenv("DREAMER_TZ") or os.getenv("VIXY_TZ") or DEFAULT_TZ,
            "model_api_key": os.getenv("MODEL_API_KEY") or None,
            "muse_url": os.getenv("MUSE_URL") or DEFAULT_MUSE_URL,
            "muse_model": os.getenv("MUSE_MODEL") or DEFAULT_MODEL,
            "store_responses": _flag("MUSE_STORE_RESPONSES", True),
            "allow_contributor_model": _flag("DREAMER_ALLOW_CONTRIBUTOR_MODEL"),
            "graph_layout": layout,
            "allow_inferred_evidence": _flag("DREAMER_ALLOW_INFERRED_EVIDENCE"),
        }
        env = {k: v for k, v in env.items() if v is not None}
        env.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**env)

    def with_overrides(self, **overrides: Any) -> "DreamerConfig":
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)

    def __post_init__(self) -> None:
        if self.graph_layout not in GRAPH_LAYOUTS:
            raise ValueError(f"graph_layout must be one of {GRAPH_LAYOUTS}")

    @property
    def has_model(self) -> bool:
        return bool(self.model_api_key)

    @property
    def unified(self) -> bool:
        return self.graph_layout == "unified"

    def check_model_policy(self) -> None:
        """Raise before any request if the configured model may train on our data."""
        if is_contributor_model(self.muse_model) and (self.unified or not self.allow_contributor_model):
            raise ValueError(
                f"{self.muse_model!r} is a contributor model (Meta may train on submitted "
                "content). Use a non-contributor model for real recordings, or set "
                "DREAMER_ALLOW_CONTRIBUTOR_MODEL=1 for synthetic data only."
            )

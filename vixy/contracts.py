"""Shared conventions between Context Engine, the runtime, the agent and the Dreamer.

Context Engine's v1 contracts stay unchanged (extra="forbid"); the fields it has
no slot for travel in its free-form ``source`` metadata under one versioned,
validated shape, ``vixy.source.v1`` (this is how the Dreamer's frame adapter
already carried counts). Full description: docs/CONTRACTS.md.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SOURCE_SCHEMA = "vixy.source.v1"
GRAPH_SCHEMA_VERSION = "vixy.graph.v2"
# graphs this runtime can upgrade with `python -m vixy migrate` (additive, explicit, never silent)
MIGRATABLE_FROM = ("vixy.graph.v1",)
ASK_SCHEMA = "vixy.ask.v1"
API_VERSION = "v1"
UNKNOWN = "unknown"

SpeakerRole = Literal["wearer", "other", "unknown"]


class SourceMeta(BaseModel):
    """``event.source`` for events produced by Vixy adapters.

    Unknown keys are allowed (upstream pipelines may add their own), the keys
    below are typed. ``speaker_is_wearer`` is only ever set from an explicit
    upstream flag; it is never inferred from ``speaker_id``.
    """

    model_config = ConfigDict(extra="allow")

    schema_: Literal["vixy.source.v1"] = Field(SOURCE_SCHEMA, alias="schema")
    user_id: str = Field(min_length=1)
    adapter: str
    pipeline: str
    stream: str
    frame_id: Optional[str] = None
    counts: Optional[dict[str, int]] = None
    segment_id: Optional[str] = None
    recording_id: Optional[str] = None
    speaker_is_wearer: Optional[bool] = None
    language: Optional[str] = None


def source_dict(**fields) -> dict:
    """Validated ``vixy.source.v1`` dict without None values (JSON-safe for CE)."""
    meta = SourceMeta(**fields)
    return {k: v for k, v in meta.model_dump(by_alias=True).items() if v is not None}


def speaker_role(source: dict | None) -> SpeakerRole:
    flag = (source or {}).get("speaker_is_wearer")
    if flag is True:
        return "wearer"
    if flag is False:
        return "other"
    return "unknown"


def speaker_is_wearer(source: dict | None) -> bool | None:
    flag = (source or {}).get("speaker_is_wearer")
    return flag if isinstance(flag, bool) else None

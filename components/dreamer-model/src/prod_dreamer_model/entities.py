"""Conservative person resolution for the unified graph.

The only identity signal speech gives us is the name as heard. The rules:

* Every bare first name ("Marta") is one entity, ``per_marta`` (the Milestone 1 id),
  so all observations about "Marta" link to the same Person.
* A full name ("Marta Lopez") is its own entity, ``per_marta-lopez``.
* A bare first name is never merged into a full name, even if only one exists:
  that would be a merge on a shared first name. Instead the bare entity gets
  ``POSSIBLY_SAME_AS`` edges to each full-name candidate and status
  ``provisional`` (one candidate) or ``ambiguous`` (several). Retrieval for
  "Marta" then returns all candidates and says that identity is uncertain.
* Different full names with the same first name are different people.

Resolution is recomputed from all verified mentions on each run, so it does not
depend on the order notes arrived in, and a re-run gives the same ids.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .semantic import Mention


def person_key(first: str, last: str | None = None) -> str:
    raw = f"{first} {last}" if last else first
    return "per_" + (re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-") or "unknown")


@dataclass
class Entity:
    id: str
    name: str
    first_name: str
    last_name: str | None = None
    status: str = "resolved"                     # resolved | provisional | ambiguous
    candidates: list[str] = field(default_factory=list)
    mentions: int = 0

    @property
    def surface(self) -> str:
        """How a Mention spells this entity (the key of ``resolve``'s result)."""
        return self.name

    def props(self) -> dict:
        out = {"name": self.name, "first_name": self.first_name, "status": self.status,
               "aliases": sorted({self.name.lower(), self.first_name.lower()}),
               "mention_count": self.mentions, "resolution": "name_as_heard.v2"}
        if self.last_name:
            out["last_name"] = self.last_name
        return out


def resolve(mentions: list[Mention]) -> dict[str, Entity]:
    """mention.surface -> Entity, for every non-user mention."""
    by_first: dict[str, list[Mention]] = defaultdict(list)
    for m in mentions:
        if not m.is_user:
            by_first[m.first].append(m)
    out: dict[str, Entity] = {}
    for first, ms in by_first.items():
        lasts = sorted({m.last for m in ms if m.last})
        full: dict[str, Entity] = {}
        for last in lasts:
            e = Entity(person_key(first, last), f"{first} {last}", first, last,
                       mentions=sum(1 for m in ms if m.last == last))
            full[last] = e
            out[e.surface] = e
        bare = [m for m in ms if not m.last]
        if bare:
            e = Entity(person_key(first), first, first, mentions=len(bare),
                       candidates=[full[last].id for last in lasts])
            if len(lasts) == 1:
                e.status = "provisional"
            elif len(lasts) > 1:
                e.status = "ambiguous"
            out[first] = e
    return out

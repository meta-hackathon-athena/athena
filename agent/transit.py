"""Bus arrival lookups. Only a clearly labelled MOCK exists; no real integration yet.

A real provider (e.g. LTA DataMall) would implement the same `arrivals` method.
Responses are untrusted data: the engine validates every field before using it.
"""

from __future__ import annotations

from typing import Protocol


class TransitProvider(Protocol):
    def arrivals(self, lat: float | None, lon: float | None, now_ms: int) -> list[dict]:
        """[{'route': '179', 'minutes': 4, 'stop': 'NTU Hall 2'}], soonest first. May raise."""
        ...


class MockTransitProvider:
    """MOCK DATA for tests and demos. Not real arrival times."""

    source = "mock"

    def __init__(self, arrivals: list[dict] | None = None):
        self._arrivals = arrivals if arrivals is not None else [
            {"route": "179", "minutes": 4, "stop": "MOCK stop"},
            {"route": "199", "minutes": 9, "stop": "MOCK stop"},
        ]
        self.calls = 0

    def arrivals(self, lat, lon, now_ms):
        self.calls += 1
        return list(self._arrivals)

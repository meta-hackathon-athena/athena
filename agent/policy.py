"""Whether a NOTIFY / ASK_USER decision actually reaches the user.

Deterministic: dedup keys, per-kind cooldowns, rolling rate limits, expiry and
"one open question at a time". Delivery itself belongs to the notification service;
the agent only hands it decisions through the Notifier interface.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from .models import Action, Decision

HOUR = 3_600_000


@dataclass
class PolicyConfig:
    # dedup-key prefix -> cooldown (ms). None = once ever (per key).
    cooldowns: dict[str, int | None] = field(default_factory=lambda: {
        "conflict": None, "upcoming": None, "question": None, "refuse": 10 * 60_000,
        "reminder": 2 * HOUR, "transit": 10 * 60_000,
        "recall": 12 * HOUR,              # a remembered fact is brought up at most twice a day
    })
    default_cooldown_ms: int = HOUR
    max_per_hour: dict[str, int] = field(default_factory=lambda: {"low": 3, "normal": 6, "high": 12})


class Notifier(Protocol):
    def deliver(self, decision: Decision) -> bool: ...


class CollectingNotifier:
    """Stand-in for the notification service: keeps what would have been delivered."""

    def __init__(self, fail: bool = False):
        self.sent: list[Decision] = []
        self.fail = fail

    def deliver(self, decision: Decision) -> bool:
        if self.fail:
            return False
        self.sent.append(decision)
        return True


class NotificationGate:
    def __init__(self, cfg: PolicyConfig | None = None, history: dict[str, int] | None = None):
        self.cfg = cfg or PolicyConfig()
        self.last_sent: dict[str, int] = dict(history or {})   # dedup_key -> ts (restored from AgentLog)
        self.sent: deque[tuple[int, str]] = deque()           # (ts, priority) for rate limits

    def _cooldown(self, key: str) -> int | None:
        return self.cfg.cooldowns.get(key.split(":", 1)[0], self.cfg.default_cooldown_ms)

    def recently_sent(self, key: str, now_ms: int) -> bool:
        """Cheap pre-check so callers don't even build decisions that would be dropped as duplicates."""
        if key not in self.last_sent:
            return False
        cd = self._cooldown(key)
        return cd is None or now_ms - self.last_sent[key] < cd

    def check(self, d: Decision, now_ms: int) -> str | None:
        """None if allowed, else the reason it is suppressed."""
        if d.action not in (Action.NOTIFY, Action.ASK_USER):
            return None
        if d.expires_ms is not None and d.expires_ms <= now_ms:
            return "expired"
        if d.deduplication_key and self.recently_sent(d.deduplication_key, now_ms):
            return "duplicate"
        while self.sent and self.sent[0][0] <= now_ms - HOUR:
            self.sent.popleft()
        # budget = everything sent in the last hour; lower priorities get a smaller budget
        if len(self.sent) >= self.cfg.max_per_hour[d.priority.value]:
            return "rate_limited"
        return None

    def record(self, d: Decision, now_ms: int) -> None:
        if d.deduplication_key:
            self.last_sent[d.deduplication_key] = now_ms
        self.sent.append((now_ms, d.priority.value))

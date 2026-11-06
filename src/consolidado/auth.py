"""API key pool + runtime quota accounting.

Auth chain implemented here (layers 1 and 2):
  1. ``YOUTUBE_API_KEYS``  — comma-separated pool, rotated on quota errors.
  2. ``YOUTUBE_API_KEY``   — single key, appended to the same pool.

Layers 3 (yt-dlp) and 4 (Atom RSS) live in ``ytdlp_client.py`` / ``rss_client.py``
and are zero-quota, so they never touch this tracker.

Quota units are the official YouTube Data API v3 costs; they are charged only
after a call succeeds, because a 403 quota error does not consume units.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

#: Official YouTube Data API v3 quota cost per endpoint, in units.
ENDPOINT_COST: dict[str, int] = {
    "search": 100,
    "videos": 1,
    "channels": 1,
    "playlistItems": 1,
    "playlists": 1,
    "commentThreads": 1,
}

#: Google error reasons that mean "this key is out of quota".
QUOTA_REASONS = frozenset(
    {
        "quotaExceeded",
        "dailyLimitExceeded",
        "dailyLimitExceededUnreg",
        "userRateLimitExceeded",
        "rateLimitExceeded",
    }
)


class NoApiKeyError(RuntimeError):
    """No YouTube API key was found in the environment."""


class QuotaExhaustedError(RuntimeError):
    """Every key in the pool returned a quota error."""


@dataclass
class QuotaTracker:
    """Session-scoped counters. One instance per process, shared by all tools."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _by_tool: dict[str, int] = field(default_factory=dict)
    _by_endpoint: dict[str, int] = field(default_factory=dict)
    _calls: dict[str, int] = field(default_factory=dict)
    _cache_hits: int = 0
    _layers: set[str] = field(default_factory=set)

    def charge(self, tool: str, endpoint: str, units: int | None = None) -> int:
        """Record ``units`` (default: the endpoint's official cost) for ``tool``."""
        cost = ENDPOINT_COST.get(endpoint, 1) if units is None else units
        with self._lock:
            self._by_tool[tool] = self._by_tool.get(tool, 0) + cost
            self._by_endpoint[endpoint] = self._by_endpoint.get(endpoint, 0) + cost
            self._calls[tool] = self._calls.get(tool, 0) + 1
        return cost

    def cache_hit(self) -> None:
        with self._lock:
            self._cache_hits += 1

    def mark_layer(self, layer: str) -> None:
        """Record which auth layer actually served data: data_api | ytdlp | rss."""
        with self._lock:
            self._layers.add(layer)

    def total(self) -> int:
        with self._lock:
            return sum(self._by_tool.values())

    def snapshot(self, pool: "ApiKeyPool | None" = None) -> dict:
        with self._lock:
            by_tool = dict(sorted(self._by_tool.items(), key=lambda kv: -kv[1]))
            by_endpoint = dict(sorted(self._by_endpoint.items(), key=lambda kv: -kv[1]))
            calls = dict(self._calls)
            layers = sorted(self._layers)
            cache_hits = self._cache_hits
            total = sum(by_tool.values())

        status: dict = {
            "session_quota_units": total,
            "api_calls": sum(calls.values()),
            "cache_hits": cache_hits,
            "by_tool": by_tool,
            "by_endpoint": by_endpoint,
            "layers_used": layers,
            "daily_free_quota": 10000,
            "note": (
                "Units are the official YouTube Data API v3 costs charged on success. "
                "The API does not expose remaining quota; this is a session-local tally."
            ),
        }
        if pool is not None:
            status["keys"] = pool.describe()
            status["session_quota_units"] = total
            status["quota_remaining_estimate"] = max(0, 10000 * max(pool.key_count, 1) - total)
        return status


class ApiKeyPool:
    """Ordered, deduplicated key pool with rotation on quota exhaustion."""

    def __init__(self, keys: list[str] | None = None) -> None:
        self._keys: list[str] = self._collect(keys)
        self._exhausted: dict[int, str] = {}
        self._reason: dict[int, str] = {}
        self._services: dict[int, object] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- discovery
    @staticmethod
    def _collect(explicit: list[str] | None) -> list[str]:
        raw: list[str] = []
        if explicit:
            raw.extend(explicit)
        raw.extend((os.environ.get("YOUTUBE_API_KEYS") or "").split(","))
        raw.extend((os.environ.get("YOUTUBE_API_KEY") or "").split(","))
        keys: list[str] = []
        for candidate in raw:
            key = candidate.strip()
            if key and key not in keys:
                keys.append(key)
        return keys

    def reload(self) -> None:
        """Re-read the environment (used after the operator exports a key)."""
        with self._lock:
            self._keys = self._collect(None)
            self._exhausted.clear()
            self._services.clear()

    # ------------------------------------------------------------------ queries
    @property
    def key_count(self) -> int:
        return len(self._keys)

    @property
    def active_index(self) -> int | None:
        for index in range(len(self._keys)):
            if index not in self._exhausted:
                return index
        return None

    def describe(self) -> dict:
        return {
            "total": len(self._keys),
            "usable": len(self._keys) - len(self._exhausted),
            "exhausted": len(self._exhausted),
            "active_index": self.active_index,
            "sources": ["YOUTUBE_API_KEYS", "YOUTUBE_API_KEY"],
        }

    def ensure(self) -> None:
        if not self._keys:
            raise NoApiKeyError(
                "No YouTube API key configured. Export YOUTUBE_API_KEYS "
                "(comma-separated) or YOUTUBE_API_KEY. Layers 3 (yt-dlp) and 4 (RSS) "
                "still work without a key — only Data API tools need one."
            )

    # ----------------------------------------------------------------- services
    def service(self) -> tuple[object, int]:
        """Return ``(googleapiclient service, key_index)`` for the first usable key."""
        self.ensure()
        index = self.active_index
        if index is None:
            raise QuotaExhaustedError(
                "Every key in the pool hit quotaExceeded. Wait for the daily reset "
                "or add another key to YOUTUBE_API_KEYS."
            )
        with self._lock:
            cached = self._services.get(index)
        if cached is not None:
            return cached, index

        import httplib2
        from googleapiclient.discovery import build

        # 30 s hard socket timeout: without it httplib2 waits on the OS-level TCP
        # timeout (minutes), which is what made parallel calls look like hangs.
        service = build(
            "youtube",
            "v3",
            developerKey=self._keys[index],
            cache_discovery=False,
            http=httplib2.Http(timeout=30),
        )
        with self._lock:
            self._services[index] = service
        return service, index

    def mark_exhausted(self, index: int, reason: str) -> bool:
        """Retire a key. Returns True when another key remains."""
        with self._lock:
            self._exhausted[index] = reason
            self._reason[index] = reason
            remaining = len(self._keys) - len(self._exhausted)
        return remaining > 0

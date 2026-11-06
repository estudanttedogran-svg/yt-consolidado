"""Tests for the wire protections added to YouTubeClient: lock, retry, timeout.

No network: the service object is a stub and ``request.execute`` is a fake.
"""

from __future__ import annotations

import ssl
import threading
import time

import pytest

from consolidado.auth import QuotaTracker
from consolidado.youtube_client import MAX_ATTEMPTS, YouTubeApiError, YouTubeClient


class StubPool:
    """Minimal stand-in for ApiKeyPool: one key, one service, no rotation."""

    key_count = 1

    def service(self):
        return object(), 0

    def mark_exhausted(self, index: int, reason: str) -> bool:
        return False


class FakeRequest:
    """Replays a script of outcomes: an exception class, or a payload dict."""

    def __init__(self, *outcomes, delay: float = 0.0) -> None:
        self.outcomes = list(outcomes)
        self.delay = delay
        self.attempts = 0

    def execute(self):
        self.attempts += 1
        outcome = self.outcomes.pop(0) if self.outcomes else {"items": []}
        if self.delay:
            time.sleep(self.delay)
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome("boom")
        return outcome


def build(request) -> tuple[YouTubeClient, QuotaTracker]:
    tracker = QuotaTracker()
    return YouTubeClient(pool=StubPool(), tracker=tracker), tracker


def test_ssl_error_is_retried_and_charged_once(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    request = FakeRequest(ssl.SSLError, ssl.SSLEOFError, {"items": [1]})
    client, tracker = build(request)

    response = client._request("tool", "videos", lambda service: request)

    assert response == {"items": [1]}
    assert request.attempts == 3
    assert sleeps == [2.0, 4.0]  # linear backoff, not a busy loop
    assert tracker.total() == 1  # charged once, on the real response


def test_timeout_exhausts_attempts_and_never_charges(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    request = FakeRequest(TimeoutError, TimeoutError, TimeoutError)
    client, tracker = build(request)

    with pytest.raises(YouTubeApiError) as error:
        client._request("tool", "videos", lambda service: request)

    assert request.attempts == MAX_ATTEMPTS == 3
    assert "after 3 attempts" in str(error.value)
    assert tracker.total() == 0


def test_requests_never_overlap_across_threads():
    """The whole point: parallel tool calls are serialized inside the client."""
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    class ConcurrentRequest:
        def execute(self):
            with guard:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            with guard:
                state["active"] -= 1
            return {"ok": True}

    client, _ = build(None)
    request = ConcurrentRequest()

    threads = [
        threading.Thread(target=lambda: client._request("tool", "videos", lambda service: request))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert state["peak"] == 1  # without the lock this reaches 4

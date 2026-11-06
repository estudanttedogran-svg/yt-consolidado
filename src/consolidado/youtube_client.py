"""Thin wrapper over the YouTube Data API v3 with key rotation and quota charging.

Every public method takes ``tool`` — the MCP tool name — so ``get_quota_status``
can attribute units to the caller. Responses are raw API dicts: shaping happens
in ``tools/*`` so each ported tool keeps the exact shape of its original.

Layer 1/2 only (API keys). Layer 3 (yt-dlp) and 4 (RSS) are separate modules.

Two protections against the SSL/timeout failures seen when the model fires calls
in parallel:

* every request is serialized process-wide (``YouTubeClient._api_lock``);
* transient wire errors are retried with linear backoff before they surface.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from typing import Any, Callable, Iterable

from googleapiclient.errors import HttpError

from .auth import (
    ENDPOINT_COST,
    QUOTA_REASONS,
    ApiKeyPool,
    NoApiKeyError,
    QuotaExhaustedError,
    QuotaTracker,
)

#: Wire failures — the request may be fine, the connection was not.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    ssl.SSLError,  # includes SSLEOFError, the one the MCP died on
    ssl.SSLEOFError,
    socket.timeout,  # alias of TimeoutError since 3.10, kept for clarity
    TimeoutError,
    ConnectionError,  # ConnectionResetError, BrokenPipeError, ConnectionAbortedError
    http.client.RemoteDisconnected,  # server closed the socket without a response
)

#: Attempts per key on a transient failure (1 original + 2 retries).
MAX_ATTEMPTS = 3
#: Backoff base; grows linearly per attempt (2 s, then 4 s).
RETRY_DELAY_SECONDS = 2.0


class YouTubeApiError(RuntimeError):
    """Non-quota API failure, already unwrapped into a readable message."""

    def __init__(self, message: str, status: int | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


#: Process-wide singletons. Tests inject their own via the ``client=`` keyword.
POOL = ApiKeyPool()
TRACKER = QuotaTracker()


def _error_reason(error: HttpError) -> str:
    """Best-effort extraction of the Google ``reason`` field."""
    try:
        payload = error.error_details or []
        for detail in payload:
            if isinstance(detail, dict) and detail.get("reason"):
                return str(detail["reason"])
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        content = error.content.decode("utf-8", "replace")
        for reason in QUOTA_REASONS:
            if reason in content:
                return reason
    except Exception:  # pragma: no cover - defensive
        pass
    return ""


class YouTubeClient:
    """Quota-aware facade. All calls go through :meth:`_request`.

    One request in flight, process-wide: the Data API is metered and
    rate-limited, and parallel bursts are what produced the SSL/timeout
    failures. The lock is held across retries too, so a flapping endpoint
    applies backpressure instead of piling up — and it also protects the
    pool's round-robin index, which is shared mutable state.
    """

    #: Class-level on purpose — a second YouTubeClient (a test fake, a stray
    #: construction) must still not talk to the API at the same time.
    #: RLock, so a nested call from the same thread cannot deadlock itself.
    _api_lock: threading.RLock = threading.RLock()

    def __init__(self, pool: ApiKeyPool | None = None, tracker: QuotaTracker | None = None) -> None:
        self.pool = pool if pool is not None else POOL
        self.tracker = tracker if tracker is not None else TRACKER

    # ------------------------------------------------------------------ plumbing
    def _execute_with_retry(self, endpoint: str, request: Any) -> dict[str, Any]:
        """``request.execute()`` with linear backoff on transient wire errors.

        Runs with :attr:`_api_lock` held, so the sleep below is backpressure for
        every other caller. A request that timed out may still have been metered
        server-side; quota is charged locally only on a real response.
        """
        last: BaseException | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return request.execute()
            except TRANSIENT_ERRORS as error:
                last = error
                if attempt == MAX_ATTEMPTS - 1:
                    break
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
        if last is not None:
            raise last
        raise YouTubeApiError(f"{endpoint} failed without an exception")  # unreachable

    def _request(
        self,
        tool: str,
        endpoint: str,
        call: Callable[[Any], Any],
        units: int | None = None,
    ) -> dict[str, Any]:
        """Run ``call(service)`` serialized, retried, with rotation and charging."""
        attempts = max(self.pool.key_count, 1)
        last_reason = ""
        with self._api_lock:
            for _ in range(attempts):
                service, index = self.pool.service()
                try:
                    response = self._execute_with_retry(endpoint, call(service))
                except HttpError as error:
                    reason = _error_reason(error)
                    if error.resp.status == 403 and reason in QUOTA_REASONS:
                        last_reason = reason
                        if self.pool.mark_exhausted(index, reason):
                            continue
                        raise QuotaExhaustedError(
                            "Every key in the pool hit quotaExceeded. Wait for the daily "
                            "reset or add another key to YOUTUBE_API_KEYS."
                        ) from error
                    raise YouTubeApiError(
                        f"{endpoint} failed: {error}",
                        status=getattr(error.resp, "status", None),
                        reason=reason or None,
                    ) from error
                except NoApiKeyError:
                    raise
                except TRANSIENT_ERRORS as error:
                    raise YouTubeApiError(
                        f"{endpoint} failed after {MAX_ATTEMPTS} attempts: {error!r}"
                    ) from error
                except Exception as error:  # JSON, discovery, anything else
                    raise YouTubeApiError(f"{endpoint} failed: {error}") from error

                self.tracker.charge(tool, endpoint, units)
                self.tracker.mark_layer("data_api")
                return response

            raise QuotaExhaustedError(
                f"Every key in the pool hit quotaExceeded (last reason: {last_reason})."
            )

    # -------------------------------------------------------------------- calls
    def search(
        self,
        tool: str,
        query: str | None = None,
        max_results: int = 10,
        search_type: str = "video",
        order: str | None = None,
        published_after: str | None = None,
        published_before: str | None = None,
        region_code: str | None = None,
        relevance_language: str | None = None,
        channel_id: str | None = None,
        video_duration: str | None = None,
        video_definition: str | None = None,
        safe_search: str | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "part": "snippet",
            "maxResults": min(max_results, 50),
            "type": search_type,
        }
        optional = {
            "q": query,
            "order": order,
            "publishedAfter": published_after,
            "publishedBefore": published_before,
            "regionCode": region_code,
            "relevanceLanguage": relevance_language,
            "channelId": channel_id,
            "videoDuration": video_duration,
            "videoDefinition": video_definition,
            "safeSearch": safe_search,
            "pageToken": page_token,
        }
        params.update({key: value for key, value in optional.items() if value})
        return self._request(tool, "search", lambda service: service.search().list(**params))

    def channels(
        self,
        tool: str,
        channel_ids: Iterable[str] | None = None,
        for_handle: str | None = None,
        for_username: str | None = None,
        parts: str = "snippet,contentDetails,statistics",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"part": parts}
        if channel_ids:
            params["id"] = ",".join(list(channel_ids)[:50])
        elif for_handle:
            params["forHandle"] = for_handle.lstrip("@")
        elif for_username:
            params["forUsername"] = for_username
        else:
            raise ValueError("one of channel_ids, for_handle or for_username is required")
        return self._request(tool, "channels", lambda service: service.channels().list(**params))

    def playlist_items(
        self,
        tool: str,
        playlist_id: str,
        max_results: int = 50,
        page_token: str | None = None,
        parts: str = "snippet,contentDetails",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "part": parts,
            "playlistId": playlist_id,
            "maxResults": min(max_results, 50),
        }
        if page_token:
            params["pageToken"] = page_token
        return self._request(
            tool, "playlistItems", lambda service: service.playlistItems().list(**params)
        )

    def videos(
        self,
        tool: str,
        video_ids: Iterable[str],
        parts: str = "snippet,contentDetails,statistics",
    ) -> dict[str, Any]:
        ids = ",".join(list(video_ids)[:50])
        return self._request(
            tool, "videos", lambda service: service.videos().list(part=parts, id=ids)
        )

    def playlists(self, tool: str, playlist_id: str, parts: str = "snippet,contentDetails") -> dict:
        return self._request(
            tool, "playlists", lambda service: service.playlists().list(part=parts, id=playlist_id)
        )


_CLIENT: YouTubeClient | None = None


def get_client() -> YouTubeClient:
    """Shared client (lazy, so importing the package needs no key)."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = YouTubeClient()
    return _CLIENT


__all__ = [
    "ENDPOINT_COST",
    "YouTubeApiError",
    "YouTubeClient",
    "get_client",
    "POOL",
    "TRACKER",
]

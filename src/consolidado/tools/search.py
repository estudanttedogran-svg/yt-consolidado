"""Search tools — two originals, two shapes, both preserved byte for byte.

``search_videos``     → port of youtube-mcp ``search_videos`` (formatted shape).
``youtube_search``    → port of tube-bridge ``youtube_search`` (raw-ish shape
                        with Data API primary and yt-dlp fallback).

They are intentionally not merged: FASE 6 compares each against its own original.
"""

from __future__ import annotations

from typing import Any

from ..auth import NoApiKeyError, QuotaExhaustedError
from ..youtube_client import YouTubeApiError, YouTubeClient, get_client
from ..ytdlp_client import YtDlpError, YtDlpRateLimited, search_videos as ytdlp_search


def _format_search_results(query: str, raw_response: dict[str, Any]) -> dict[str, Any]:
    """Same projection as youtube-mcp ``formatters.format_search_results``."""
    results: list[dict[str, Any]] = []
    for item in raw_response.get("items", []):
        id_info = item.get("id", {}) or {}
        kind = id_info.get("kind", "")
        target_id = ""
        url = ""
        category = "unknown"
        if "video" in kind:
            target_id = id_info.get("videoId", "")
            url = f"https://www.youtube.com/watch?v={target_id}"
            category = "video"
        elif "channel" in kind:
            target_id = id_info.get("channelId", "")
            url = f"https://www.youtube.com/channel/{target_id}"
            category = "channel"
        elif "playlist" in kind:
            target_id = id_info.get("playlistId", "")
            url = f"https://www.youtube.com/playlist?list={target_id}"
            category = "playlist"

        snippet = item.get("snippet", {}) or {}
        results.append(
            {
                "id": target_id,
                "kind": category,
                "title": snippet.get("title", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt", ""),
                "description": snippet.get("description", ""),
                "url": url,
            }
        )

    page_info = raw_response.get("pageInfo", {}) or {}
    return {
        "query": query,
        "total_results": page_info.get("totalResults"),
        "next_page_token": raw_response.get("nextPageToken"),
        "results": results,
    }


def search_videos(
    query: str,
    max_results: int = 10,
    search_type: str = "video",
    order: str = "relevance",
    published_after: str | None = None,
    region_code: str | None = None,
    raw: bool = False,
    client: YouTubeClient | None = None,
) -> dict[str, Any]:
    """Search videos, channels or playlists — youtube-mcp parity.

    Layer: YouTube Data API (needs a key). Quota: 100 units (search.list).
    ``raw=True`` returns the unaltered Google response.
    """
    client = client or get_client()
    try:
        response = client.search(
            "search_videos",
            query=query,
            max_results=max_results,
            search_type=search_type,
            order=order,
            published_after=published_after,
            region_code=region_code,
        )
    except (NoApiKeyError, QuotaExhaustedError, YouTubeApiError) as error:
        return {"success": False, "error": str(error)}

    if raw:
        return response
    return {"success": True, **_format_search_results(query, response)}


def youtube_search(
    query: str,
    limit: int = 10,
    order: str | None = None,
    published_after: str | None = None,
    published_before: str | None = None,
    channel_id: str | None = None,
    video_duration: str | None = None,
    video_definition: str | None = None,
    safe_search: str | None = None,
    client: YouTubeClient | None = None,
) -> dict[str, Any]:
    """Search videos with date/channel/duration/order filters — tube-bridge parity.

    Layer 1/2 (Data API, 100 units) → Layer 3 (yt-dlp, 0 units) when no usable
    key remains. Search has no channel to feed the RSS layer, so a yt-dlp 403 is
    reported instead of silently degraded.
    """
    client = client or get_client()
    try:
        response = client.search(
            "youtube_search",
            query=query,
            max_results=limit,
            search_type="video",
            order=order,
            published_after=published_after,
            published_before=published_before,
            channel_id=channel_id,
            video_duration=video_duration,
            video_definition=video_definition,
            safe_search=safe_search,
        )
    except (NoApiKeyError, QuotaExhaustedError, YouTubeApiError) as api_error:
        try:
            fallback = ytdlp_search(query, limit)
            fallback["_fallback_reason"] = str(api_error)
            client.tracker.mark_layer("ytdlp")
            return fallback
        except YtDlpRateLimited as rate_error:
            return {
                "query": query,
                "source": "none",
                "total_results": 0,
                "videos": [],
                "error": str(rate_error),
                "_fallback_reason": str(api_error),
                "note": "yt-dlp is rate limited and search cannot use the RSS layer (no channel id).",
            }
        except YtDlpError as fallback_error:
            return {
                "query": query,
                "source": "none",
                "total_results": 0,
                "videos": [],
                "error": str(fallback_error),
                "_fallback_reason": str(api_error),
            }

    videos = []
    for item in response.get("items", []):
        video_id = (item.get("id") or {}).get("videoId", "")
        snippet = item.get("snippet", {}) or {}
        videos.append(
            {
                "id": video_id,
                "title": snippet.get("title", ""),
                "url": f"https://youtube.com/watch?v={video_id}",
                "channel": snippet.get("channelTitle", ""),
                "channel_id": snippet.get("channelId", ""),
                "published_at": snippet.get("publishedAt", ""),
                "description": (snippet.get("description", "") or "")[:200],
                "thumbnail": ((snippet.get("thumbnails") or {}).get("default") or {}).get("url"),
            }
        )

    return {
        "query": query,
        "source": "YouTube Data API v3",
        "total_results": len(videos),
        "next_page_token": response.get("nextPageToken"),
        "videos": videos,
    }

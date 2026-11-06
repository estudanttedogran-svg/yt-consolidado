"""Layer 3: zero-quota access through yt-dlp (no API key, no quota units).

Used for channel upload listing (the one tool that must work headless) and as an
automatic fallback when the Data API has no usable key left.

Every subprocess call runs ``<sys.executable> -m yt_dlp`` so it always uses the
yt-dlp pinned in this project's environment, never a stray global binary.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

TIMEOUT_DEFAULT = 45


class YtDlpError(RuntimeError):
    """yt-dlp failed for a reason other than rate limiting."""


class YtDlpRateLimited(YtDlpError):
    """YouTube returned 403 / 'sign in to confirm' — caller should fall back to RSS."""


_RATE_MARKERS = (
    "HTTP Error 403",
    "Sign in to confirm",
    "confirm you’re not a bot",
    "confirm you're not a bot",
    "This content isn't available",
)


def _run(args: list[str], timeout: int = TIMEOUT_DEFAULT) -> tuple[list[dict[str, Any]], str]:
    command = [sys.executable, "-m", "yt_dlp", *args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise YtDlpError(f"yt-dlp timed out after {timeout}s") from error

    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0 and not (completed.stdout or "").strip():
        if any(marker in stderr for marker in _RATE_MARKERS):
            raise YtDlpRateLimited(stderr.splitlines()[-1] if stderr else "rate limited")
        raise YtDlpError(stderr.splitlines()[-1] if stderr else "yt-dlp failed")

    items: list[dict[str, Any]] = []
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items, stderr


def normalize_channel_url(channel_url: str) -> str:
    """Accept '@handle', 'handle', 'UC...' or a full URL and return a channel URL."""
    value = channel_url.strip()
    if value.startswith("http"):
        return value.rstrip("/")
    if value.startswith("@"):
        return f"https://youtube.com/{value}"
    if value.startswith("UC") and len(value) == 24:
        return f"https://youtube.com/channel/{value}"
    return f"https://youtube.com/@{value}"


def get_channel_videos_recent(channel_url: str, limit: int = 10) -> dict[str, Any]:
    """Recent uploads — shape identical to tube-bridge ``youtube_get_channel_videos``."""
    limit = max(1, min(limit, 50))
    url = normalize_channel_url(channel_url)
    items, stderr = _run(
        [
            f"{url}/videos",
            "--dump-json",
            "--flat-playlist",
            "--playlist-end",
            str(limit),
            "--extractor-args",
            "youtube:max_comments=0",
        ]
    )

    videos: list[dict[str, Any]] = []
    channel_name = ""
    for data in items:
        video_id = data.get("id", "")
        if not channel_name:
            channel_name = data.get("channel") or data.get("uploader") or ""
        videos.append(
            {
                "id": video_id,
                "title": data.get("title", ""),
                "url": f"https://youtube.com/watch?v={video_id}",
                "duration": data.get("duration"),
                "view_count": data.get("view_count"),
                "upload_date": data.get("upload_date"),
            }
        )

    result: dict[str, Any] = {
        "channel": channel_name,
        "channel_url": url,
        "total_videos": len(videos),
        "videos": videos,
    }
    if not videos:
        result["_warning"] = "No videos found — channel may not exist or has no uploads"
    if stderr and videos:
        result["_ytdlp_stderr"] = stderr[:400]
    return result


def search_videos(query: str, limit: int = 10) -> dict[str, Any]:
    """Anonymous search — shape identical to tube-bridge ``youtube_search`` (yt-dlp path)."""
    limit = max(1, min(limit, 50))
    items, stderr = _run(
        [
            f"ytsearch{limit}:{query}",
            "--dump-json",
            "--flat-playlist",
            "--extractor-args",
            "youtube:max_comments=0",
        ],
        timeout=60,
    )
    videos = []
    for data in items:
        video_id = data.get("id", "")
        videos.append(
            {
                "id": video_id,
                "title": data.get("title", ""),
                "url": f"https://youtube.com/watch?v={video_id}",
                "duration": data.get("duration"),
                "view_count": data.get("view_count"),
                "channel": data.get("channel") or data.get("uploader"),
                "channel_url": data.get("channel_url") or data.get("uploader_url"),
                "upload_date": data.get("upload_date"),
            }
        )
    if stderr and not videos:
        raise YtDlpError(f"Could not search YouTube for {query!r}")
    return {
        "query": query,
        "source": "yt-dlp (anonymous)",
        "total_results": len(videos),
        "videos": videos,
    }


def get_video_info(video_id: str) -> dict[str, Any]:
    """Single video metadata, shaped like the Data API path in ``youtube_get_video_info``."""
    items, stderr = _run(
        [
            f"https://youtube.com/watch?v={video_id}",
            "--dump-json",
            "--extractor-args",
            "youtube:max_comments=0",
        ],
        timeout=30,
    )
    if not items:
        raise YtDlpError(f"Could not fetch info for video {video_id}")

    data = items[0]
    upload_date = data.get("upload_date") or ""
    result: dict[str, Any] = {
        "id": data.get("id", video_id),
        "title": data.get("title", ""),
        "url": f"https://youtube.com/watch?v={video_id}",
        "duration": data.get("duration"),
        "view_count": data.get("view_count"),
        "channel": data.get("channel") or data.get("uploader") or "",
        "channel_id": data.get("channel_id", ""),
        "channel_url": data.get("channel_url", ""),
        "upload_date": upload_date,
        "description": (data.get("description") or "")[:500],
        "thumbnail": data.get("thumbnail"),
        "tags": (data.get("tags") or [])[:20] or None,
        "source": "yt-dlp (anonymous)",
    }
    if stderr:
        result["_ytdlp_stderr"] = stderr[:400]
    return result

"""Video tools — tube-bridge parity (info, channel uploads, transcript)."""

from __future__ import annotations

import re
from typing import Any

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApiException,
)

from ..auth import NoApiKeyError, QuotaExhaustedError
from ..youtube_client import YouTubeApiError, YouTubeClient, get_client
from ..ytdlp_client import (
    YtDlpError,
    YtDlpRateLimited,
    get_channel_videos_recent,
    get_video_info as ytdlp_video_info,
)
from ..rss_client import get_rss_uploads
from .custom import parse_iso8601_seconds

VIDEO_ID_PATTERNS = (
    re.compile(r"(?:v=|/v/|/embed/|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})"),
    re.compile(r"^([A-Za-z0-9_-]{11})$"),
)


def extract_video_id(value: str) -> str:
    """Accept a video id or any common YouTube URL and return the 11-char id.

    Raises ``ValueError`` on anything else — the original (tube-bridge
    ``youtube.client.extract_video_id``) raises instead of passing the string
    through, and passing it through would spend a Data API call on garbage.
    """
    text = (value or "").strip()
    for pattern in VIDEO_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    raise ValueError(f"Cannot extract video ID from: {value}")


def youtube_get_video_info(url: str, client: YouTubeClient | None = None) -> dict[str, Any]:
    """Rich metadata for one video — tube-bridge parity.

    Layer 1/2 (Data API, 1 unit) → Layer 3 (yt-dlp, 0 units) when keys are gone.
    ``duration`` is in seconds; ``upload_date`` is ``YYYYMMDD``.
    """
    client = client or get_client()
    video_id = extract_video_id(url)

    try:
        response = client.videos(
            "youtube_get_video_info", [video_id], parts="snippet,statistics,contentDetails"
        )
        items = response.get("items", [])
        if not items:
            raise YouTubeApiError(f"Video not found: {video_id}")
    except (NoApiKeyError, QuotaExhaustedError, YouTubeApiError) as api_error:
        try:
            fallback = ytdlp_video_info(video_id)
            client.tracker.mark_layer("ytdlp")
            fallback["_fallback_reason"] = str(api_error)
            return fallback
        except YtDlpRateLimited as rate_error:
            return {
                "id": video_id,
                "url": f"https://youtube.com/watch?v={video_id}",
                "error": str(rate_error),
                "_fallback_reason": str(api_error),
                "note": "yt-dlp is rate limited; a single video has no RSS fallback.",
            }
        except YtDlpError as fallback_error:
            return {
                "id": video_id,
                "url": f"https://youtube.com/watch?v={video_id}",
                "error": str(fallback_error),
                "_fallback_reason": str(api_error),
            }

    item = items[0]
    snippet = item.get("snippet", {}) or {}
    statistics = item.get("statistics", {}) or {}
    content_details = item.get("contentDetails", {}) or {}
    published_at = snippet.get("publishedAt", "")

    return {
        "id": video_id,
        "title": snippet.get("title", ""),
        "url": f"https://youtube.com/watch?v={video_id}",
        "duration": parse_iso8601_seconds(content_details.get("duration")),
        "view_count": int(statistics.get("viewCount", 0)) if statistics.get("viewCount") else None,
        "channel": snippet.get("channelTitle", ""),
        "channel_id": snippet.get("channelId", ""),
        "channel_url": f"https://youtube.com/channel/{snippet.get('channelId', '')}",
        "upload_date": published_at[:10].replace("-", ""),
        "description": (snippet.get("description", "") or "")[:500],
        "thumbnail": ((snippet.get("thumbnails") or {}).get("default") or {}).get("url"),
        "tags": (snippet.get("tags") or [])[:20] or None,
        "source": "YouTube Data API v3",
    }


def youtube_get_channel_videos(
    channel_url: str, limit: int = 10, client: YouTubeClient | None = None
) -> dict[str, Any]:
    """Recent uploads via yt-dlp — tube-bridge parity.

    Layer 3 (yt-dlp). Quota: 0 units, no key. If YouTube rate-limits yt-dlp the
    call falls back to Layer 4 (Atom RSS) when the argument is a channel id, and
    says so in ``_fallback``.
    """
    client = client or get_client()
    try:
        result = get_channel_videos_recent(channel_url, limit)
        client.tracker.mark_layer("ytdlp")
        return result
    except YtDlpRateLimited as rate_error:
        if channel_url.strip().startswith("UC"):
            rss = get_rss_uploads(channel_url, max_results=min(limit, 15))
            if rss.get("success"):
                client.tracker.mark_layer("rss")
                rss["_fallback"] = "rss"
                rss["_ytdlp_error"] = str(rate_error)
                return rss
        return {
            "channel_url": channel_url,
            "total_videos": 0,
            "videos": [],
            "error": str(rate_error),
            "note": "yt-dlp rate limited; RSS needs a UC… channel id to be usable.",
        }
    except YtDlpError as error:
        return {
            "channel_url": channel_url,
            "total_videos": 0,
            "videos": [],
            "error": str(error),
        }


def _pick_language(api: YouTubeTranscriptApi, video_id: str, lang: str | None) -> str | None:
    """Return the language code to fetch, preferring ``lang`` then any manual track."""
    try:
        transcript_list = api.list(video_id)
    except Exception:
        return lang
    for transcript in transcript_list:
        if lang and transcript.language_code.split("-")[0] == lang.split("-")[0]:
            return transcript.language_code
    if lang is None:
        for transcript in transcript_list:
            if not transcript.is_generated:
                return transcript.language_code
        for transcript in transcript_list:
            return transcript.language_code
    return lang


def youtube_get_transcript(
    url: str, lang: str | None = None, with_timestamps: bool = False
) -> dict[str, Any]:
    """Transcript text for a video — tube-bridge parity.

    Layer: youtube-transcript-api (public timedtext). Quota: 0 units, no key.
    Manual captions are preferred over auto-generated when ``lang`` is omitted.
    """
    video_id = extract_video_id(url)
    api = YouTubeTranscriptApi()
    language = _pick_language(api, video_id, lang)

    try:
        fetched = api.fetch(video_id, languages=[language] if language else ["en"])
        snippets = list(fetched.snippets)
        detected_language = fetched.language_code if hasattr(fetched, "language_code") else language
        is_generated = bool(getattr(fetched, "is_generated", False))
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as error:
        return {
            "video_id": video_id,
            "error": str(error),
            "note": "Captions are disabled, absent, or the video is unavailable.",
        }
    except YouTubeTranscriptApiException as error:
        return {"video_id": video_id, "error": str(error)}

    if with_timestamps:
        lines = []
        for snippet in snippets:
            start = snippet.start
            lines.append(
                f"[{int(start // 60):02d}:{int(start % 60):02d}] "
                f"{snippet.text.strip().replace(chr(10), ' ')}"
            )
        output = "\n".join(lines)
    else:
        output = " ".join(snippet.text for snippet in snippets)

    return {
        "video_id": video_id,
        "language": detected_language,
        "is_generated": is_generated,
        "segment_count": len(snippets),
        "with_timestamps": with_timestamps,
        "text": output,
    }

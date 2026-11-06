"""Layer 4: zero-quota Atom feed reader (feedparser).

Emergency path for "what did this channel publish recently" when keys are gone
and yt-dlp is rate limited. Requires a known channel id or channel URL.

Output shape is byte-compatible with youtube-mcp's ``get_channel_rss_videos``:
``success``, ``source``, ``quota_units_consumed``, ``channel_id``,
``channel_title``, ``channel_url``, ``video_count``, ``videos[...]``.
"""

from __future__ import annotations

from typing import Any

import feedparser

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def build_feed_url(channel_id: str) -> str:
    value = channel_id.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("@"):
        return f"https://www.youtube.com/feeds/videos.xml?user={value.lstrip('@')}"
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={value}"


def _resolve_channel_id(from_feed: str | None, requested: str) -> str:
    """feedparser occasionally drops the ``UC`` prefix; rebuild it and prefer the input id."""
    candidate = (from_feed or "").strip()
    if len(candidate) == 22 and not candidate.startswith("UC"):
        candidate = "UC" + candidate
    if candidate.startswith("UC") and len(candidate) == 24:
        return candidate
    given = requested.strip()
    if given.startswith("UC") and len(given) == 24:
        return given
    return candidate or given


def _stat_views(entry: Any) -> int:
    stats = entry.get("media_statistics")
    if isinstance(stats, dict):
        try:
            return int(stats.get("views", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _thumbnail(entry: Any) -> str:
    thumbs = entry.get("media_thumbnail") or []
    if isinstance(thumbs, list) and thumbs:
        first = thumbs[0]
        if isinstance(first, dict):
            return first.get("url", "")
    return ""


def _description(entry: Any) -> str:
    value = entry.get("media_description") or entry.get("summary") or ""
    return value[:300] if value else ""


def get_rss_uploads(channel_id: str, max_results: int = 15) -> dict[str, Any]:
    """Latest uploads for a known channel. 0 quota units, no API key."""
    feed_url = build_feed_url(channel_id)
    try:
        parsed = feedparser.parse(feed_url, agent=USER_AGENT)
    except Exception as error:  # pragma: no cover - feedparser rarely raises
        return {
            "success": False,
            "source": "youtube_atom_rss",
            "error": f"Failed to fetch or parse RSS feed for channel '{channel_id}': {error}",
            "feed_url": feed_url,
        }

    feed = parsed.get("feed") or {}
    entries = parsed.get("entries") or []
    if parsed.get("bozo") and not entries and parsed.get("bozo_exception"):
        return {
            "success": False,
            "source": "youtube_atom_rss",
            "error": (
                f"Failed to fetch or parse RSS feed for channel '{channel_id}': "
                f"{parsed['bozo_exception']}"
            ),
            "feed_url": feed_url,
        }

    videos: list[dict[str, Any]] = []
    for entry in entries[: max(1, min(max_results, 15))]:
        video_id = entry.get("yt_videoid", "")
        videos.append(
            {
                "video_id": video_id,
                "title": entry.get("title", ""),
                "published_at": entry.get("published", ""),
                "updated_at": entry.get("updated", ""),
                "url": entry.get("link") or f"https://www.youtube.com/watch?v={video_id}",
                "description_snippet": _description(entry),
                "thumbnail_url": _thumbnail(entry),
                "views": _stat_views(entry),
            }
        )

    return {
        "success": True,
        "source": "youtube_atom_rss",
        "quota_units_consumed": 0,
        "channel_id": _resolve_channel_id(feed.get("yt_channelid"), channel_id),
        "channel_title": feed.get("title", ""),
        "channel_url": feed.get("link", ""),
        "video_count": len(videos),
        "videos": videos,
    }

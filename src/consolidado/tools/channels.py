"""Channel tools — youtube-mcp parity, zubeid parity, and the zero-quota RSS path."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..auth import NoApiKeyError, QuotaExhaustedError
from ..rss_client import get_rss_uploads
from ..youtube_client import YouTubeApiError, YouTubeClient, get_client
from .custom import get_first_published_date

EMAIL_REGEX = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
URL_REGEX = re.compile(r"\bhttps?://[^\s<>\"')]+", re.IGNORECASE)

BRAND_KEYWORDS = [
    "official", "records", "vevo", "music", "news", "network", "media", "trailer",
    "trailers", "studio", "studios", "tv", "television", "clips", "highlights",
    "publisher", "developer", "esports", "radio", "topic",
]
CREATOR_KEYWORDS = [
    "i play", "my channel", "my videos", "my content", "i upload", "let's play",
    "lets play", "streamer", "content creator", "creator", "reviewer",
]


def _uniq(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _int_or_none(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def _format_channel_details(item: dict[str, Any]) -> dict[str, Any]:
    """Same projection as youtube-mcp ``formatters.format_channel_details``."""
    channel_id = item.get("id", "")
    snippet = item.get("snippet", {}) or {}
    statistics = item.get("statistics", {}) or {}
    related = ((item.get("contentDetails") or {}).get("relatedPlaylists")) or {}
    custom_url = snippet.get("customUrl")
    return {
        "channel_id": channel_id,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "custom_url": custom_url,
        "published_at": snippet.get("publishedAt"),
        "subscriber_count": _int_or_none(statistics.get("subscriberCount")),
        "video_count": _int_or_none(statistics.get("videoCount")),
        "view_count": _int_or_none(statistics.get("viewCount")),
        "uploads_playlist_id": related.get("uploads"),
        "url": (
            f"https://www.youtube.com/{custom_url}"
            if custom_url
            else f"https://www.youtube.com/channel/{channel_id}"
        ),
    }


def _batch_channels(client: YouTubeClient, tool: str, channel_ids: list[str]) -> list[dict]:
    if not channel_ids:
        return []
    response = client.channels(tool, channel_ids=channel_ids[:50])
    return [_format_channel_details(item) for item in response.get("items", [])]


def search_channels(
    query: str,
    max_results: int = 10,
    order: str = "relevance",
    region_code: str | None = None,
    client: YouTubeClient | None = None,
) -> dict[str, Any]:
    """Search channels enriched with subscribers/views — youtube-mcp parity.

    Layer: Data API. Quota: 101 units (100 search + 1 channels batch).
    """
    client = client or get_client()
    try:
        search_response = client.search(
            "search_channels",
            query=query,
            max_results=max_results,
            search_type="channel",
            order=order,
            region_code=region_code,
        )
        items = search_response.get("items", [])
        if not items:
            return {"success": True, "query": query, "count": 0, "channels": []}

        channel_ids = [
            (item.get("id") or {}).get("channelId")
            for item in items
            if (item.get("id") or {}).get("channelId")
        ]
        details = {entry["channel_id"]: entry for entry in _batch_channels(client, "search_channels", channel_ids)}

        enriched = []
        for item in items:
            id_info = item.get("id") or {}
            channel_id = id_info.get("channelId")
            info = details.get(channel_id, {})
            snippet = item.get("snippet") or {}
            custom_url = info.get("custom_url")
            enriched.append(
                {
                    "channel_id": channel_id,
                    "channel_title": snippet.get("title") or info.get("title"),
                    "handle": custom_url,
                    "description": snippet.get("description") or info.get("description"),
                    "subscribers": info.get("subscriber_count", 0),
                    "total_views": info.get("view_count", 0),
                    "video_count": info.get("video_count", 0),
                    "url": (
                        f"https://www.youtube.com/channel/{channel_id}"
                        if not custom_url
                        else f"https://www.youtube.com/{custom_url}"
                    ),
                }
            )
    except (NoApiKeyError, QuotaExhaustedError, YouTubeApiError) as error:
        return {"success": False, "error": str(error)}

    return {"success": True, "query": query, "count": len(enriched), "channels": enriched}


def get_channel_details(
    channel_id: str | None = None,
    for_handle: str | None = None,
    for_username: str | None = None,
    raw: bool = False,
    client: YouTubeClient | None = None,
) -> dict[str, Any]:
    """Channel metadata by id, @handle or username — youtube-mcp parity.

    Layer: Data API. Quota: 1 unit.
    """
    if not (channel_id or for_handle or for_username):
        return {
            "success": False,
            "error": "Must provide one of 'channel_id', 'for_handle', or 'for_username'.",
        }

    client = client or get_client()
    try:
        response = client.channels(
            "get_channel_details",
            channel_ids=[channel_id] if channel_id else None,
            for_handle=for_handle,
            for_username=for_username,
        )
    except (NoApiKeyError, QuotaExhaustedError, YouTubeApiError) as error:
        return {"success": False, "error": str(error)}

    if raw:
        return response
    items = response.get("items", [])
    if not items:
        return {"success": False, "error": "Channel not found."}
    return {"success": True, "channel": _format_channel_details(items[0])}


def find_breakout_creators(
    niche: str,
    max_channel_age_months: int = 24,
    min_subscribers: int = 1000,
    max_subscribers: int = 300000,
    region_code: str | None = "US",
    max_results: int = 10,
    first_video_pages: int = 3,
    client: YouTubeClient | None = None,
) -> dict[str, Any]:
    """Channels whose first upload is recent and grew fast — youtube-mcp port.

    Layer: Data API. Quota: 101 units (100 search + 1 channels batch) plus 1–3
    per surviving channel for the first-upload walk. ``channel_age_months`` is
    measured from the FIRST VIDEO, not from the account creation date. That is a
    deliberate break from youtube-mcp's ``find_breakout_growth_channels``, which
    aged channels by account; the output keeps ``created_at`` (account) for shape
    and adds ``first_video_published_at``.
    """
    client = client or get_client()
    try:
        search_response = client.search(
            "find_breakout_creators",
            query=niche,
            max_results=min(max_results * 3, 50),
            search_type="channel",
            region_code=region_code,
        )
        channel_ids = [
            (item.get("id") or {}).get("channelId")
            for item in search_response.get("items", [])
            if (item.get("id") or {}).get("channelId")
        ]
        batch = _batch_channels(client, "find_breakout_creators", channel_ids)
    except (NoApiKeyError, QuotaExhaustedError, YouTubeApiError) as error:
        return {"success": False, "error": str(error)}

    now = datetime.now(timezone.utc)
    breakout: list[dict[str, Any]] = []

    for channel in batch:
        subs = channel.get("subscriber_count") or 0
        if subs < min_subscribers:
            continue
        if max_subscribers and subs > max_subscribers:
            continue

        published_at = channel.get("published_at")
        age_days = None
        age_months = None
        monthly_velocity = None

        # Age is measured from the FIRST VIDEO, not from the account creation
        # date: a dormant account that started uploading last month is exactly
        # the creator this tool is looking for.
        first = get_first_published_date(
            channel.get("channel_id") or "",
            always_enumerate=True,
            max_pages=first_video_pages,
            client=client,
        )
        first_video_at = first.get("first_video_published_at") if first.get("success") else None
        if first_video_at is None or first.get("enumeration_truncated"):
            # No proof of the first upload — never fall back to the account age.
            continue

        try:
            created = datetime.fromisoformat(first_video_at.replace("Z", "+00:00"))
            age_days = max((now - created).days, 1)
            age_months = round(age_days / 30.4, 1)
            if age_months > max_channel_age_months:
                continue
            monthly_velocity = round(subs / max(age_months, 0.5))
        except Exception:
            continue

        video_count = max(channel.get("video_count") or 1, 1)
        breakout.append(
            {
                "channel_name": channel.get("title"),
                "handle": channel.get("custom_url"),
                "subscribers": subs,
                "video_count": channel.get("video_count"),
                "total_views": channel.get("view_count"),
                "first_video_published_at": first_video_at,
                "channel_age_months": age_months,
                "subs_gained_per_month": monthly_velocity,
                "subs_per_video": round(subs / video_count),
                "created_at": published_at,
                "url": channel.get("url"),
            }
        )

    breakout.sort(
        key=lambda entry: entry.get("subs_gained_per_month") or entry.get("subs_per_video") or 0,
        reverse=True,
    )
    return {
        "success": True,
        "niche": niche,
        "total_found": len(breakout),
        "breakout_channels": breakout[:max_results],
    }


def _classify_channel(channel: dict[str, Any]) -> dict[str, Any]:
    snippet = channel.get("snippet") or {}
    branding = ((channel.get("brandingSettings") or {}).get("channel")) or {}
    title = str(snippet.get("title") or "").lower()
    description = str(branding.get("description") or snippet.get("description") or "").lower()
    text = f"{title} {description}"
    brand_hits = [keyword for keyword in BRAND_KEYWORDS if keyword in text]
    creator_hits = [keyword for keyword in CREATOR_KEYWORDS if keyword in text]

    heuristic = "unknown"
    if len(brand_hits) >= 2 and len(brand_hits) > len(creator_hits):
        heuristic = "brand_or_media"
    elif creator_hits and len(creator_hits) >= len(brand_hits):
        heuristic = "creator"

    return {
        "channelTypeHeuristic": heuristic,
        "brandKeywordHits": brand_hits,
        "creatorKeywordHits": creator_hits,
        "heuristicConfidence": max(len(brand_hits), len(creator_hits)),
    }


def _normalize_channel(channel: dict[str, Any], latest_published_at: str | None) -> dict[str, Any]:
    """Port of youtube-mcp-server ``normalizeChannel``."""
    snippet = channel.get("snippet") or {}
    statistics = channel.get("statistics") or {}
    branding = ((channel.get("brandingSettings") or {}).get("channel")) or {}
    description = branding.get("description") or snippet.get("description") or ""
    custom_url = snippet.get("customUrl") or None
    channel_id = channel.get("id") or None

    normalized = {
        "channelId": channel_id,
        "title": snippet.get("title") or None,
        "description": description,
        "customUrl": custom_url,
        "url": (
            f"https://www.youtube.com/{custom_url}"
            if custom_url
            else (f"https://www.youtube.com/channel/{channel_id}" if channel_id else None)
        ),
        "country": branding.get("country") or snippet.get("country") or None,
        "defaultLanguage": branding.get("defaultLanguage") or snippet.get("defaultLanguage") or None,
        "joinedAt": snippet.get("publishedAt") or None,
        "uploadsPlaylistId": (
            ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        ),
        "subscriberCount": int(statistics.get("subscriberCount") or 0),
        "videoCount": int(statistics.get("videoCount") or 0),
        "viewCount": int(statistics.get("viewCount") or 0),
        "emailsFound": _uniq(EMAIL_REGEX.findall(description)),
        "contactLinks": _uniq(URL_REGEX.findall(description)),
        "businessEmail": None,
        "contactExtractionNote": (
            "YouTube Data API does not expose business email. "
            "Emails and links are parsed from channel description only."
        ),
    }
    normalized.update(_classify_channel(channel))
    return {
        **channel,
        "latestVideoPublishedAt": latest_published_at,
        "normalizedMetadata": normalized,
    }


def channels_getChannel(
    channelId: str,
    include_latest_upload: bool = True,
    client: YouTubeClient | None = None,
) -> dict[str, Any]:
    """Raw channel + normalizedMetadata — youtube-mcp-server (zubeid) parity.

    Layer: Data API. Quota: 1 unit, plus **100 units** when
    ``include_latest_upload`` is true: the original finds the newest upload with
    one ``search.list`` per channel (an expensive shortcut this port keeps for
    parity but makes optional).
    """
    client = client or get_client()
    try:
        response = client.channels(
            "channels_getChannel",
            channel_ids=[channelId],
            parts="snippet,statistics,contentDetails,brandingSettings",
        )
        items = response.get("items", [])
        if not items:
            raise YouTubeApiError(f"Channel not found: {channelId}")

        latest = None
        if include_latest_upload:
            latest_response = client.search(
                "channels_getChannel",
                query=None,
                max_results=1,
                search_type="video",
                order="date",
                channel_id=channelId,
            )
            latest_items = latest_response.get("items", [])
            if latest_items:
                latest = ((latest_items[0].get("snippet") or {}).get("publishedAt"))
    except (NoApiKeyError, QuotaExhaustedError, YouTubeApiError) as error:
        return {"success": False, "error": str(error)}

    return _normalize_channel(items[0], latest)


def get_channel_rss_videos(
    channel_id: str,
    max_results: int = 15,
    client: YouTubeClient | None = None,
) -> dict[str, Any]:
    """Latest uploads from the public Atom feed — youtube-mcp parity.

    Layer 4 (RSS). Quota: 0 units, no API key. Hard cap of 15 entries — that is
    all the feed exposes.
    """
    result = get_rss_uploads(channel_id, max_results=max_results)
    if result.get("success"):
        (client or get_client()).tracker.mark_layer("rss")
    return result

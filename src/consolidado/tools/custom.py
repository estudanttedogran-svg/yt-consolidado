"""The three tools that did not exist in any of the three originals.

All three share the same shape: one ``channels.list`` (1 unit) to discover the
uploads playlist, then a bounded number of ``playlistItems.list`` pages (1 unit
each) and ``videos.list`` batches (1 unit per 50 ids).

Each function reports ``quota_cost`` from the tracker delta, not from a constant.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from ..auth import QuotaTracker
from ..youtube_client import TRACKER, YouTubeClient, get_client

ISO_DURATION = re.compile(r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$")


def parse_iso8601_seconds(value: str | None) -> int | None:
    """``PT12M34S`` / ``PT1H`` / ``P1DT2H`` → seconds. ``None`` for unparsable."""
    if not value:
        return None
    match = ISO_DURATION.match(value.strip())
    if not match:
        return None
    parts = {key: int(raw) if raw else 0 for key, raw in match.groupdict().items()}
    total = parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    return total


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if len(text) == 10:  # date only
        text = f"{text}T00:00:00+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _quota_delta(tracker: QuotaTracker, before: int) -> int:
    return tracker.total() - before


def _fetch_channel(client: YouTubeClient, tool: str, channel_id: str) -> dict[str, Any]:
    parts = "snippet,contentDetails,statistics"
    if channel_id.strip().startswith("@"):
        response = client.channels(tool, for_handle=channel_id, parts=parts)
    else:
        response = client.channels(tool, channel_ids=[channel_id], parts=parts)
    items = response.get("items") or []
    if not items:
        raise LookupError(f"Channel not found: {channel_id}")
    return items[0]


def _uploads_playlist(channel: dict[str, Any]) -> str:
    related = (channel.get("contentDetails") or {}).get("relatedPlaylists") or {}
    playlist_id = related.get("uploads")
    if not playlist_id:
        raise LookupError(
            f"Channel {channel.get('id')} exposes no uploads playlist "
            "(contentDetails.relatedPlaylists.uploads is missing)."
        )
    return playlist_id


def _iter_upload_pages(
    client: YouTubeClient,
    tool: str,
    playlist_id: str,
    max_pages: int,
    page_size: int = 50,
):
    """Yield ``(items, page)`` for up to ``max_pages`` pages, newest first."""
    token: str | None = None
    for page in range(1, max_pages + 1):
        response = client.playlist_items(
            tool, playlist_id=playlist_id, max_results=page_size, page_token=token
        )
        items = response.get("items") or []
        yield items, page
        token = response.get("nextPageToken")
        if not items or not token:
            return


def get_first_published_date(
    channel_id: str,
    enumerate_if_younger_than_days: int = 90,
    enumerate_if_video_count_under: int = 0,
    always_enumerate: bool = False,
    max_pages: int = 20,
    client: YouTubeClient | None = None,
    tracker: QuotaTracker | None = None,
) -> dict[str, Any]:
    """Oldest publication date of a channel — proxy first, hard proof when asked.

    Layer: YouTube Data API (needs a key). Quota: 1 unit for the channel, plus
    1 unit per 50 uploads when enumeration runs. Enumeration triggers when the
    channel is younger than ``enumerate_if_younger_than_days``, when it has at
    most ``enumerate_if_video_count_under`` uploads (0 disables the second
    route), or always — ``always_enumerate=True``. The second route matters
    because channel creation and first upload are different dates: an old channel
    that started uploading last week is exactly the case this tool exists to
    catch.

    ``always_enumerate=True`` is the mode the age filters use: the account
    creation date is never accepted as a stand-in for the first upload. If the
    walk cannot reach the first video, ``first_video_published_at`` stays
    ``None``, ``proxy_used`` is ``"unavailable"`` and ``enumeration_truncated``
    is True — the caller must reject the channel rather than guess. ``max_pages``
    caps the walk so a huge channel cannot burn the daily budget.
    """
    client = client or get_client()
    tracker = tracker or client.tracker
    before = tracker.total()

    try:
        channel = _fetch_channel(client, "get_first_published_date", channel_id)
    except LookupError as error:
        return {
            "success": False,
            "channel_id": channel_id,
            "error": str(error),
            "quota_cost": _quota_delta(tracker, before),
        }
    created_at = (channel.get("snippet") or {}).get("publishedAt")
    created_dt = _parse_timestamp(created_at)
    now = datetime.now(timezone.utc)
    days_since = max((now - created_dt).days, 0) if created_dt else None
    video_count = int(((channel.get("statistics") or {}).get("videoCount")) or 0)

    first_video_at: str | None = None
    pages_scanned = 0
    truncated = False
    proxy = "channel_creation"

    young = days_since is not None and days_since <= enumerate_if_younger_than_days
    small = 0 < video_count <= enumerate_if_video_count_under
    if always_enumerate or young or small:
        try:
            playlist_id = _uploads_playlist(channel)
        except LookupError as error:
            return {
                "success": False,
                "channel_id": channel_id,
                "error": str(error),
                "quota_cost": _quota_delta(tracker, before),
            }
        # Never spend more pages than the channel has, and never fewer than needed.
        # ``always_enumerate`` keeps the full ``max_pages`` budget: narrowing it by
        # video_count would make a channel whose history ends exactly on the cap
        # page look truncated when it is not.
        budget = max_pages
        if small and not young and not always_enumerate:
            budget = min(max_pages, max(1, -(-video_count // 50)))
        for items, page in _iter_upload_pages(
            client, "get_first_published_date", playlist_id, max_pages=budget
        ):
            pages_scanned = page
            if items:
                candidate = (items[-1].get("snippet") or {}).get("publishedAt")
                if candidate:
                    first_video_at = candidate
            if page == budget:
                # The walk stopped because it hit the page cap — the oldest item
                # seen is not necessarily the channel's first video.
                truncated = True

        if first_video_at:
            proxy = "uploads_playlist_enumeration"
        elif always_enumerate:
            proxy = "unavailable"

    result: dict[str, Any] = {
        "success": True,
        "channel_id": channel.get("id", channel_id),
        "channel_created_at": created_at,
        "first_video_published_at": first_video_at,
        "proxy_used": proxy,
        "days_since_channel_creation": days_since,
        "pages_scanned": pages_scanned,
        "enumeration_truncated": truncated,
        "quota_cost": _quota_delta(tracker, before),
    }
    return result


def _scan_window(
    client: YouTubeClient,
    tool: str,
    channel_id: str,
    since_dt: datetime,
    max_pages: int,
    batch_parts: str,
) -> dict[str, Any]:
    """Collect every upload published at or after ``since_dt`` (bounded)."""
    channel = _fetch_channel(client, tool, channel_id)
    playlist_id = _uploads_playlist(channel)

    in_window: list[dict[str, Any]] = []
    pages = 0
    truncated = False
    for items, page in _iter_upload_pages(client, tool, playlist_id, max_pages=max_pages):
        pages = page
        reached_past_window = False
        for item in items:
            snippet = item.get("snippet") or {}
            published = _parse_timestamp(snippet.get("publishedAt"))
            video_id = (snippet.get("resourceId") or {}).get("videoId") or item.get("id")
            if published is None or published < since_dt:
                reached_past_window = True
                continue
            in_window.append(
                {
                    "video_id": video_id,
                    "published_at": snippet.get("publishedAt"),
                    "title": snippet.get("title", ""),
                }
            )
        if reached_past_window or page == max_pages:
            truncated = page == max_pages and not reached_past_window
            break

    stats: dict[str, Any] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for start in range(0, len(in_window), 50):
        batch = in_window[start : start + 50]
        response = client.videos(tool, [entry["video_id"] for entry in batch], parts=batch_parts)
        for item in response.get("items") or []:
            stats[item.get("id", "")] = item

    for entry in in_window:
        item = stats.get(entry["video_id"], {})
        entry["view_count"] = int((item.get("statistics") or {}).get("viewCount", 0) or 0)
        entry["duration"] = (item.get("contentDetails") or {}).get("duration")
        entry["snippet_title"] = (item.get("snippet") or {}).get("title") or entry["title"]
        by_id[entry["video_id"]] = entry

    return {
        "channel": channel,
        "pages_scanned": pages,
        "truncated": truncated,
        "videos": in_window,
    }


def max_views_in_channel(
    channel_id: str,
    since_date: str,
    max_pages: int = 10,
    client: YouTubeClient | None = None,
    tracker: QuotaTracker | None = None,
) -> dict[str, Any]:
    """Highest view count among uploads published on/after ``since_date``.

    Layer: YouTube Data API (needs a key). Quota: 1 (channel) + 1 per 50 uploads
    scanned + 1 per 50 videos inside the window. ``max_pages`` bounds the scan
    at 500 uploads by default.
    """
    client = client or get_client()
    tracker = tracker or client.tracker
    before = tracker.total()

    since_dt = _parse_timestamp(since_date)
    if since_dt is None:
        return {
            "success": False,
            "channel_id": channel_id,
            "since_date": since_date,
            "error": "since_date must be ISO 8601 (YYYY-MM-DD or full RFC 3339).",
        }

    try:
        scan = _scan_window(
            client, "max_views_in_channel", channel_id, since_dt, max_pages, "statistics,snippet"
        )
    except LookupError as error:
        return {
            "success": False,
            "channel_id": channel_id,
            "since_date": since_date,
            "error": str(error),
            "quota_cost": _quota_delta(tracker, before),
        }
    videos = scan["videos"]
    winner = max(videos, key=lambda entry: entry["view_count"], default=None)

    return {
        "success": True,
        "channel_id": scan["channel"].get("id", channel_id),
        "since_date": since_date,
        "max_views": winner["view_count"] if winner else 0,
        "max_views_video_id": winner["video_id"] if winner else None,
        "videos_checked": len(videos),
        "pages_scanned": scan["pages_scanned"],
        "truncated": scan["truncated"],
        "quota_cost": _quota_delta(tracker, before),
    }


def _max_views_rich(
    channel_id: str,
    since_dt: datetime,
    max_pages: int = 10,
    client: YouTubeClient | None = None,
) -> dict[str, Any]:
    """Internal variant used by the orchestrator: adds title/url of the winner."""
    client = client or get_client()
    scan = _scan_window(
        client, "max_views_in_channel", channel_id, since_dt, max_pages, "statistics,snippet"
    )
    videos = scan["videos"]
    winner = max(videos, key=lambda entry: entry["view_count"], default=None)
    return {
        "max_views": winner["view_count"] if winner else 0,
        "video_id": winner["video_id"] if winner else None,
        "title": winner["snippet_title"] if winner else None,
        "videos_checked": len(videos),
        "truncated": scan["truncated"],
    }


def _uploads_playlist_fast(channel_id: str) -> str | None:
    """UC… → UU… is the documented uploads-playlist convention; saves 1 unit."""
    value = channel_id.strip()
    if len(value) == 24 and value.startswith("UC"):
        return "UU" + value[2:]
    return None


def avg_duration(
    channel_id: str,
    sample_size: int = 20,
    client: YouTubeClient | None = None,
    tracker: QuotaTracker | None = None,
) -> dict[str, Any]:
    """Mean duration of the last ``sample_size`` uploads.

    Layer: YouTube Data API (needs a key). Quota: 1 (playlist page) + 1 per 50
    ids — 2 units for a 20-video sample. Falls back to a ``channels.list``
    lookup (+1) only when the id is not a canonical ``UC…`` channel id.
    """
    client = client or get_client()
    tracker = tracker or client.tracker
    before = tracker.total()

    playlist_id = _uploads_playlist_fast(channel_id)
    if playlist_id is None:
        try:
            channel = _fetch_channel(client, "avg_duration", channel_id)
            playlist_id = _uploads_playlist(channel)
        except LookupError as error:
            return {
                "success": False,
                "channel_id": channel_id,
                "sample_size": sample_size,
                "error": str(error),
                "quota_cost": _quota_delta(tracker, before),
            }
        resolved_id = channel.get("id", channel_id)
    else:
        resolved_id = channel_id

    page = client.playlist_items(
        "avg_duration", playlist_id=playlist_id, max_results=min(max(1, sample_size), 50)
    )
    video_ids = [
        ((item.get("snippet") or {}).get("resourceId") or {}).get("videoId")
        for item in (page.get("items") or [])
    ]
    video_ids = [vid for vid in video_ids if vid]

    iso_values: list[str] = []
    seconds: list[int] = []
    for start in range(0, len(video_ids), 50):
        batch = video_ids[start : start + 50]
        response = client.videos("avg_duration", batch, parts="contentDetails")
        for item in response.get("items") or []:
            raw = (item.get("contentDetails") or {}).get("duration")
            if not raw:
                continue
            parsed = parse_iso8601_seconds(raw)
            iso_values.append(raw)
            if parsed is not None and parsed > 0:
                seconds.append(parsed)

    if not seconds:
        return {
            "success": False,
            "channel_id": resolved_id,
            "sample_size": sample_size,
            "error": "No parsable durations in the returned sample (empty channel or lives only).",
            "parsed_iso": iso_values,
            "quota_cost": _quota_delta(tracker, before),
        }

    average = mean(seconds)
    return {
        "success": True,
        "channel_id": resolved_id,
        "sample_size": sample_size,
        "avg_duration_minutes": round(average / 60, 1),
        "min_minutes": round(min(seconds) / 60, 1),
        "max_minutes": round(max(seconds) / 60, 1),
        "parsed_iso": iso_values,
        "videos_analyzed": len(seconds),
        "quota_cost": _quota_delta(tracker, before),
    }

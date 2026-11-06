"""MCP entry point: 15 tools, stdio by default, HTTP optional.

Every wrapper here is thin on purpose — the logic lives in ``tools/*`` so the
same function can be unit-tested with an injected fake client. Each docstring
declares the auth layer and the quota cost the agent should expect.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[assignment]

from . import __version__
from .auth import NoApiKeyError
from .youtube_client import POOL, TRACKER
from .tools import channels as channel_tools
from .tools import custom as custom_tools
from .tools import orchestrator as orchestrator_tools
from .tools import search as search_tools
from .tools import videos as video_tools

mcp = MCPServer("consolidado", version=__version__)


# --------------------------------------------------------------------------- search
@mcp.tool()
def search_videos(
    query: str,
    max_results: int = 10,
    search_type: str = "video",
    order: str = "relevance",
    published_after: str | None = None,
    region_code: str | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """Search videos/channels/playlists — port of youtube-mcp ``search_videos``.

    Layer: YouTube Data API (key required). Quota: 100 units.
    Returns {success, query, total_results, next_page_token, results[]}.
    """
    return search_tools.search_videos(
        query=query,
        max_results=max_results,
        search_type=search_type,
        order=order,
        published_after=published_after,
        region_code=region_code,
        raw=raw,
    )


@mcp.tool()
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
) -> dict[str, Any]:
    """Search with date/channel/duration filters — port of tube-bridge ``youtube_search``.

    Layer 1/2 (Data API, 100 units) → Layer 3 (yt-dlp, 0 units) on exhaustion.
    Returns {query, source, total_results, next_page_token, videos[]}.
    """
    return search_tools.youtube_search(
        query=query,
        limit=limit,
        order=order,
        published_after=published_after,
        published_before=published_before,
        channel_id=channel_id,
        video_duration=video_duration,
        video_definition=video_definition,
        safe_search=safe_search,
    )


# ------------------------------------------------------------------------- channels
@mcp.tool()
def search_channels(
    query: str,
    max_results: int = 10,
    order: str = "relevance",
    region_code: str | None = None,
) -> dict[str, Any]:
    """Search channels enriched with stats — port of youtube-mcp ``search_channels``.

    Layer: Data API. Quota: 101 units (100 search + 1 channels batch).
    """
    return channel_tools.search_channels(
        query=query, max_results=max_results, order=order, region_code=region_code
    )


@mcp.tool()
def get_channel_details(
    channel_id: str | None = None,
    for_handle: str | None = None,
    for_username: str | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """Channel metadata by id/@handle/username — port of youtube-mcp.

    Layer: Data API. Quota: 1 unit. Includes ``uploads_playlist_id``.
    """
    return channel_tools.get_channel_details(
        channel_id=channel_id, for_handle=for_handle, for_username=for_username, raw=raw
    )


@mcp.tool()
def find_breakout_creators(
    niche: str,
    max_channel_age_months: int = 24,
    min_subscribers: int = 1000,
    max_subscribers: int = 300000,
    region_code: str | None = "US",
    max_results: int = 10,
) -> dict[str, Any]:
    """New channels with subscriber velocity — port of youtube-mcp ``find_breakout_growth_channels``.

    Layer: Data API. Quota: 101 units.
    """
    return channel_tools.find_breakout_creators(
        niche=niche,
        max_channel_age_months=max_channel_age_months,
        min_subscribers=min_subscribers,
        max_subscribers=max_subscribers,
        region_code=region_code,
        max_results=max_results,
    )


@mcp.tool()
def channels_getChannel(channelId: str, include_latest_upload: bool = True) -> dict[str, Any]:
    """Raw channel + normalizedMetadata — port of youtube-mcp-server (zubeid).

    Layer: Data API. Quota: 1 unit; **+100 units** when ``include_latest_upload``
    is true (the original resolves the newest upload with one search.list).
    Set it to false when you only need country/joinedAt/emailsFound.
    """
    return channel_tools.channels_getChannel(
        channelId=channelId, include_latest_upload=include_latest_upload
    )


@mcp.tool()
def get_channel_rss_videos(channel_id: str, max_results: int = 15) -> dict[str, Any]:
    """Latest uploads from the Atom feed — port of youtube-mcp ``get_channel_rss_videos``.

    Layer 4 (RSS). Quota: 0 units, no key. Feed exposes at most 15 entries.
    """
    return channel_tools.get_channel_rss_videos(channel_id, max_results=max_results)


# --------------------------------------------------------------------------- videos
@mcp.tool()
def youtube_get_video_info(url: str) -> dict[str, Any]:
    """Rich metadata for one video — port of tube-bridge ``youtube_get_video_info``.

    Layer 1/2 (1 unit) → Layer 3 (yt-dlp, 0 units). ``duration`` in seconds.
    """
    return video_tools.youtube_get_video_info(url)


@mcp.tool()
def youtube_get_channel_videos(channel_url: str, limit: int = 10) -> dict[str, Any]:
    """Recent uploads via yt-dlp — port of tube-bridge ``youtube_get_channel_videos``.

    Layer 3 (0 units, no key). Falls back to Layer 4 (RSS) if yt-dlp is 403'd
    and the argument is a ``UC…`` channel id.
    """
    return video_tools.youtube_get_channel_videos(channel_url, limit=limit)


@mcp.tool()
def youtube_get_transcript(
    url: str, lang: str | None = None, with_timestamps: bool = False
) -> dict[str, Any]:
    """Video transcript — port of tube-bridge ``youtube_get_transcript``.

    Layer: public timedtext. Quota: 0 units, no key.
    """
    return video_tools.youtube_get_transcript(url, lang=lang, with_timestamps=with_timestamps)


# --------------------------------------------------------------------------- custom
@mcp.tool()
def get_first_published_date(
    channel_id: str,
    enumerate_if_younger_than_days: int = 90,
    enumerate_if_video_count_under: int = 0,
    max_pages: int = 20,
) -> dict[str, Any]:
    """Oldest publication date of a channel. **Did not exist in any original.**

    Layer: Data API. Quota: 1 unit (channel creation proxy), plus 1 unit per 50
    uploads when enumeration runs — when the channel is younger than
    ``enumerate_if_younger_than_days`` or has at most
    ``enumerate_if_video_count_under`` uploads (0 = never enumerate on size).
    """
    return custom_tools.get_first_published_date(
        channel_id,
        enumerate_if_younger_than_days=enumerate_if_younger_than_days,
        enumerate_if_video_count_under=enumerate_if_video_count_under,
        max_pages=max_pages,
    )


@mcp.tool()
def max_views_in_channel(
    channel_id: str, since_date: str, max_pages: int = 10
) -> dict[str, Any]:
    """Highest view count among uploads since ``since_date``. **New tool.**

    Layer: Data API. Quota: 1 + 1 per 50 uploads scanned + 1 per 50 videos in
    the window (``max_pages`` default caps at 500 uploads).
    """
    return custom_tools.max_views_in_channel(channel_id, since_date, max_pages=max_pages)


@mcp.tool()
def avg_duration(channel_id: str, sample_size: int = 20) -> dict[str, Any]:
    """Mean duration of the last N uploads. **New tool.**

    Layer: Data API. Quota: 2 units for N ≤ 50 (1 playlist page + 1 videos batch).
    """
    return custom_tools.avg_duration(channel_id, sample_size=sample_size)


# ---------------------------------------------------------------------- orchestrator
@mcp.tool()
def find_emerging_channels(
    query: str,
    n: int = 10,
    max_channel_age_days: int = 30,
    min_video_views: int = 100000,
    region_code: str = "US",
    relevance_language: str = "en",
    min_avg_duration_min: float = 8.0,
    max_avg_duration_min: float = 30.0,
    subscriber_min: int = 0,
    subscriber_max: int = 100000,
    dry_run: bool = False,
    discovery_pages: int = 1,
    max_candidates_checked: int = 25,
    sample_size: int = 10,
    small_channel_video_cap: int = 100,
) -> dict[str, Any]:
    """Emerging channels by composite filters — the product tool.

    Layer: Data API. Quota: 100 per discovery page + 1 per 50 channels enriched
    + 3–5 per channel verified (~130–450 for a default run).
    ``small_channel_video_cap`` also enumerates channels with few uploads, so an
    old channel that only started publishing last week is still caught.
    Shortfalls are reported, never padded: filters are not relaxed automatically.
    """
    return orchestrator_tools.find_emerging_channels(
        query=query,
        n=n,
        max_channel_age_days=max_channel_age_days,
        min_video_views=min_video_views,
        region_code=region_code,
        relevance_language=relevance_language,
        min_avg_duration_min=min_avg_duration_min,
        max_avg_duration_min=max_avg_duration_min,
        subscriber_min=subscriber_min,
        subscriber_max=subscriber_max,
        dry_run=dry_run,
        discovery_pages=discovery_pages,
        max_candidates_checked=max_candidates_checked,
        sample_size=sample_size,
        small_channel_video_cap=small_channel_video_cap,
    )


# --------------------------------------------------------------------------- quota
@mcp.tool()
def get_quota_status() -> dict[str, Any]:
    """Session quota ledger: units spent per tool and per endpoint, layers used.

    No API call, 0 units. The Data API does not expose remaining quota, so this
    is a session-local tally against the 10,000/day free tier per key.
    """
    return TRACKER.snapshot(POOL)


def main() -> None:
    parser = argparse.ArgumentParser(description="Consolidated YouTube MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--print-tools",
        action="store_true",
        help="Print the registered tool catalog as JSON and exit",
    )
    args = parser.parse_args()

    if args.print_tools:
        tools = mcp._tool_manager.list_tools() if hasattr(mcp, "_tool_manager") else []
        print(json.dumps([tool.name for tool in tools], indent=2))
        return

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return
    try:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    except TypeError:
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()

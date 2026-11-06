"""Test doubles: a fake YouTube client with the same surface as the real one.

The real client charges quota inside ``_request``; the fake charges the same
official costs so ``quota_cost`` assertions mean something.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from consolidado.auth import ENDPOINT_COST, QuotaTracker


def channel_id(seed: str) -> str:
    """Build a canonical 24-char ``UC…`` id so the UU fast path applies."""
    return "UC" + (seed * 22)[:22]


def iso(days_ago: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeClient:
    def __init__(self) -> None:
        self.tracker = QuotaTracker()
        self.channels_by_id: dict[str, dict[str, Any]] = {}
        self.playlists: dict[str, list[list[dict[str, Any]]]] = {}
        self.videos_by_id: dict[str, dict[str, Any]] = {}
        self.search_items: list[dict[str, Any]] = []
        self.search_pages = 1
        self.calls = {"search": 0, "channels": 0, "playlistItems": 0, "videos": 0, "playlists": 0}

    # -------------------------------------------------------------- construction
    def add_channel(
        self,
        cid: str,
        title: str,
        created_days_ago: int,
        videos: list[tuple[str, int, int, str]],
        country: str | None = "US",
        subscribers: int = 12000,
        custom_url: str | None = None,
    ) -> None:
        """``videos`` = [(video_id, published_days_ago, views, iso_duration)]."""
        uploads = "UU" + cid[2:]
        self.channels_by_id[cid] = {
            "id": cid,
            "snippet": {
                "title": title,
                "publishedAt": iso(created_days_ago),
                "description": f"{title} -- contact: {title.lower().replace(' ', '')}@example.com",
                "customUrl": custom_url,
                "country": country,
            },
            "statistics": {
                "subscriberCount": str(subscribers),
                "videoCount": str(len(videos)),
                "viewCount": str(sum(views for _, _, views, _ in videos)),
            },
            "contentDetails": {"relatedPlaylists": {"uploads": uploads}},
            "brandingSettings": {"channel": {"description": f"{title} my channel", "country": country}},
        }
        items = []
        for video_id, days_ago, _, _ in videos:
            items.append(
                {
                    "snippet": {
                        "publishedAt": iso(days_ago),
                        "resourceId": {"videoId": video_id},
                        "title": f"{title} video {video_id}",
                    }
                }
            )
        self.playlists[uploads] = [items] if items else [[]]
        for video_id, _, views, duration in videos:
            self.videos_by_id[video_id] = {
                "id": video_id,
                "snippet": {"title": f"{title} video {video_id}"},
                "statistics": {"viewCount": str(views)},
                "contentDetails": {"duration": duration},
            }

    def add_search_hit(self, cid: str, title: str, video_id: str, days_ago: int = 3) -> None:
        self.search_items.append(
            {
                "id": {"kind": "youtube#video", "videoId": video_id},
                "snippet": {
                    "channelId": cid,
                    "channelTitle": title,
                    "title": f"{title} recent upload",
                    "publishedAt": iso(days_ago),
                    "description": "recent",
                },
            }
        )

    # ------------------------------------------------------------------ charging
    def _charge(self, tool: str, endpoint: str) -> None:
        self.calls[endpoint] += 1
        self.tracker.charge(tool, endpoint, ENDPOINT_COST[endpoint])

    # -------------------------------------------------------------------- surface
    def search(self, tool: str, **kwargs: Any) -> dict[str, Any]:
        self._charge(tool, "search")
        index = 0
        token = kwargs.get("page_token")
        if token and token.startswith("page"):
            index = int(token.replace("page", ""))
        window = self.search_items[index * 50 : (index + 1) * 50]
        has_more = index + 1 < self.search_pages
        response: dict[str, Any] = {
            "items": window,
            "pageInfo": {"totalResults": len(self.search_items)},
        }
        if has_more:
            response["nextPageToken"] = f"page{index + 1}"
        return response

    def channels(
        self,
        tool: str,
        channel_ids: list[str] | None = None,
        for_handle: str | None = None,
        for_username: str | None = None,
        parts: str = "snippet,contentDetails,statistics",
    ) -> dict[str, Any]:
        self._charge(tool, "channels")
        if channel_ids:
            items = [self.channels_by_id[cid] for cid in channel_ids if cid in self.channels_by_id]
        elif for_handle:
            handle = for_handle.lstrip("@")
            items = [
                channel
                for channel in self.channels_by_id.values()
                if (channel["snippet"].get("customUrl") or "").lstrip("@") == handle
            ]
        else:
            items = []
        return {"items": items}

    def playlist_items(
        self,
        tool: str,
        playlist_id: str,
        max_results: int = 50,
        page_token: str | None = None,
        parts: str = "snippet,contentDetails",
    ) -> dict[str, Any]:
        self._charge(tool, "playlistItems")
        pages = self.playlists.get(playlist_id, [[]])
        index = int(page_token.replace("page", "")) if page_token else 0
        index = min(index, len(pages) - 1)
        response: dict[str, Any] = {"items": pages[index]}
        if index + 1 < len(pages):
            response["nextPageToken"] = f"page{index + 1}"
        return response

    def videos(
        self,
        tool: str,
        video_ids: list[str],
        parts: str = "snippet,contentDetails,statistics",
    ) -> dict[str, Any]:
        self._charge(tool, "videos")
        return {"items": [self.videos_by_id[vid] for vid in video_ids if vid in self.videos_by_id]}


@pytest.fixture
def fake() -> FakeClient:
    """Four channels: one passes every filter, three fail exactly one each."""
    client = FakeClient()

    good = channel_id("A")
    client.add_channel(
        good,
        "Automation Lab",
        created_days_ago=10,
        videos=[
            ("vidGood1", 5, 250_000, "PT12M00S"),
            ("vidGood2", 8, 40_000, "PT10M00S"),
            ("vidGood3", 10, 100_000, "PT11M00S"),
        ],
        subscribers=18_400,
        custom_url="@automationlab",
    )
    client.add_search_hit(good, "Automation Lab", "vidGood1")

    old = channel_id("B")
    client.add_channel(
        old,
        "Legacy Automation",
        created_days_ago=1200,
        videos=[("vidOld1", 900, 5_000_000, "PT14M00S")],
        subscribers=90_000,
    )
    client.add_search_hit(old, "Legacy Automation", "vidOld1")

    weak = channel_id("C")
    client.add_channel(
        weak,
        "Tiny Automation",
        created_days_ago=20,
        videos=[("vidWeak1", 15, 5_000, "PT13M00S")],
        subscribers=900,
    )
    client.add_search_hit(weak, "Tiny Automation", "vidWeak1")

    short = channel_id("D")
    client.add_channel(
        short,
        "Shortform Automation",
        created_days_ago=25,
        videos=[("vidShort1", 20, 500_000, "PT3M00S")],
        subscribers=4_000,
    )
    client.add_search_hit(short, "Shortform Automation", "vidShort1")

    return client


@pytest.fixture
def ids(fake: FakeClient) -> dict[str, str]:
    return {
        "good": channel_id("A"),
        "old": channel_id("B"),
        "weak": channel_id("C"),
        "short": channel_id("D"),
    }

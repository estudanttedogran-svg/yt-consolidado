"""Tests for the channel tools — the age basis of ``find_breakout_creators``.

The tool ages channels by their FIRST UPLOAD, not by the account creation date.
``FakeClient.add_channel`` puts every upload in one playlist page, so the
``always_enumerate`` walk always reaches the oldest item here.
"""

from __future__ import annotations

from consolidado.tools.channels import find_breakout_creators
from conftest import FakeClient, channel_id, iso


def _channel_search_hit(fake: FakeClient, cid: str, title: str) -> None:
    fake.search_items.append(
        {
            "id": {"kind": "youtube#channel", "channelId": cid},
            "snippet": {"title": title},
        }
    )


def test_breakout_ages_by_first_video_not_account(fake: FakeClient):
    """Account 1500 days old, first upload 20 days ago, 150 videos → kept.

    The account date alone would have thrown this channel out: it is the
    first-video basis that keeps it.
    """
    cid = channel_id("Q")
    fake.add_channel(
        cid,
        "Old Account New Uploads",
        created_days_ago=1500,
        videos=[(f"vidQ{i}", 20, 1_000, "PT10M00S") for i in range(150)],
        subscribers=50_000,
        custom_url="@oldaccountq",
    )
    _channel_search_hit(fake, cid, "Old Account New Uploads")

    result = find_breakout_creators("history", max_channel_age_months=24, client=fake)

    assert result["success"] is True
    assert [entry["channel_name"] for entry in result["breakout_channels"]] == [
        "Old Account New Uploads"
    ]
    entry = result["breakout_channels"][0]
    assert entry["first_video_published_at"].startswith(iso(20)[:10])
    assert entry["channel_age_months"] < 1  # measured from the first upload
    assert entry["subs_gained_per_month"] > 0


def test_breakout_drops_channel_whose_first_video_is_too_old(fake: FakeClient):
    """A young account is not enough: an old first upload fails ``max_channel_age_months``."""
    cid = channel_id("Q")
    fake.add_channel(
        cid,
        "Ancient Uploader",
        created_days_ago=1500,
        videos=[(f"vidQ{i}", 1200, 1_000, "PT10M00S") for i in range(150)],
        subscribers=50_000,
    )
    _channel_search_hit(fake, cid, "Ancient Uploader")

    result = find_breakout_creators("history", max_channel_age_months=24, client=fake)

    assert result["success"] is True
    assert result["total_found"] == 0
    assert result["breakout_channels"] == []

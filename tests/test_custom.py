"""Tests for the three tools that had no original.

Every assertion is on behaviour that the spec pins down: proxy choice, date
filtering, mean/rounding, and the quota each call is allowed to spend.
"""

from __future__ import annotations

import pytest

from consolidado.tools.custom import (
    avg_duration,
    get_first_published_date,
    max_views_in_channel,
    parse_iso8601_seconds,
)
from conftest import FakeClient, channel_id, iso


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PT1H23M45S", 5025),
        ("PT12M34S", 754),
        ("PT45S", 45),
        ("PT1H", 3600),
        ("PT0S", 0),
        ("P1DT2H", 93600),
        ("P2D", 172800),
        (None, None),
        ("", None),
        ("13:45", None),
    ],
)
def test_parse_iso8601_seconds(raw, expected):
    assert parse_iso8601_seconds(raw) == expected


def test_first_published_date_enumerates_when_channel_is_young(fake: FakeClient, ids):
    result = get_first_published_date(ids["good"], client=fake)

    assert result["success"] is True
    assert result["proxy_used"] == "uploads_playlist_enumeration"
    # oldest item on the uploads playlist is vidGood3 at 10 days
    assert result["first_video_published_at"].startswith(iso(10)[:10])
    assert result["days_since_channel_creation"] == 10
    assert result["pages_scanned"] == 1
    assert result["quota_cost"] == 2  # 1 channels.list + 1 playlistItems page


def test_first_published_date_uses_proxy_for_old_channel(fake: FakeClient, ids):
    result = get_first_published_date(ids["old"], client=fake)

    assert result["proxy_used"] == "channel_creation"
    assert result["first_video_published_at"] is None
    assert result["quota_cost"] == 1  # only channels.list
    assert fake.calls["playlistItems"] == 0


def test_first_published_date_enumerates_small_old_channel(fake: FakeClient):
    """Old channel, few uploads, recent start: creation date would lie, so enumerate."""
    cid = channel_id("Y")
    fake.add_channel(
        cid,
        "Dormant Then Active",
        900,
        [("vidDormant1", 6, 300_000, "PT14M00S"), ("vidDormant2", 9, 20_000, "PT13M00S")],
    )

    proxied = get_first_published_date(cid, client=fake)
    assert proxied["proxy_used"] == "channel_creation"
    assert proxied["first_video_published_at"] is None
    assert proxied["quota_cost"] == 1

    enumerated = get_first_published_date(
        cid, enumerate_if_video_count_under=100, client=fake
    )
    assert enumerated["proxy_used"] == "uploads_playlist_enumeration"
    assert enumerated["first_video_published_at"].startswith(iso(9)[:10])
    assert enumerated["quota_cost"] == 2


def test_first_published_date_respects_page_cap(fake: FakeClient, ids):
    playlist = "UU" + ids["good"][2:]
    fake.playlists[playlist] = [
        [{"snippet": {"publishedAt": iso(5), "resourceId": {"videoId": "a"}}}],
        [{"snippet": {"publishedAt": iso(45), "resourceId": {"videoId": "b"}}}],
    ]

    result = get_first_published_date(ids["good"], max_pages=1, client=fake)

    assert result["enumeration_truncated"] is True
    assert result["pages_scanned"] == 1
    assert result["quota_cost"] == 2


def test_max_views_filters_by_since_date(fake: FakeClient, ids):
    result = max_views_in_channel(ids["good"], since_date=iso(30), client=fake)

    assert result["success"] is True
    assert result["max_views"] == 250_000
    assert result["max_views_video_id"] == "vidGood1"
    assert result["videos_checked"] == 3  # every upload is inside the 30-day window
    assert result["quota_cost"] == 3  # channel + 1 page + 1 videos batch


def test_max_views_rejects_bad_since_date(fake: FakeClient, ids):
    result = max_views_in_channel(ids["good"], since_date="not-a-date", client=fake)

    assert result["success"] is False
    assert "ISO 8601" in result["error"]


def test_max_views_returns_zero_when_window_is_empty(fake: FakeClient, ids):
    result = max_views_in_channel(ids["good"], since_date=iso(0), client=fake)

    assert result["success"] is True
    assert result["max_views"] == 0
    assert result["max_views_video_id"] is None
    assert result["videos_checked"] == 0


def test_avg_duration_uses_uu_fast_path_and_means(fake: FakeClient, ids):
    result = avg_duration(ids["good"], sample_size=3, client=fake)

    assert result["success"] is True
    assert result["avg_duration_minutes"] == 11.0  # (12 + 10 + 11) / 3
    assert result["min_minutes"] == 10.0
    assert result["max_minutes"] == 12.0
    assert result["parsed_iso"] == ["PT12M00S", "PT10M00S", "PT11M00S"]
    assert result["quota_cost"] == 2  # playlist page + videos batch, no channels.list
    assert fake.calls["channels"] == 0


def test_avg_duration_falls_back_to_channels_lookup_for_handle(fake: FakeClient, ids):
    result = avg_duration("@automationlab", sample_size=2, client=fake)

    # handle is not a UC id, so the client must resolve it first
    assert result["success"] is True
    assert fake.calls["channels"] >= 1


def test_avg_duration_ignores_unparsable_durations(fake: FakeClient):
    cid = channel_id("Z")
    fake.add_channel(cid, "Lives Only", 40, [("live1", 2, 100, "P0D")])

    result = avg_duration(cid, sample_size=1, client=fake)

    assert result["success"] is False
    assert "No parsable durations" in result["error"]


def test_first_published_date_always_enumerate_never_uses_creation_proxy(fake: FakeClient, ids):
    """``always_enumerate`` is what the age filters call: no creation-date proxy."""
    default = get_first_published_date(ids["old"], client=fake)
    forced = get_first_published_date(ids["old"], always_enumerate=True, max_pages=3, client=fake)

    # the default path still allows the proxy for a big/old channel...
    assert default["proxy_used"] == "channel_creation"
    assert default["first_video_published_at"] is None
    # ...but the forced path reads the uploads playlist and reports the real first upload
    assert forced["proxy_used"] == "uploads_playlist_enumeration"
    assert forced["first_video_published_at"].startswith(iso(900)[:10])
    assert forced["enumeration_truncated"] is False
    assert forced["quota_cost"] == 2  # channels.list + one playlistItems page


def test_first_published_date_always_enumerate_reports_unavailable_when_empty(fake: FakeClient):
    """No uploads to read means no proof — the field stays None, never the account date."""
    cid = channel_id("Z")
    fake.add_channel(cid, "Empty Shell", created_days_ago=1200, videos=[])

    result = get_first_published_date(cid, always_enumerate=True, max_pages=3, client=fake)

    assert result["success"] is True
    assert result["first_video_published_at"] is None
    assert result["proxy_used"] == "unavailable"
    assert result["enumeration_truncated"] is False

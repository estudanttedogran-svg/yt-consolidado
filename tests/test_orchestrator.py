"""Tests for the orchestrator: staging, hard filters, ranking, shortfall honesty."""

from __future__ import annotations

from consolidado.tools.orchestrator import find_emerging_channels
from conftest import FakeClient, channel_id, iso

BASE = dict(query="ai automation", n=3, max_channel_age_days=30, min_video_views=100_000)


def test_dry_run_stops_after_discovery(fake: FakeClient):
    result = find_emerging_channels(**BASE, dry_run=True, client=fake)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["stage"] == "discovery"
    assert result["unique_channels_found"] == 4
    assert len(result["candidates"]) == 4
    assert result["quota_total"] == 100  # one search page
    assert fake.calls["channels"] == 0  # enrichment never ran


def test_full_run_keeps_only_channels_that_pass_every_filter(fake: FakeClient, ids):
    result = find_emerging_channels(**BASE, client=fake)

    assert result["success"] is True
    assert result["n_returned"] == 1
    assert len(result["channels"]) == 1

    channel = result["channels"][0]
    assert channel["channel_id"] == ids["good"]
    assert channel["country"] == "US"
    assert channel["subscriber_count"] == 18_400
    assert channel["max_views"] == 250_000
    assert channel["max_views_video"]["video_id"] == "vidGood1"
    assert channel["max_views_video"]["url"].endswith("vidGood1")
    assert channel["avg_duration_minutes"] == 11.0
    assert channel["videos_checked"] == 3
    assert channel["first_video_published_at"] is not None
    assert channel["quota_spent"] >= 3
    assert channel["score"] > 0


def test_result_is_never_padded(fake: FakeClient):
    result = find_emerging_channels(**BASE, client=fake)

    assert result["n_requested"] == 3
    assert result["n_returned"] == 1
    assert "not relaxed automatically" in result["shortfalls"]
    assert "2 rejected" in result["shortfalls"] or "rejected" in result["shortfalls"]
    assert "4 unique channels" in result["shortfalls"]


def test_zero_survivors_still_returns_a_readable_report(fake: FakeClient):
    result = find_emerging_channels(
        **{**BASE, "min_video_views": 10_000_000}, client=fake
    )

    assert result["success"] is True
    assert result["n_returned"] == 0
    assert result["channels"] == []
    assert "no video above" in result["shortfalls"]


def test_quota_breakdown_matches_total(fake: FakeClient):
    result = find_emerging_channels(**BASE, client=fake)
    breakdown = result["quota_breakdown"]

    assert sum(breakdown.values()) == result["quota_total"]
    assert breakdown["discovery"] == 100
    assert breakdown["enrichment"] == 1
    assert result["quota_total"] > 100


def test_ranking_orders_by_score_descending(fake: FakeClient):
    better = channel_id("E")
    fake.add_channel(
        better,
        "Faster Automation",
        created_days_ago=5,
        videos=[("vidBest1", 4, 400_000, "PT15M00S")],
        subscribers=30_000,
        custom_url="@fasterautomation",
    )
    fake.add_search_hit(better, "Faster Automation", "vidBest1", days_ago=2)

    result = find_emerging_channels(**{**BASE, "n": 5}, client=fake)

    assert result["n_returned"] == 2
    scores = [channel["score"] for channel in result["channels"]]
    assert scores == sorted(scores, reverse=True)
    assert result["channels"][0]["channel_id"] == better


def test_discovery_pages_multiplies_quota(fake: FakeClient):
    fake.search_pages = 2

    result = find_emerging_channels(**BASE, discovery_pages=2, dry_run=True, client=fake)

    assert result["quota_total"] == 200
    assert result["videos_scanned"] == 100


def test_candidate_cap_is_reported(fake: FakeClient):
    result = find_emerging_channels(
        **{**BASE, "n": 10, "max_candidates_checked": 1}, client=fake
    )

    assert result["n_returned"] <= 1
    assert "max_candidates_checked=1" in result["shortfalls"]


def test_old_channel_with_recent_first_upload_is_accepted(fake: FakeClient):
    """The case the fix exists for: created 900 days ago, first upload 9 days ago."""
    dormant = channel_id("Y")
    fake.add_channel(
        dormant,
        "Dormant Then Active",
        900,
        [("vidDormant1", 6, 300_000, "PT14M00S"), ("vidDormant2", 9, 20_000, "PT13M00S")],
        subscribers=9_000,
        custom_url="@dormantactive",
    )
    fake.add_search_hit(dormant, "Dormant Then Active", "vidDormant1", days_ago=2)

    result = find_emerging_channels(**{**BASE, "n": 5}, client=fake)

    assert result["n_returned"] == 2
    assert dormant in [channel["channel_id"] for channel in result["channels"]]
    accepted = next(c for c in result["channels"] if c["channel_id"] == dormant)
    assert accepted["first_video_published_at"].startswith(iso(9)[:10])
    assert accepted["days_since_first_publication"] <= 10


def test_near_misses_explain_the_gap(fake: FakeClient):
    result = find_emerging_channels(**BASE, client=fake)

    assert result["near_misses"]
    assert all("reason" in miss for miss in result["near_misses"])
    views = [miss["max_views"] for miss in result["near_misses"]]
    assert views == sorted(views, reverse=True)
    assert result["rejected_counts"]["low_views"] >= 1


def test_since_cutoff_is_rfc3339_in_filters(fake: FakeClient):
    result = find_emerging_channels(**BASE, client=fake)

    assert result["filters_applied"]["published_after"].endswith("Z")
    assert result["filters_applied"]["published_after"][:4] == iso(30)[:4]


def test_verification_stops_once_the_goal_is_met():
    """A batch boundary is the checkpoint: the second batch is never read."""

    class Tracking(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.touched: list[str] = []

        def playlist_items(self, tool, playlist_id=None, **kwargs):
            if playlist_id:
                self.touched.append(playlist_id)
            return super().playlist_items(tool, playlist_id, **kwargs)

    client = Tracking()

    good = channel_id("A")
    client.add_channel(
        good,
        "Automation Lab",
        created_days_ago=10,
        videos=[("vidGood1", 5, 250_000, "PT12M00S")],
        subscribers=18_400,
    )
    client.add_search_hit(good, "Automation Lab", "vidGood1")

    late: list[str] = []
    for seed in "BCDEFGHIJKL":  # 11 canais velhos: caem no filtro de idade
        cid = channel_id(seed)
        client.add_channel(
            cid,
            f"Legacy {seed}",
            created_days_ago=900,
            videos=[(f"vid{seed}", 800, 10, "PT10M00S")],
            subscribers=1_000,
        )
        client.add_search_hit(cid, f"Legacy {seed}", f"vid{seed}")
        late.append(cid)

    result = find_emerging_channels(
        query="ai automation",
        n=1,
        max_channel_age_days=30,
        min_video_views=100_000,
        client=client,
    )

    assert result["n_returned"] == 1
    assert result["skipped_after_goal"] == 2  # 12 candidatos, lote de 10
    assert result["shortfalls"] is None  # meta batida: sem shortfall fabricado

    uploads = {"UU" + cid[2:] for cid in [good, *late]}
    examined = {playlist for playlist in client.touched if playlist in uploads}
    assert len(examined) == 10  # só o primeiro lote
    assert "UU" + late[-1][2:] not in examined
    assert "UU" + late[-2][2:] not in examined


def test_age_gate_uses_first_video_not_account_for_a_big_old_account(fake: FakeClient):
    """The bug this fix removes: an account opened long ago, uploading now.

    With the creation-date proxy this channel is 1200 days old and gets rejected;
    with the first-video basis its oldest upload is 5 days old and it is kept.
    videoCount is above ``small_channel_video_cap``, so the old code never even
    looked at the uploads playlist.
    """
    active = channel_id("Y")
    fake.add_channel(
        active,
        "Old Account New Uploads",
        1200,
        [(f"vidAct{i}", 5, 500_000 if i == 0 else 1_000, "PT10M00S") for i in range(120)],
        subscribers=12_000,
        custom_url="@oldaccountnew",
    )
    fake.add_search_hit(active, "Old Account New Uploads", "vidAct0", days_ago=1)

    result = find_emerging_channels(**{**BASE, "n": 5}, client=fake)

    assert active in [channel["channel_id"] for channel in result["channels"]]
    kept = next(c for c in result["channels"] if c["channel_id"] == active)
    assert kept["first_video_published_at"].startswith(iso(5)[:10])
    # only the fixture's genuinely-old channel is rejected on age
    assert result["rejected_counts"]["not_new"] == 1


def test_channel_without_provable_first_video_is_rejected_not_guessed(fake: FakeClient):
    """No uploads to walk means no proof: ``age_unproven``, never the creation date."""
    empty = channel_id("Y")
    fake.add_channel(empty, "Empty Uploads", created_days_ago=10, videos=[], subscribers=5_000)
    fake.add_search_hit(empty, "Empty Uploads", "vidNone", days_ago=1)

    result = find_emerging_channels(**BASE, client=fake)

    assert empty not in [channel["channel_id"] for channel in result["channels"]]
    assert result["rejected_counts"]["age_unproven"] == 1

"""The product layer: ``find_emerging_channels``.

Five stages, each one metered:
  1. Discovery          — search.list, 100 units per page of 50 videos.
  2. Channel enrichment — channels.list, 1 unit per batch of 50.
  3. Hard verification  — 3–5 units per surviving channel.
  4. Ranking            — local arithmetic, 0 units.
  5. Output             — local, 0 units.

Nothing is relaxed automatically. If fewer than ``n`` channels survive, the
shortfall is explained and returned as-is: loosening filters is the operator's
call, not the server's.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..auth import NoApiKeyError, QuotaExhaustedError, QuotaTracker
from ..youtube_client import YouTubeApiError, YouTubeClient, get_client
from .custom import (
    _max_views_rich,
    _parse_timestamp,
    avg_duration,
    get_first_published_date,
)


#: Channels verified per batch. Between batches the loop checks whether the
#: requested count was already reached and stops, so a slow run cannot blow the
#: MCP client's timeout.
VERIFY_BATCH_SIZE = 10


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(days, 1))


def _rfc3339(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _discover(
    client: YouTubeClient,
    query: str,
    cutoff: datetime,
    region_code: str,
    relevance_language: str,
    pages: int,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Stage 1 — harvest unique channels from recent uploads."""
    found: dict[str, dict[str, Any]] = {}
    token: str | None = None

    for _ in range(max(1, pages)):
        response = client.search(
            "find_emerging_channels",
            query=query,
            max_results=50,
            search_type="video",
            order="date",
            published_after=_rfc3339(cutoff),
            region_code=region_code,
            relevance_language=relevance_language,
            page_token=token,
        )
        for item in response.get("items", []):
            snippet = item.get("snippet") or {}
            channel_id = snippet.get("channelId")
            if not channel_id or channel_id in found:
                continue
            found[channel_id] = {
                "channel_title": snippet.get("channelTitle", ""),
                "first_seen_video_id": (item.get("id") or {}).get("videoId"),
                "first_seen_video_title": snippet.get("title", ""),
                "first_seen_published_at": snippet.get("publishedAt", ""),
            }
        token = response.get("nextPageToken")
        if not token:
            break

    return found, len(found)


def _enrich(
    client: YouTubeClient,
    channel_ids: list[str],
    region_code: str,
    subscriber_min: int,
    subscriber_max: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Stage 2 — one channels.list per 50 ids, cheap filters only."""
    eliminated = {"country": 0, "subscribers": 0}
    survivors: list[dict[str, Any]] = []

    for start in range(0, len(channel_ids), 50):
        batch = channel_ids[start : start + 50]
        response = client.channels(
            "find_emerging_channels", channel_ids=batch, parts="snippet,statistics,contentDetails"
        )
        for item in response.get("items", []):
            snippet = item.get("snippet") or {}
            statistics = item.get("statistics") or {}
            country = snippet.get("country")
            if country and country != region_code:
                eliminated["country"] += 1
                continue
            subscribers = int(statistics.get("subscriberCount") or 0)
            if statistics.get("hiddenSubscriberCount") is True:
                subscribers = 0
            if subscribers < subscriber_min or (subscriber_max and subscribers > subscriber_max):
                eliminated["subscribers"] += 1
                continue
            survivors.append(
                {
                    "channel_id": item.get("id"),
                    "title": snippet.get("title", ""),
                    "custom_url": snippet.get("customUrl"),
                    "country": country,
                    "created_at": snippet.get("publishedAt"),
                    "subscriber_count": subscribers,
                    "video_count": int(statistics.get("videoCount") or 0),
                    "view_count": int(statistics.get("viewCount") or 0),
                    "uploads_playlist_id": (
                        ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
                    ),
                }
            )
    return survivors, eliminated


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
    first_video_pages: int = 3,
    client: YouTubeClient | None = None,
    tracker: QuotaTracker | None = None,
) -> dict[str, Any]:
    """Find channels whose first upload is recent and whose videos already pop.

    Layer: Data API only (needs a key). Quota: 100 units per discovery page
    (``discovery_pages``, default 1) + 1 per 50 channels enriched + 1–3 per
    channel verified (the first-upload walk) plus 1 per 50 uploads the view scan
    touches. A default run costs ~150–500 units depending on how many channels
    survive stage 2.

    The age gate measures the FIRST VIDEO PUBLISHED DATE, never the account
    creation date: an account opened years ago that started posting last week is
    exactly what this tool hunts. ``first_video_pages`` caps how deep the walk
    goes to prove the first upload. A channel whose first video cannot be proven
    within that budget is rejected and reported, never guessed at.

    ``small_channel_video_cap`` is kept for callers written before the age gate
    was rewritten; the gate no longer depends on it.

    ``dry_run=True`` stops after stage 1 and returns the raw candidate list so
    the operator can inspect before spending enrichment quota.

    Filters are never relaxed automatically: a short result is reported, not
    padded.
    """
    client = client or get_client()
    tracker = tracker or client.tracker
    started = time.monotonic()
    before = tracker.total()
    cutoff = _cutoff(max_channel_age_days)

    filters = {
        "query": query,
        "n": n,
        "max_channel_age_days": max_channel_age_days,
        "published_after": _rfc3339(cutoff),
        "min_video_views": min_video_views,
        "region_code": region_code,
        "relevance_language": relevance_language,
        "min_avg_duration_min": min_avg_duration_min,
        "max_avg_duration_min": max_avg_duration_min,
        "subscriber_min": subscriber_min,
        "subscriber_max": subscriber_max,
        "discovery_pages": discovery_pages,
        "max_candidates_checked": max_candidates_checked,
        "sample_size": sample_size,
        "small_channel_video_cap": small_channel_video_cap,
        "age_basis": "first_video_published_at",
        "first_video_pages": first_video_pages,
        "dry_run": dry_run,
    }

    # ------------------------------------------------------------------ stage 1
    try:
        discovered, unique_channels = _discover(
            client, query, cutoff, region_code, relevance_language, discovery_pages
        )
    except (NoApiKeyError, QuotaExhaustedError, YouTubeApiError) as error:
        return {
            "success": False,
            "query": query,
            "filters_applied": filters,
            "error": str(error),
            "quota_total": tracker.total() - before,
        }

    quota_discovery = tracker.total() - before

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "query": query,
            "filters_applied": filters,
            "stage": "discovery",
            "videos_scanned": 50 * discovery_pages,
            "unique_channels_found": unique_channels,
            "candidates": [
                {"channel_id": channel_id, **meta}
                for channel_id, meta in list(discovered.items())[:100]
            ],
            "quota_total": quota_discovery,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "near_misses": [],
            "next_step": (
                "Re-run with dry_run=false to enrich and verify. Each candidate costs "
                "3–5 units; stage 2 costs 1 unit per 50 channels."
            ),
        }

    # ------------------------------------------------------------------ stage 2
    try:
        survivors, eliminated = _enrich(
            client, list(discovered.keys()), region_code, subscriber_min, subscriber_max
        )
    except (NoApiKeyError, QuotaExhaustedError, YouTubeApiError) as error:
        return {
            "success": False,
            "query": query,
            "filters_applied": filters,
            "error": str(error),
            "quota_total": tracker.total() - before,
        }

    candidates = survivors[: max_candidates_checked]
    dropped_by_cap = max(0, len(survivors) - len(candidates))

    # ------------------------------------------------------------------ stage 3
    verified: list[dict[str, Any]] = []
    near_misses: list[dict[str, Any]] = []
    rejected = {
        "not_new": 0,
        "low_views": 0,
        "duration": 0,
        "verify_error": 0,
        "age_unproven": 0,
    }
    skipped_after_goal = 0
    goal = max(1, n)

    for start in range(0, len(candidates), VERIFY_BATCH_SIZE):
        batch = candidates[start : start + VERIFY_BATCH_SIZE]

        for channel in batch:
            channel_id = channel["channel_id"]
            per_channel_before = tracker.total()
            try:
                first = get_first_published_date(
                    channel_id,
                    always_enumerate=True,
                    max_pages=first_video_pages,
                    client=client,
                    tracker=tracker,
                )
                if not first.get("success"):
                    rejected["verify_error"] += 1
                    continue

                # The age gate reads the first VIDEO, never the account:
                # ``channel_created_at`` is not a proxy for it.
                anchor = first.get("first_video_published_at")
                anchor_dt = _parse_timestamp(anchor)
                if anchor_dt is None or first.get("enumeration_truncated"):
                    rejected["age_unproven"] += 1
                    continue
                if anchor_dt < cutoff:
                    rejected["not_new"] += 1
                    continue

                views = _max_views_rich(channel_id, cutoff, client=client)
                average_minutes: float | None = None

                if views["max_views"] < min_video_views:
                    rejected["low_views"] += 1
                    near_misses.append(
                        {
                            "channel_id": channel_id,
                            "channel_title": channel["title"],
                            "first_video_published_at": anchor,
                            "max_views": views["max_views"],
                            "avg_duration_minutes": None,
                            "reason": f"max_views below {min_video_views}",
                        }
                    )
                    continue

                duration_info = avg_duration(
                    channel_id, sample_size=sample_size, client=client, tracker=tracker
                )
                if not duration_info.get("success"):
                    rejected["duration"] += 1
                    near_misses.append(
                        {
                            "channel_id": channel_id,
                            "channel_title": channel["title"],
                            "first_video_published_at": anchor,
                            "max_views": views["max_views"],
                            "avg_duration_minutes": None,
                            "reason": "no parsable durations",
                        }
                    )
                    continue
                average_minutes = duration_info["avg_duration_minutes"]
                if average_minutes < min_avg_duration_min or average_minutes > max_avg_duration_min:
                    rejected["duration"] += 1
                    near_misses.append(
                        {
                            "channel_id": channel_id,
                            "channel_title": channel["title"],
                            "first_video_published_at": anchor,
                            "max_views": views["max_views"],
                            "avg_duration_minutes": average_minutes,
                            "reason": (
                                f"avg duration outside {min_avg_duration_min}–{max_avg_duration_min} min"
                            ),
                        }
                    )
                    continue
            except (NoApiKeyError, QuotaExhaustedError, YouTubeApiError):
                rejected["verify_error"] += 1
                continue

            days_since_creation = first.get("days_since_channel_creation")
            days_since_first = max((datetime.now(timezone.utc) - anchor_dt).days, 1)
            age_for_score = days_since_first or days_since_creation or 1
            score = (views["max_views"] / 1000) * (1 / max(age_for_score, 1)) * (average_minutes / 15)

            verified.append(
                {
                    "channel_id": channel_id,
                    "channel_title": channel["title"],
                    "channel_url": (
                        f"https://youtube.com/{channel['custom_url']}"
                        if channel.get("custom_url")
                        else f"https://youtube.com/channel/{channel_id}"
                    ),
                    "channel_created_at": first.get("channel_created_at"),
                    "days_since_creation": days_since_creation,
                    "days_since_first_publication": days_since_first,
                    "country": channel.get("country"),
                    "subscriber_count": channel["subscriber_count"],
                    "first_video_published_at": first.get("first_video_published_at"),
                    "max_views": views["max_views"],
                    "max_views_video": {
                        "video_id": views["video_id"],
                        "title": views["title"],
                        "url": (
                            f"https://youtube.com/watch?v={views['video_id']}"
                            if views["video_id"]
                            else None
                        ),
                    },
                    "avg_duration_minutes": average_minutes,
                    "videos_checked": views["videos_checked"],
                    "score": round(score, 1),
                    "quota_spent": tracker.total() - per_channel_before,
                }
            )

        if len(verified) >= goal:
            skipped_after_goal = len(candidates) - (start + len(batch))
            break

    # ------------------------------------------------------------------ stage 4
    verified.sort(key=lambda entry: entry["score"], reverse=True)
    selected = verified[: max(1, n)]
    near_misses.sort(key=lambda entry: entry["max_views"], reverse=True)

    # ------------------------------------------------------------------ stage 5
    shortfall: str | None = None
    if len(selected) < n:
        reasons = [
            f"{unique_channels} unique channels found in discovery",
            f"{len(survivors)} survived country/subscriber filters",
            f"{rejected['not_new']} rejected: first publication older than {max_channel_age_days} days",
            f"{rejected['low_views']} rejected: no video above {min_video_views} views",
            f"{rejected['duration']} rejected: average duration outside {min_avg_duration_min}–{max_avg_duration_min} min",
        ]
        if rejected["age_unproven"]:
            reasons.append(
                f"{rejected['age_unproven']} rejected: first video not proven within "
                f"{first_video_pages} upload pages"
            )
        if rejected["verify_error"]:
            reasons.append(f"{rejected['verify_error']} could not be verified (API error)")
        if dropped_by_cap:
            reasons.append(f"{dropped_by_cap} candidates skipped by max_candidates_checked={max_candidates_checked}")
        if skipped_after_goal:
            reasons.append(
                f"{skipped_after_goal} candidates left unverified: the {n}-channel "
                f"goal was met, batches of {VERIFY_BATCH_SIZE}"
            )
        shortfall = (
            f"Returned {len(selected)} of {n} requested. " + "; ".join(reasons) + ". "
            "Filters were not relaxed automatically — widen them deliberately if you want more."
        )

    return {
        "success": True,
        "query": query,
        "filters_applied": filters,
        "n_requested": n,
        "n_returned": len(selected),
        "channels": selected,
        "near_misses": near_misses[:10],
        "rejected_counts": rejected,
        "skipped_after_goal": skipped_after_goal,
        "quota_total": tracker.total() - before,
        "quota_breakdown": {
            "discovery": quota_discovery,
            "enrichment": 1 * ((len(discovered) + 49) // 50),
            "verification": (tracker.total() - before) - quota_discovery - ((len(discovered) + 49) // 50),
        },
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "shortfalls": shortfall,
    }

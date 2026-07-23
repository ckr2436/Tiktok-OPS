from __future__ import annotations

from app.celery_app import WEBSITE_ADS_MEDIA_TASK_QUEUE, WEBSITE_ADS_TASK_QUEUE, beat_schedule, celery_app


WEBSITE_ADS_BEAT_ENTRIES = (
    "website_ads_monitor_cycle",
    "website_ads_daily_report_cycle",
    "website_ads_asset_library_cycle",
    "website_ads_targeting_catalog_sync",
    "website_ads_asset_media_cache_dispatch",
    "website_ads_asset_analysis_dispatch",
    "website_ads_asset_expansion_cycle",
)


def test_website_ads_control_plane_has_a_dedicated_queue() -> None:
    assert WEBSITE_ADS_TASK_QUEUE == "website_ads"
    assert WEBSITE_ADS_TASK_QUEUE != "gmvmax"
    assert celery_app.conf.task_routes["website_ads.*"]["queue"] == WEBSITE_ADS_TASK_QUEUE
    assert WEBSITE_ADS_TASK_QUEUE in {queue.name for queue in celery_app.conf.task_queues}


def test_website_ads_beat_tasks_never_publish_to_gmvmax() -> None:
    for name in WEBSITE_ADS_BEAT_ENTRIES:
        entry = beat_schedule[name]
        assert entry["task"].startswith("website_ads.")
        assert entry["options"]["queue"] == WEBSITE_ADS_TASK_QUEUE

    # Media I/O is intentionally isolated further; this is not a GMV Max
    # queue either and must remain ahead of the wildcard Website Ads route.
    assert celery_app.conf.task_routes["website_ads.upload_video"]["queue"] == WEBSITE_ADS_MEDIA_TASK_QUEUE
    assert WEBSITE_ADS_MEDIA_TASK_QUEUE != "gmvmax"


def test_gmvmax_creative_media_dispatch_remains_a_gmvmax_task() -> None:
    entry = beat_schedule["gmvmax_creative_asset_media_cache_dispatch"]
    assert entry["task"] == "gmvmax.creative_asset_media_cache_dispatch"
    assert entry["options"]["queue"] == "gmvmax"

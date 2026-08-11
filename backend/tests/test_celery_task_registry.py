from app.celery_app import (
    AI_VIDEO_API_TASK_QUEUE,
    AI_VIDEO_BROWSER_TASK_QUEUE,
    AI_VIDEO_BROWSER_POLL_TASK_QUEUE,
    AI_VIDEO_DOWNLOAD_TASK_QUEUE,
    AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    HERMES_AGENT_TASK_QUEUE,
    HERMES_MAINTENANCE_TASK_QUEUE,
    WHISPER_TASK_QUEUE,
    task_modules_for_runtime,
    task_modules_for_worker_queue,
)


def test_hermes_worker_does_not_load_unrelated_task_graphs():
    modules = task_modules_for_worker_queue(HERMES_AGENT_TASK_QUEUE)

    assert modules == (
        "app.tasks.hermes_agent.tasks",
        "app.tasks.hermes_agent.content_runtime_tasks",
        "app.tasks.hermes_agent.content_factory_tasks",
    )
    assert "app.tasks.website_ads_tasks" not in modules
    assert "app.gmvmax.tasks_sync" not in modules
    assert "app.features.tenants.openai_whisper.tasks" not in modules


def test_ai_video_production_workers_only_load_provider_tasks():
    expected = (
        "app.tasks.ai_video.result_download_tasks",
        "app.tasks.ai_video.video_tasks",
        "app.tasks.globalaiopc.video_tasks",
    )
    assert task_modules_for_worker_queue(AI_VIDEO_API_TASK_QUEUE) == expected
    assert task_modules_for_worker_queue(AI_VIDEO_BROWSER_TASK_QUEUE) == expected
    assert task_modules_for_worker_queue(AI_VIDEO_BROWSER_POLL_TASK_QUEUE) == expected


def test_ai_video_download_and_maintenance_registries_are_isolated():
    assert task_modules_for_worker_queue(AI_VIDEO_DOWNLOAD_TASK_QUEUE) == (
        "app.tasks.ai_video.result_download_tasks",
    )
    assert task_modules_for_worker_queue(AI_VIDEO_MAINTENANCE_TASK_QUEUE) == (
        "app.tasks.ai_video.result_download_tasks",
        "app.tasks.ai_video.video_tasks",
        "app.tasks.jimeng_lab_tasks",
        "app.tasks.doubao_lab_tasks",
    )


def test_hermes_browser_maintenance_does_not_load_content_factory():
    assert task_modules_for_worker_queue(HERMES_MAINTENANCE_TASK_QUEUE) == (
        "app.tasks.hermes_agent.tasks",
    )


def test_whisper_worker_only_loads_whisper_tasks():
    assert task_modules_for_worker_queue(WHISPER_TASK_QUEUE) == (
        "app.features.tenants.openai_whisper.tasks",
        "app.tasks.tiktok_shop_video_transcript_tasks",
    )


def test_unspecialized_process_keeps_complete_registry():
    modules = task_modules_for_worker_queue(None)

    assert "app.tasks.website_ads_tasks" in modules
    assert "app.gmvmax.tasks_sync" in modules
    assert "app.tasks.hermes_agent.content_factory_tasks" in modules
    assert "app.features.tenants.openai_whisper.tasks" in modules


def test_api_producer_and_beat_do_not_import_consumer_task_graphs():
    assert task_modules_for_runtime(None, "producer") == ()
    assert task_modules_for_runtime(None, "beat") == ()
    assert task_modules_for_runtime(HERMES_AGENT_TASK_QUEUE, "worker") == (
        "app.tasks.hermes_agent.tasks",
        "app.tasks.hermes_agent.content_runtime_tasks",
        "app.tasks.hermes_agent.content_factory_tasks",
    )


def test_whisper_worker_registers_content_producer_reference_analysis():
    from app.features.tenants.openai_whisper.tasks import (
        analyze_content_producer_reference,
    )

    assert (
        analyze_content_producer_reference.name
        == "openai_whisper.analyze_content_producer_reference"
    )
    assert analyze_content_producer_reference.queue == WHISPER_TASK_QUEUE

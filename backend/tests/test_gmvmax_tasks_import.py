import os
from pathlib import Path
import subprocess
import sys


def test_import_gmvmax_tasks():
    from app.tasks import ttb_gmvmax_tasks as mod

    assert hasattr(mod, "task_gmvmax_sync_campaigns")
    assert not hasattr(mod, "task_gmvmax_sync_metrics")
    assert hasattr(mod, "task_gmvmax_sync_creative_metrics_10min")
    assert hasattr(mod, "task_gmvmax_sync_creative_metrics_10min_for_campaign")
    assert hasattr(mod, "reconcile_create_intents_task")
    assert not hasattr(mod, "task_gmvmax_apply_action")
    assert not hasattr(mod, "task_gmvmax_evaluate_strategy")

    assert mod.task_gmvmax_sync_campaigns.name == "gmvmax.sync_campaigns"
    assert mod.task_gmvmax_sync_creative_metrics_10min.name == "gmvmax.sync_creative_metrics_10min"
    assert (
        mod.task_gmvmax_sync_creative_metrics_10min_for_campaign.name
        == "gmvmax.sync_creative_metrics_10min_for_campaign"
    )
    assert (
        mod.reconcile_create_intents_task.name
        == "gmvmax.reconcile_create_intents"
    )


def test_active_catalog_attempt_state_is_fully_scoped():
    from app.tasks import ttb_gmvmax_tasks as mod

    class _Scalars:
        @staticmethod
        def all():
            return []

    class _Db:
        statement = ""

        class _Bind:
            class _Dialect:
                name = "sqlite"

            dialect = _Dialect()

        def get_bind(self):
            return self._Bind()

        def scalars(self, statement):
            self.statement = str(statement)
            return _Scalars()

    db = _Db()
    assert (
        mod._iter_active_catalog_campaign_scopes(
            db,
            workspace_id=7,
            auth_id=11,
            advertiser_id="adv-1",
        )
        == []
    )
    normalized = " ".join(db.statement.split())
    for scope_clause in (
        "gmv_creative_10min_sync_state.workspace_id = gmvmax_product_campaign_catalog.workspace_id",
        "gmv_creative_10min_sync_state.auth_id = gmvmax_product_campaign_catalog.auth_id",
        "gmv_creative_10min_sync_state.advertiser_id = gmvmax_product_campaign_catalog.advertiser_id",
        "gmv_creative_10min_sync_state.campaign_id = gmvmax_product_campaign_catalog.campaign_id",
    ):
        assert scope_clause in normalized


def test_website_ads_media_route_precedes_general_ads_route():
    from app.celery_app import (
        WEBSITE_ADS_MEDIA_TASK_QUEUE,
        WEBSITE_ADS_TASK_QUEUE,
        celery_app,
    )

    router = celery_app.amqp.Router()
    upload_queue = router.route({}, "website_ads.upload_video")["queue"]
    monitor_queue = router.route({}, "website_ads.monitor_cycle")["queue"]

    assert upload_queue.name == WEBSITE_ADS_MEDIA_TASK_QUEUE
    assert monitor_queue.name == WEBSITE_ADS_TASK_QUEUE
    assert monitor_queue.name != "gmvmax"


def test_service_first_import_does_not_reenter_celery_task_registry():
    backend_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import app.services.gmvmax_creative_guard; "
                "import app.services.gmvmax_smart_guard; "
                "import app.features.tenants.ttb.gmv_max.control; "
                "import app.celery_app; "
                "import app.app"
            ),
        ],
        cwd=backend_root,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr

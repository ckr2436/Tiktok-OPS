def test_import_gmvmax_tasks():
    from app.tasks import ttb_gmvmax_tasks as mod

    assert hasattr(mod, "task_gmvmax_sync_campaigns")
    assert hasattr(mod, "task_gmvmax_sync_metrics")
    assert hasattr(mod, "task_gmvmax_apply_action")

    assert mod.task_gmvmax_sync_campaigns.name == "gmvmax.sync_campaigns"
    assert mod.task_gmvmax_sync_metrics.name == "gmvmax.sync_metrics"
    assert mod.task_gmvmax_apply_action.name == "gmvmax.apply_action"

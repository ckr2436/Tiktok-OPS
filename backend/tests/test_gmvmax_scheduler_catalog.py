from app.services import scheduler_catalog as sc


def _catalog_map():
    return {spec.name: spec for spec in sc.CATALOG}


def test_gmvmax_tasks_present():
    catalog = _catalog_map()
    assert "gmvmax:campaigns:sync" in catalog
    assert "gmvmax:metrics:sync_hourly" in catalog
    assert "gmvmax:metrics:sync_daily" in catalog
    assert "gmvmax:campaigns:apply_action" in catalog

    hourly = catalog["gmvmax:metrics:sync_hourly"]
    assert hourly.task == "gmvmax.sync_metrics"
    assert hourly.crontab == "*/30 * * * *"
    assert isinstance(hourly.kwargs, dict)
    assert hourly.kwargs.get("granularity") == "HOUR"

    daily = catalog["gmvmax:metrics:sync_daily"]
    assert daily.task == "gmvmax.sync_metrics"
    assert daily.crontab == "0 3 * * *"
    assert daily.kwargs.get("granularity") == "DAY"

    manual = catalog["gmvmax:campaigns:apply_action"]
    assert manual.task == "gmvmax.apply_action"
    assert manual.manual_only is True

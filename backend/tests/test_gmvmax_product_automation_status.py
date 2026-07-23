from app.features.tenants.ttb.router import meta


class _FakeRows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.statement = ""

    def execute(self, statement, params):
        self.statement = str(statement)
        return _FakeRows(self._rows)


def test_product_automation_status_prefers_catalog_over_stale_realtime(monkeypatch):
    monkeypatch.setattr(
        meta,
        "_advertiser_date_window_utc",
        lambda *args, **kwargs: (None, None),
    )
    db = _FakeSession(
        [
            {
                "product_id": "product-1",
                "latest_campaign_id": "campaign-1",
                "latest_campaign_name": "Campaign",
                "latest_strategy_enabled": 0,
                "latest_catalog_status": "DISABLE",
                "latest_realtime_status": "ENABLE",
                "latest_effective_status": "DISABLE",
                "active_campaign_count": 0,
            }
        ]
    )

    result = meta._load_product_automation_stats(
        db,
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        product_ids=["product-1"],
    )

    assert result["product-1"]["campaign_operation_status"] == "DISABLE"
    assert result["product-1"]["active_campaign_count"] == 0
    normalized_statement = " ".join(db.statement.split())
    assert "mc.effective_operation_status='ENABLE'" in normalized_statement
    assert "from gmv_product_metrics_daily" in normalized_statement
    assert "from gmv_product_metrics_hourly" in normalized_statement
    assert "from product_daily d where d.source_observed_at is not null" in normalized_statement
    assert (
        "d.metric_date=h.metric_date and d.source_observed_at is not null"
        in normalized_statement
    )
    assert ":advertiser_today" not in normalized_statement
    assert "from gmvmax_product_creative_metrics_daily" not in normalized_statement
    assert "r.latest_cost_cents" not in normalized_statement
    assert "coalesce(mc.strategy_enabled, 0) desc" not in normalized_statement
    assert result["product-1"]["metric_scope"] == "LATEST_CAMPAIGN_PRODUCT"

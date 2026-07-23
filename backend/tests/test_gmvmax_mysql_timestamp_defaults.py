from __future__ import annotations

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.data.models.gmvmax_campaign_metrics import (
    GmvmaxProductCampaignMetricsDaily,
)
from app.data.models.gmvmax_campaign_snapshots import (
    GmvmaxProductCampaignSnapshotBatch,
)


def test_mysql_timestamp_defaults_are_sql_expressions() -> None:
    for model in (
        GmvmaxProductCampaignMetricsDaily,
        GmvmaxProductCampaignSnapshotBatch,
    ):
        ddl = str(CreateTable(model.__table__).compile(dialect=mysql.dialect()))
        assert "DEFAULT CURRENT_TIMESTAMP(6)" in ddl
        assert "DEFAULT 'CURRENT_TIMESTAMP(6)'" not in ddl

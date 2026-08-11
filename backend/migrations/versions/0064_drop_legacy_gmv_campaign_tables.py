"""Drop legacy gmv campaign tables"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "0064_drop_legacy_gmv_campaign_tables"
down_revision = "0063_gmvmax_snapshot_indexes"
branch_labels = None
depends_on = None

PREFIXES = [
    "gmv_campaigns_legacy_",
    "gmv_campaign_products_legacy_",
    "gmv_campaign_metrics_daily_legacy_",
    "gmv_campaign_metrics_hourly_legacy_",
    "gmv_campaign_creatives_legacy_",
    "gmv_campaign_livestreams_legacy_",
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = set(inspector.get_table_names())

    for table in list(existing):
        for prefix in PREFIXES:
            if table.startswith(prefix):
                for referencing_table in existing:
                    for fk in inspector.get_foreign_keys(referencing_table):
                        if fk["referred_table"] == table:
                            op.drop_constraint(
                                fk["name"], referencing_table, type_="foreignkey"
                            )
                op.drop_table(table)
                break


def downgrade() -> None:
    # Legacy tables are intentionally not recreated.
    pass

"""ensure GMV Max unique constraints"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0020_ttb_gmvmax_uniques"
down_revision = "0019_ttb_gmvmax_core"
branch_labels = None
depends_on = None


def _ensure_unique(table: str, name: str, columns: list[str]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {uc.get("name") for uc in inspector.get_unique_constraints(table)}
    if name not in existing:
        op.create_unique_constraint(name, table, columns)


def _drop_unique(table: str, name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {uc.get("name") for uc in inspector.get_unique_constraints(table)}
    if name in existing:
        op.drop_constraint(name, table_name=table, type_="unique")


def upgrade() -> None:
    _ensure_unique(
        "ttb_gmvmax_campaigns",
        "uk_ttb_gmvmax_campaign_scope",
        ["workspace_id", "auth_id", "campaign_id"],
    )
    _ensure_unique(
        "ttb_gmvmax_metrics_hourly",
        "uk_ttb_gmvmax_metrics_hourly",
        ["campaign_id", "interval_start"],
    )
    _ensure_unique(
        "ttb_gmvmax_metrics_daily",
        "uk_ttb_gmvmax_metrics_daily",
        ["campaign_id", "date"],
    )


def downgrade() -> None:
    _drop_unique("ttb_gmvmax_metrics_daily", "uk_ttb_gmvmax_metrics_daily")
    _drop_unique("ttb_gmvmax_metrics_hourly", "uk_ttb_gmvmax_metrics_hourly")
    _drop_unique("ttb_gmvmax_campaigns", "uk_ttb_gmvmax_campaign_scope")

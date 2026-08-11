"""Add delivery status to 10-minute creative metrics.

Revision ID: 0092_add_creative_status_to_10min_metrics
Revises: 0091_oauth_provider_service_id_repair
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0092_add_creative_status_to_10min_metrics"
down_revision = "0091_oauth_provider_service_id_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("gmv_creative_metrics_10min")}
    if "creative_status" not in columns:
        op.add_column(
            "gmv_creative_metrics_10min",
            sa.Column("creative_status", sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("gmv_creative_metrics_10min")}
    if "creative_status" in columns:
        op.drop_column("gmv_creative_metrics_10min", "creative_status")

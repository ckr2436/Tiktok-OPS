"""Add unique scope constraint for GMV Max monitoring strategies

Revision ID: 0050_gmvmax_monitoring_unique_scope
Revises: 0048_gmvmax_monitoring_strategies
Create Date: 2025-05-31 00:00:00.000000
"""

from alembic import op

revision = "0050_gmvmax_monitoring_unique_scope"
down_revision = "0048_gmvmax_monitoring_strategies"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_gmvmax_strategy_scope",
        "gmvmax_monitoring_strategies",
        [
            "workspace_id",
            "promotion_type",
            "level",
            "auth_id",
            "advertiser_id",
            "store_id",
        ],
    )


def downgrade():
    op.drop_constraint(
        "uq_gmvmax_strategy_scope",
        "gmvmax_monitoring_strategies",
        type_="unique",
    )

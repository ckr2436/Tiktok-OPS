"""Add production indexes for GMV campaigns

Revision ID: 0051_gmv_campaign_indexes
Revises: 0050_gmvmax_monitoring_unique_scope
Create Date: 2025-06-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0051_gmv_campaign_indexes"
down_revision = "0050_gmvmax_monitoring_unique_scope"
branch_labels = None
depends_on = None


_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("idx_gmv_campaign_workspace_status", ["workspace_id", "promotion_type", "is_deleted", "status"]),
    ("idx_gmv_campaign_workspace_updated", ["workspace_id", "promotion_type", "ext_updated_time"]),
)


def _index_exists(bind, table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(bind)
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade():
    bind = op.get_bind()
    for name, columns in _INDEXES:
        if not _index_exists(bind, "gmv_campaigns", name):
            op.create_index(name, "gmv_campaigns", columns)


def downgrade():
    bind = op.get_bind()
    for name, _ in _INDEXES:
        if _index_exists(bind, "gmv_campaigns", name):
            op.drop_index(name, table_name="gmv_campaigns")

"""Add GMV Max product fields"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "0018_ttb_product_gmv_fields"
down_revision = "0017_kie_api_models"
branch_labels = None
depends_on = None


def _column_exists(inspector, table: str, column: str) -> bool:
    try:
        return any(col.get("name") == column for col in inspector.get_columns(table))
    except Exception:
        return False


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    if "ttb_products" not in inspector.get_table_names():
        return

    columns_to_add = [
        ("image_url", sa.String(length=1024)),
        ("min_price", sa.Numeric(18, 4)),
        ("max_price", sa.Numeric(18, 4)),
        ("historical_sales", sa.Integer()),
        ("category", sa.String(length=255)),
        ("gmv_max_ads_status", sa.String(length=32)),
        ("is_running_custom_shop_ads", sa.Boolean()),
    ]

    for name, column_type in columns_to_add:
        if _column_exists(inspector, "ttb_products", name):
            continue
        op.add_column("ttb_products", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    if "ttb_products" not in inspector.get_table_names():
        return

    columns = [
        "is_running_custom_shop_ads",
        "gmv_max_ads_status",
        "category",
        "historical_sales",
        "max_price",
        "min_price",
        "image_url",
    ]

    for name in columns:
        if not _column_exists(inspector, "ttb_products", name):
            continue
        op.drop_column("ttb_products", name)


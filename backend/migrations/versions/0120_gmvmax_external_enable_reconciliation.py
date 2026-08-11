"""add auditable external-enable confirmation to GMV Max manual pauses

Revision ID: 0120_gmvmax_external_enable
Revises: 0119_tiktok_shop_flash_sale_schedules
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0120_gmvmax_external_enable"
down_revision = "0119_tiktok_shop_flash_sale_schedules"
branch_labels = None
depends_on = None


TABLE_NAME = "gmvmax_campaign_manual_overrides"


def _columns() -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)
    }


def upgrade() -> None:
    columns = _columns()
    additions = (
        (
            "override_started_at",
            sa.Column("override_started_at", mysql.DATETIME(fsp=6), nullable=True),
        ),
        (
            "external_enable_first_observed_at",
            sa.Column(
                "external_enable_first_observed_at",
                mysql.DATETIME(fsp=6),
                nullable=True,
            ),
        ),
        (
            "external_enable_last_observed_at",
            sa.Column(
                "external_enable_last_observed_at",
                mysql.DATETIME(fsp=6),
                nullable=True,
            ),
        ),
        (
            "external_enable_observation_count",
            sa.Column(
                "external_enable_observation_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        ),
        (
            "resolved_at",
            sa.Column("resolved_at", mysql.DATETIME(fsp=6), nullable=True),
        ),
        (
            "resolution_type",
            sa.Column("resolution_type", sa.String(length=64), nullable=True),
        ),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column(TABLE_NAME, column)

    op.execute(
        sa.text(
            f"UPDATE {TABLE_NAME} "
            "SET override_started_at = COALESCE(override_started_at, updated_at) "
            "WHERE active = 1 AND override_started_at IS NULL"
        )
    )


def downgrade() -> None:
    columns = _columns()
    for name in (
        "resolution_type",
        "resolved_at",
        "external_enable_observation_count",
        "external_enable_last_observed_at",
        "external_enable_first_observed_at",
        "override_started_at",
    ):
        if name in columns:
            op.drop_column(TABLE_NAME, name)

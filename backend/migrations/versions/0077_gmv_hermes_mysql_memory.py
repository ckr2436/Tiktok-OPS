"""add structured MySQL memory for GMV Hermes policy learning

Revision ID: 0077_gmv_hermes_mysql_memory
Revises: 0076_creative_asset_collation
Create Date: 2026-07-10 14:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0077_gmv_hermes_mysql_memory"
down_revision = "0076_creative_asset_collation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "gmv_hermes_ad_policy_evaluations" not in tables:
        op.create_table(
            "gmv_hermes_ad_policy_evaluations",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("auth_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=64), nullable=False),
            sa.Column("item_group_id", sa.String(length=64), nullable=False),
            sa.Column("report_date", sa.Date(), nullable=False),
            sa.Column("source_report_id", mysql.BIGINT(unsigned=True), nullable=True),
            sa.Column("source_plan_default_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("params_json", sa.JSON(), nullable=False),
            sa.Column("threshold_json", sa.JSON(), nullable=False),
            sa.Column("cost_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("gross_revenue_cents", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("orders", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("roi", sa.Numeric(18, 6), nullable=False, server_default="0"),
            sa.Column("target_roi", sa.Numeric(18, 6), nullable=False, server_default="0"),
            sa.Column("outcome", sa.String(length=32), nullable=False),
            sa.Column("reason", sa.String(length=512), nullable=True),
            sa.Column("observed_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column(
                "created_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column(
                "updated_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            ),
            sa.UniqueConstraint(
                "source_plan_default_id",
                "report_date",
                name="uq_gmv_hermes_policy_evaluation",
            ),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
        )
        op.create_index(
            "idx_gmv_hermes_policy_scope_date",
            "gmv_hermes_ad_policy_evaluations",
            ["workspace_id", "auth_id", "advertiser_id", "store_id", "report_date"],
        )
        op.create_index(
            "idx_gmv_hermes_policy_product_date",
            "gmv_hermes_ad_policy_evaluations",
            ["item_group_id", "report_date", "outcome"],
        )

    if "gmv_hermes_ad_memory_facts" not in tables:
        op.create_table(
            "gmv_hermes_ad_memory_facts",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("workspace_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("auth_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=64), nullable=False),
            sa.Column("item_group_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("memory_type", sa.String(length=64), nullable=False),
            sa.Column("subject_key", sa.String(length=191), nullable=False),
            sa.Column("statement", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="CANDIDATE"),
            sa.Column("confidence", sa.Numeric(8, 6), nullable=False, server_default="0"),
            sa.Column("independent_days", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("evidence_orders", mysql.BIGINT(), nullable=False, server_default="0"),
            sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("evidence_json", sa.JSON(), nullable=False),
            sa.Column("first_observed_date", sa.Date(), nullable=True),
            sa.Column("last_observed_date", sa.Date(), nullable=True),
            sa.Column("last_validated_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column(
                "created_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column(
                "updated_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            ),
            sa.UniqueConstraint(
                "workspace_id",
                "auth_id",
                "advertiser_id",
                "store_id",
                "item_group_id",
                "memory_type",
                "subject_key",
                name="uq_gmv_hermes_memory_fact",
            ),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
        )
        op.create_index(
            "idx_gmv_hermes_memory_scope_status",
            "gmv_hermes_ad_memory_facts",
            ["workspace_id", "auth_id", "advertiser_id", "store_id", "status", "updated_at"],
        )
        op.create_index(
            "idx_gmv_hermes_memory_product",
            "gmv_hermes_ad_memory_facts",
            ["item_group_id", "memory_type", "status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "gmv_hermes_ad_memory_facts" in tables:
        op.drop_table("gmv_hermes_ad_memory_facts")
    if "gmv_hermes_ad_policy_evaluations" in tables:
        op.drop_table("gmv_hermes_ad_policy_evaluations")

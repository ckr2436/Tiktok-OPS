"""migrate gmv action/strategy and introduce creative heating"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.sql import text

revision = "0044_gmv_creative_heating_and_action_strategy"
down_revision = "0043_gmv_action_strategy"
branch_labels = None
depends_on = None


promotion_enum = sa.Enum("PRODUCT", "LIVE", name="promotiontypeenum")


ID_TYPE = sa.Integer().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def _create_creative_heating_table() -> None:
    op.create_table(
        "gmv_creative_heating",
        sa.Column("id", ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("creative_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=True),
        sa.Column("promotion_type", promotion_enum, nullable=False),
        sa.Column("creative_name", sa.String(length=255), nullable=True),
        sa.Column("product_id", sa.String(length=64), nullable=True),
        sa.Column("item_id", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=True),
        sa.Column("target_daily_budget", sa.Numeric(18, 4), nullable=True),
        sa.Column("budget_delta", sa.Numeric(18, 4), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("max_duration_minutes", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("evaluation_window_minutes", sa.Integer(), nullable=False, server_default=sa.text("60")),
        sa.Column("min_clicks", sa.Integer(), nullable=True),
        sa.Column("min_ctr", sa.Numeric(10, 4), nullable=True),
        sa.Column("min_gross_revenue", sa.Numeric(18, 4), nullable=True),
        sa.Column("auto_stop_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_heating_active", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("last_action_type", sa.String(length=64), nullable=True),
        sa.Column("last_action_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_status", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_action_request", sa.JSON(), nullable=True),
        sa.Column("last_action_response", sa.JSON(), nullable=True),
        sa.Column("last_evaluated_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_evaluation_result", sa.String(length=64), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id", "auth_id", "campaign_id", "creative_id", "promotion_type", name="uk_gmv_creative_heating_scope"
        ),
        sa.Index(
            "idx_gmv_creative_heating_campaign", "workspace_id", "auth_id", "campaign_id"
        ),
        sa.Index(
            "idx_gmv_creative_heating_creative", "workspace_id", "auth_id", "creative_id"
        ),
        sa.Index(
            "idx_gmv_creative_heating_status", "workspace_id", "auth_id", "status"
        ),
    )


def _backfill_action_logs(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO gmv_action_logs (
                workspace_id,
                auth_id,
                campaign_id,
                action,
                reason,
                before_json,
                after_json,
                performed_by,
                result,
                error_message,
                created_at
            )
            SELECT
                legacy.workspace_id,
                legacy.auth_id,
                gmv_campaigns.id AS campaign_pk,
                legacy.action,
                legacy.reason,
                legacy.before_json,
                legacy.after_json,
                legacy.performed_by,
                legacy.result,
                legacy.error_message,
                legacy.created_at
            FROM ttb_gmvmax_action_logs AS legacy
            JOIN ttb_gmvmax_campaigns AS c ON c.id = legacy.campaign_id
            JOIN gmv_campaigns ON gmv_campaigns.workspace_id = legacy.workspace_id
                AND gmv_campaigns.auth_id = legacy.auth_id
                AND gmv_campaigns.campaign_id = c.campaign_id
            LEFT JOIN gmv_action_logs AS tgt ON tgt.workspace_id = legacy.workspace_id
                AND tgt.auth_id = legacy.auth_id
                AND tgt.campaign_id = gmv_campaigns.id
                AND tgt.action = legacy.action
                AND tgt.created_at = legacy.created_at
            WHERE tgt.id IS NULL
            """
        )
    )


def _backfill_strategy_configs(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO gmv_strategy_configs (
                workspace_id,
                auth_id,
                campaign_id,
                enabled,
                target_roi,
                min_roi,
                max_roi,
                min_impressions,
                min_clicks,
                max_budget_raise_pct_per_day,
                max_budget_cut_pct_per_day,
                max_roas_step_per_adjust,
                cooldown_minutes,
                min_runtime_minutes_before_first_change,
                config_json,
                created_at,
                updated_at
            )
            SELECT
                legacy.workspace_id,
                legacy.auth_id,
                legacy.campaign_id,
                legacy.enabled,
                legacy.target_roi,
                legacy.min_roi,
                legacy.max_roi,
                legacy.min_impressions,
                legacy.min_clicks,
                legacy.max_budget_raise_pct_per_day,
                legacy.max_budget_cut_pct_per_day,
                legacy.max_roas_step_per_adjust,
                legacy.cooldown_minutes,
                legacy.min_runtime_minutes_before_first_change,
                legacy.config_json,
                legacy.created_at,
                legacy.updated_at
            FROM ttb_gmvmax_strategy_config AS legacy
            LEFT JOIN gmv_strategy_configs AS tgt
                ON tgt.workspace_id = legacy.workspace_id
                AND tgt.auth_id = legacy.auth_id
                AND tgt.campaign_id = legacy.campaign_id
            WHERE tgt.id IS NULL
            """
        )
    )


def _backfill_creative_heating(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO gmv_creative_heating (
                workspace_id,
                auth_id,
                advertiser_id,
                campaign_id,
                creative_id,
                item_group_id,
                promotion_type,
                creative_name,
                product_id,
                item_id,
                mode,
                target_daily_budget,
                budget_delta,
                currency,
                max_duration_minutes,
                note,
                evaluation_window_minutes,
                min_clicks,
                min_ctr,
                min_gross_revenue,
                auto_stop_enabled,
                is_heating_active,
                status,
                last_action_type,
                last_action_at,
                last_status,
                last_error,
                last_action_request,
                last_action_response,
                last_evaluated_at,
                last_evaluation_result,
                created_at,
                updated_at
            )
            SELECT
                legacy.workspace_id,
                legacy.auth_id,
                COALESCE(c.advertiser_id, ''),
                legacy.campaign_id,
                legacy.creative_id,
                legacy.product_id,
                'PRODUCT',
                legacy.creative_name,
                legacy.product_id,
                legacy.item_id,
                legacy.mode,
                legacy.target_daily_budget,
                legacy.budget_delta,
                legacy.currency,
                legacy.max_duration_minutes,
                legacy.note,
                legacy.evaluation_window_minutes,
                legacy.min_clicks,
                legacy.min_ctr,
                legacy.min_gross_revenue,
                legacy.auto_stop_enabled,
                legacy.is_heating_active,
                legacy.status,
                legacy.last_action_type,
                legacy.last_action_time,
                legacy.status,
                legacy.last_error,
                legacy.last_action_request,
                legacy.last_action_response,
                legacy.last_evaluated_at,
                legacy.last_evaluation_result,
                legacy.created_at,
                legacy.updated_at
            FROM ttb_gmvmax_creative_heating AS legacy
            LEFT JOIN gmv_creative_heating AS tgt ON tgt.workspace_id = legacy.workspace_id
                AND tgt.auth_id = legacy.auth_id
                AND tgt.campaign_id = legacy.campaign_id
                AND tgt.creative_id = legacy.creative_id
            LEFT JOIN gmv_campaigns AS c ON c.workspace_id = legacy.workspace_id
                AND c.auth_id = legacy.auth_id
                AND c.campaign_id = legacy.campaign_id
            WHERE tgt.id IS NULL
            """
        )
    )


def upgrade():
    _create_creative_heating_table()
    conn = op.get_bind()
    _backfill_action_logs(conn)
    _backfill_strategy_configs(conn)
    _backfill_creative_heating(conn)


def downgrade():
    op.drop_table("gmv_creative_heating")

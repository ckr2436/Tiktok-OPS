"""create GMV Max strategy config table"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect


# revision identifiers, used by Alembic.
revision = "0021_ttb_gmvmax_strategy_config"
down_revision = "0020_ttb_gmvmax_uniques"
branch_labels = None
depends_on = None


def _dt6():
    return mysql_dialect.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "ttb_gmvmax_strategy_config",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("auth_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("target_roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("min_roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("max_roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("min_impressions", sa.Integer(), nullable=True),
        sa.Column("min_clicks", sa.Integer(), nullable=True),
        sa.Column("max_budget_raise_pct_per_day", sa.Numeric(5, 2), nullable=True),
        sa.Column("max_budget_cut_pct_per_day", sa.Numeric(5, 2), nullable=True),
        sa.Column("max_roas_step_per_adjust", sa.Numeric(10, 4), nullable=True),
        sa.Column("cooldown_minutes", sa.Integer(), nullable=True),
        sa.Column("min_runtime_minutes_before_first_change", sa.Integer(), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "updated_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "campaign_id",
            name="uq_gmvmax_strategy_workspace_auth_campaign",
        ),
    )

    op.create_index(
        "idx_gmvmax_strategy_workspace",
        "ttb_gmvmax_strategy_config",
        ["workspace_id"],
    )
    op.create_index(
        "idx_gmvmax_strategy_auth",
        "ttb_gmvmax_strategy_config",
        ["auth_id"],
    )
    op.create_index(
        "idx_gmvmax_strategy_campaign",
        "ttb_gmvmax_strategy_config",
        ["campaign_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_gmvmax_strategy_campaign", table_name="ttb_gmvmax_strategy_config")
    op.drop_index("idx_gmvmax_strategy_auth", table_name="ttb_gmvmax_strategy_config")
    op.drop_index("idx_gmvmax_strategy_workspace", table_name="ttb_gmvmax_strategy_config")
    op.drop_table("ttb_gmvmax_strategy_config")

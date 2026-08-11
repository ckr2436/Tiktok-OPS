"""Add auto-stop fields to GMV Max creative heating."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect


revision = "0025_ttb_gmvmax_creative_heating_rules"
down_revision = "0024_ttb_gmvmax_creative_heating"
branch_labels = None
depends_on = None


def _dt6():
    return mysql_dialect.DATETIME(fsp=6)


def upgrade() -> None:
    with op.batch_alter_table("ttb_gmvmax_creative_heating", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "evaluation_window_minutes",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("60"),
            )
        )
        batch_op.add_column(sa.Column("min_clicks", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("min_ctr", sa.Numeric(10, 4), nullable=True))
        batch_op.add_column(
            sa.Column("min_gross_revenue", sa.Numeric(18, 4), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "auto_stop_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")
            )
        )
        batch_op.add_column(
            sa.Column(
                "is_heating_active", sa.Boolean(), nullable=False, server_default=sa.text("0")
            )
        )
        batch_op.add_column(sa.Column("last_evaluated_at", _dt6(), nullable=True))
        batch_op.add_column(sa.Column("last_evaluation_result", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ttb_gmvmax_creative_heating", schema=None) as batch_op:
        batch_op.drop_column("last_evaluation_result")
        batch_op.drop_column("last_evaluated_at")
        batch_op.drop_column("is_heating_active")
        batch_op.drop_column("auto_stop_enabled")
        batch_op.drop_column("min_gross_revenue")
        batch_op.drop_column("min_ctr")
        batch_op.drop_column("min_clicks")
        batch_op.drop_column("evaluation_window_minutes")

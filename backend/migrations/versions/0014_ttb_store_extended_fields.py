"""Add TikTok store metadata fields"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "0014_ttb_store_extended_fields"
down_revision = "0013_ttb_advertiser_display_timezone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "ttb_stores" not in inspector.get_table_names():
        return

    with op.batch_alter_table("ttb_stores", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "store_type",
                sa.String(length=32),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(
            sa.Column(
                "store_code",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(
            sa.Column(
                "store_authorized_bc_id",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )

    op.execute(
        """
        UPDATE ttb_stores
        SET
            store_type = COALESCE(NULLIF(store_type, ''), 'TIKTOK_SHOP'),
            store_code = COALESCE(store_code, ''),
            store_authorized_bc_id = COALESCE(store_authorized_bc_id, '')
        """
    )

    with op.batch_alter_table("ttb_stores", schema=None) as batch_op:
        batch_op.alter_column("store_type", server_default=None)
        batch_op.alter_column("store_code", server_default=None)
        batch_op.alter_column("store_authorized_bc_id", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "ttb_stores" not in inspector.get_table_names():
        return

    with op.batch_alter_table("ttb_stores", schema=None) as batch_op:
        batch_op.drop_column("store_authorized_bc_id")
        batch_op.drop_column("store_code")
        batch_op.drop_column("store_type")

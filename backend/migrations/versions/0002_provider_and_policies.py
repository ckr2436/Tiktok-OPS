"""provider registry and policies"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect


revision = "0002_provider_and_policies"
down_revision = "0001_full_schema"
branch_labels = None
depends_on = None


UBigInt = (
    sa.BigInteger()
    .with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")
    .with_variant(sa.Integer(), "sqlite")
)

policy_mode_enum = sa.Enum("whitelist", "blacklist", name="ttb_policy_mode")
policy_domain_enum = sa.Enum("bc", "advertiser", "shop", "product", name="ttb_policy_domain")


def _ts_created():
    return mysql_dialect.TIMESTAMP(fsp=6), sa.text("CURRENT_TIMESTAMP(6)")


def _ts_updated():
    return (
        mysql_dialect.TIMESTAMP(fsp=6),
        sa.text("CURRENT_TIMESTAMP(6)"),
        sa.text("CURRENT_TIMESTAMP(6)"),
    )


def _dt6():
    return mysql_dialect.DATETIME(fsp=6)


def upgrade() -> None:
    bind = op.get_bind()
    is_mysql = bind.dialect.name == "mysql"

    if not is_mysql:
        policy_mode_enum.create(bind, checkfirst=True)
        policy_domain_enum.create(bind, checkfirst=True)

    col_c_t, col_c_def = _ts_created()
    col_u_t, col_u_def, col_u_onupd = _ts_updated()

    op.create_table(
        "platform_providers",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", col_c_t, nullable=False, server_default=col_c_def),
        sa.Column("updated_at", col_u_t, nullable=False, server_default=col_u_def, server_onupdate=col_u_onupd),
        sa.UniqueConstraint("key", name="uq_platform_providers_key"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        sqlite_autoincrement=True,
    )

    op.create_table(
        "ttb_platform_policies",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "provider_key",
            sa.String(64),
            sa.ForeignKey("platform_providers.key", onupdate="CASCADE", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UBigInt,
            sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "mode",
            policy_mode_enum if not is_mysql else sa.Enum("whitelist", "blacklist", name="ttb_policy_mode"),
            nullable=False,
        ),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "created_by_user_id",
            UBigInt,
            sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by_user_id",
            UBigInt,
            sa.ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "updated_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_policies_provider_enabled",
        "ttb_platform_policies",
        ["provider_key", "is_enabled"],
    )
    op.create_index(
        "idx_policies_workspace_enabled",
        "ttb_platform_policies",
        ["workspace_id", "is_enabled"],
    )

    op.create_table(
        "ttb_policy_items",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "policy_id",
            UBigInt,
            sa.ForeignKey("ttb_platform_policies.id", onupdate="CASCADE", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "domain",
            policy_domain_enum
            if not is_mysql
            else sa.Enum("bc", "advertiser", "shop", "product", name="ttb_policy_domain"),
            nullable=False,
        ),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.UniqueConstraint("policy_id", "domain", "item_id", name="uq_policy_item"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("ttb_policy_items")
    op.drop_index("idx_policies_workspace_enabled", table_name="ttb_platform_policies")
    op.drop_index("idx_policies_provider_enabled", table_name="ttb_platform_policies")
    op.drop_table("ttb_platform_policies")
    op.drop_table("platform_providers")

    bind = op.get_bind()
    is_mysql = bind.dialect.name == "mysql"
    if not is_mysql:
        policy_domain_enum.drop(bind, checkfirst=True)
        policy_mode_enum.drop(bind, checkfirst=True)

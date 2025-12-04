"""introduce gmv campaign sync snapshots"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy import inspect

revision = "0045_gmv_campaign_sync_snapshots"
down_revision = "0044_gmv_creative_heating_and_action_strategy"
branch_labels = None
depends_on = None


ID_TYPE = sa.Integer().with_variant(mysql.BIGINT(unsigned=True), "mysql")
promotion_enum = sa.Enum("PRODUCT", "LIVE", name="promotiontypeenum")
NEW_TABLE = "gmv_campaign_sync_snapshots"
LEGACY_TABLE = "ttb_gmvmax_campaign_sync_snapshots"


def _has_table(name: str) -> bool:
    insp = inspect(op.get_bind())
    return name in insp.get_table_names()


def _create_snapshot_table() -> None:
    if _has_table(NEW_TABLE):
        return
    op.create_table(
        NEW_TABLE,
        sa.Column("id", ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("promotion_type", promotion_enum, nullable=True),
        sa.Column("snapshot_type", sa.String(length=64), nullable=False, server_default=sa.text("'CAMPAIGN'")),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("raw_request_id", sa.String(length=64), nullable=True),
        sa.Column("synced_at", mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "snapshot_type",
            "synced_at",
            name="uk_gmv_campaign_sync_snapshot",
        ),
        sa.Index(
            "idx_gmv_campaign_sync_snapshot_scope",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
        ),
        sa.Index("idx_gmv_campaign_sync_snapshot_time", "synced_at"),
    )


def _backfill_snapshots() -> None:
    if not _has_table(NEW_TABLE) or not _has_table(LEGACY_TABLE):
        return
    conn = op.get_bind()
    rows = list(
        conn.execute(
            sa.text(
                f"SELECT workspace_id, auth_id, advertiser_id, store_id, campaign_id, synced_at, raw_json, created_at FROM {LEGACY_TABLE}"
            )
        ).mappings()
    )
    for row in rows:
        params = {
            "workspace_id": row["workspace_id"],
            "auth_id": row["auth_id"],
            "advertiser_id": row["advertiser_id"],
            "store_id": row.get("store_id") or "",
            "campaign_id": row["campaign_id"],
            "snapshot_type": "CAMPAIGN",
            "synced_at": row["synced_at"],
            "payload_json": row.get("raw_json"),
            "promotion_type": None,
            "source": None,
            "raw_request_id": None,
            "created_at": row.get("created_at"),
            "updated_at": row.get("created_at"),
        }
        exists = conn.execute(
            sa.text(
                f"SELECT 1 FROM {NEW_TABLE} WHERE workspace_id=:workspace_id AND auth_id=:auth_id AND advertiser_id=:advertiser_id AND store_id=:store_id AND campaign_id=:campaign_id AND snapshot_type=:snapshot_type AND synced_at=:synced_at LIMIT 1"
            ),
            params,
        ).scalar()
        if exists:
            continue
        conn.execute(
            sa.text(
                f"""
                INSERT INTO {NEW_TABLE} (
                    workspace_id, auth_id, advertiser_id, store_id, campaign_id, promotion_type,
                    snapshot_type, payload_json, source, raw_request_id, synced_at, is_deleted,
                    deleted_at, created_at, updated_at
                ) VALUES (
                    :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id, :promotion_type,
                    :snapshot_type, :payload_json, :source, :raw_request_id, :synced_at, 0,
                    :deleted_at, :created_at, :updated_at
                )
                """
            ),
            {**params, "deleted_at": None},
        )


def upgrade() -> None:
    _create_snapshot_table()
    _backfill_snapshots()


def downgrade() -> None:
    if _has_table(NEW_TABLE):
        op.drop_table(NEW_TABLE)

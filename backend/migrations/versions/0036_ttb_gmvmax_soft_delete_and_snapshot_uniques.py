"""gmvmax soft delete and snapshot dedupe

Revision ID: 0036_ttb_gmvmax_soft_delete_and_snapshot_uniques
Revises: 0035_ttb_gmvmax_sync_snapshots
Create Date: 2024-10-01 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.mysql import DATETIME as mysql_datetime

# revision identifiers, used by Alembic.
revision = "0036_ttb_gmvmax_soft_delete_and_snapshot_uniques"
down_revision = "0035_ttb_gmvmax_sync_snapshots"
branch_labels = None
depends_on = None

SNAPSHOT_TABLE = "ttb_gmvmax_campaign_sync_snapshots"
CAMPAIGN_TABLE = "ttb_gmvmax_campaigns"


def _has_table(name: str) -> bool:
    insp = inspect(op.get_bind())
    return name in insp.get_table_names()


def _drop_constraint_if_exists(table: str, name: str) -> None:
    insp = inspect(op.get_bind())
    if name in {uc.get("name") for uc in insp.get_unique_constraints(table)}:
        op.drop_constraint(name, table, type_="unique")


def _drop_index_if_exists(table: str, name: str) -> None:
    insp = inspect(op.get_bind())
    if name in {ix.get("name") for ix in insp.get_indexes(table)}:
        op.drop_index(name, table_name=table)


def _create_index_if_not_exists(
    table: str, name: str, columns: list[str], unique: bool = False
) -> None:
    insp = inspect(op.get_bind())
    if name in {ix.get("name") for ix in insp.get_indexes(table)}:
        return
    if unique:
        op.create_unique_constraint(name, table, columns)
    else:
        op.create_index(name, table, columns)


def _delete_duplicates(table: str, partition_cols: list[str]) -> None:
    if not _has_table(table):
        return
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        partition_expr = ", ".join(partition_cols)
        op.execute(
            sa.text(
                f"""
                DELETE FROM {table}
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (PARTITION BY {partition_expr} ORDER BY id) AS rn
                        FROM {table}
                    ) AS ranked
                    WHERE rn > 1
                )
                """
            )
        )
        return

    join_conditions = " AND ".join([f"t1.{col} = t2.{col}" for col in partition_cols])
    op.execute(
        sa.text(
            f"""
            DELETE t1 FROM {table} t1
            JOIN {table} t2
              ON {join_conditions}
             AND t1.id > t2.id
            """
        )
    )


def upgrade() -> None:
    if _has_table(SNAPSHOT_TABLE):
        _delete_duplicates(
            SNAPSHOT_TABLE,
            ["workspace_id", "advertiser_id", "store_id", "campaign_id"],
        )
        _drop_constraint_if_exists(SNAPSHOT_TABLE, "uk_ttb_gmvmax_sync_snapshot")
        _drop_index_if_exists(SNAPSHOT_TABLE, "idx_ttb_gmvmax_sync_snapshot_scope")
        op.create_unique_constraint(
            "uniq_gmvmax_snapshot",
            SNAPSHOT_TABLE,
            ["workspace_id", "advertiser_id", "store_id", "campaign_id"],
        )
        op.create_index(
            "idx_ttb_gmvmax_sync_snapshot_scope",
            SNAPSHOT_TABLE,
            ["workspace_id", "advertiser_id", "store_id"],
        )

    if _has_table(CAMPAIGN_TABLE):
        _delete_duplicates(
            CAMPAIGN_TABLE,
            ["workspace_id", "advertiser_id", "store_id", "campaign_id"],
        )
        with op.batch_alter_table(CAMPAIGN_TABLE) as batch_op:
            if "is_deleted" not in {c.get("name") for c in inspect(op.get_bind()).get_columns(CAMPAIGN_TABLE)}:
                batch_op.add_column(
                    sa.Column(
                        "is_deleted",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.text("0"),
                        comment="0: 未删除, 1: 已删除",
                    )
                )
            if "deleted_at" not in {c.get("name") for c in inspect(op.get_bind()).get_columns(CAMPAIGN_TABLE)}:
                batch_op.add_column(sa.Column("deleted_at", mysql_datetime(fsp=6), nullable=True))
        # Ensure foreign keys still have supporting indexes when the legacy unique
        # index is dropped. MySQL requires an index on referenced columns, and the
        # existing unique index currently fulfills that requirement.
        _create_index_if_not_exists(
            CAMPAIGN_TABLE, "idx_ttb_gmvmax_campaign_workspace_id", ["workspace_id"]
        )
        _create_index_if_not_exists(
            CAMPAIGN_TABLE, "idx_ttb_gmvmax_campaign_auth_id", ["auth_id"]
        )
        # Create the new unique constraint first so there is always an index that
        # satisfies the foreign key requirements, then drop the legacy unique.
        _create_index_if_not_exists(
            CAMPAIGN_TABLE,
            "uniq_gmvmax_campaign",
            ["workspace_id", "advertiser_id", "store_id", "campaign_id"],
            unique=True,
        )
        _drop_constraint_if_exists(CAMPAIGN_TABLE, "uk_ttb_gmvmax_campaign_scope")


def downgrade() -> None:
    if _has_table(SNAPSHOT_TABLE):
        _drop_constraint_if_exists(SNAPSHOT_TABLE, "uniq_gmvmax_snapshot")
        _drop_index_if_exists(SNAPSHOT_TABLE, "idx_ttb_gmvmax_sync_snapshot_scope")
        op.create_unique_constraint(
            "uk_ttb_gmvmax_sync_snapshot",
            SNAPSHOT_TABLE,
            ["workspace_id", "auth_id", "advertiser_id", "store_id", "campaign_id", "synced_at"],
        )
        op.create_index(
            "idx_ttb_gmvmax_sync_snapshot_scope",
            SNAPSHOT_TABLE,
            ["workspace_id", "auth_id", "advertiser_id", "store_id"],
        )

    if _has_table(CAMPAIGN_TABLE):
        _drop_constraint_if_exists(CAMPAIGN_TABLE, "uniq_gmvmax_campaign")
        op.create_unique_constraint(
            "uk_ttb_gmvmax_campaign_scope",
            CAMPAIGN_TABLE,
            ["workspace_id", "auth_id", "campaign_id"],
        )
        _drop_index_if_exists(CAMPAIGN_TABLE, "idx_ttb_gmvmax_campaign_auth_id")
        _drop_index_if_exists(CAMPAIGN_TABLE, "idx_ttb_gmvmax_campaign_workspace_id")
        with op.batch_alter_table(CAMPAIGN_TABLE) as batch_op:
            batch_op.drop_column("deleted_at")
            batch_op.drop_column("is_deleted")

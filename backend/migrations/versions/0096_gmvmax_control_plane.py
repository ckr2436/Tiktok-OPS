"""Persist GMV Max task ownership, schedules, overrides, uploads, and guard lease.

Revision ID: 0096_gmvmax_control_plane
Revises: 0095_gmv_data_accuracy
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0096_gmvmax_control_plane"
down_revision = "0095_gmv_data_accuracy"
branch_labels = None
depends_on = None


UBIGINT = mysql.BIGINT(unsigned=True)
DT6 = mysql.DATETIME(fsp=6)


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _manual_upload_repair_columns() -> dict[str, sa.Column]:
    """Columns formerly created piecemeal by request-time runtime DDL."""

    return {
        "workspace_id": sa.Column("workspace_id", UBIGINT, nullable=True),
        "auth_id": sa.Column("auth_id", UBIGINT, nullable=True),
        "advertiser_id": sa.Column("advertiser_id", sa.String(length=64), nullable=True),
        "store_id": sa.Column("store_id", sa.String(length=64), nullable=True),
        "item_group_id": sa.Column("item_group_id", sa.String(length=64), nullable=True),
        "upload_id": sa.Column("upload_id", sa.String(length=64), nullable=True),
        "title": sa.Column("title", sa.String(length=500), nullable=True),
        "file_name": sa.Column("file_name", sa.String(length=500), nullable=True),
        "mime_type": sa.Column("mime_type", sa.String(length=128), nullable=True),
        "file_size": sa.Column("file_size", UBIGINT, nullable=True),
        "file_path": sa.Column("file_path", sa.Text(), nullable=True),
        "upload_status": sa.Column(
            "upload_status",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'LOCAL_ONLY'"),
        ),
        "tiktok_account_id": sa.Column("tiktok_account_id", UBIGINT, nullable=True),
        "tiktok_business_id": sa.Column(
            "tiktok_business_id", sa.String(length=128), nullable=True
        ),
        "publish_id": sa.Column("publish_id", sa.String(length=191), nullable=True),
        "tiktok_item_id": sa.Column("tiktok_item_id", sa.String(length=64), nullable=True),
        "tiktok_video_id": sa.Column("tiktok_video_id", sa.String(length=128), nullable=True),
        "tiktok_material_id": sa.Column(
            "tiktok_material_id", sa.String(length=128), nullable=True
        ),
        "tiktok_preview_url": sa.Column("tiktok_preview_url", sa.Text(), nullable=True),
        "tiktok_video_cover_url": sa.Column(
            "tiktok_video_cover_url", sa.Text(), nullable=True
        ),
        "identity_id": sa.Column("identity_id", sa.String(length=128), nullable=True),
        "identity_type": sa.Column("identity_type", sa.String(length=64), nullable=True),
        "identity_info_json": sa.Column("identity_info_json", sa.JSON(), nullable=True),
        "anchor_status": sa.Column("anchor_status", sa.String(length=64), nullable=True),
        "public_url": sa.Column("public_url", sa.Text(), nullable=True),
        "upload_error": sa.Column("upload_error", sa.Text(), nullable=True),
        "notes": sa.Column("notes", sa.Text(), nullable=True),
        "raw_json": sa.Column("raw_json", sa.JSON(), nullable=True),
        "created_at": sa.Column(
            "created_at",
            DT6,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        "updated_at": sa.Column(
            "updated_at",
            DT6,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    }


def _repair_existing_manual_upload_table() -> None:
    """Bring legacy runtime-created upload tables to the migration contract."""

    table_name = "gmvmax_manual_creative_uploads"
    inspector = sa.inspect(op.get_bind())
    existing_columns = {
        str(column.get("name")) for column in inspector.get_columns(table_name)
    }
    for column_name, column in _manual_upload_repair_columns().items():
        if column_name not in existing_columns:
            if (
                op.get_bind().dialect.name == "sqlite"
                and column_name in {"created_at", "updated_at"}
            ):
                column = sa.Column(column_name, sa.DateTime(), nullable=True)
            op.add_column(table_name, column)

    inspector = sa.inspect(op.get_bind())
    columns = {
        str(column.get("name")) for column in inspector.get_columns(table_name)
    }
    unique_columns = (
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "upload_id",
    )
    unique_constraints = inspector.get_unique_constraints(table_name)
    unique_indexes = [
        index for index in inspector.get_indexes(table_name) if index.get("unique")
    ]
    existing_unique_sets = {
        tuple(str(column) for column in item.get("column_names") or [])
        for item in [*unique_constraints, *unique_indexes]
    }
    if set(unique_columns).issubset(columns) and unique_columns not in existing_unique_sets:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.create_unique_constraint(
                "uq_gmvmax_manual_upload_scope",
                list(unique_columns),
            )

    inspector = sa.inspect(op.get_bind())
    index_names = {
        str(index.get("name")) for index in inspector.get_indexes(table_name)
    }
    product_columns = [
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "item_group_id",
    ]
    if (
        set(product_columns).issubset(columns)
        and "idx_gmvmax_manual_upload_product" not in index_names
    ):
        op.create_index(
            "idx_gmvmax_manual_upload_product",
            table_name,
            product_columns,
        )


def upgrade() -> None:
    existing = _table_names()

    if "gmvmax_task_ownership" not in existing:
        op.create_table(
            "gmvmax_task_ownership",
            sa.Column("task_id", sa.String(length=64), primary_key=True),
            sa.Column("workspace_id", UBIGINT, nullable=False),
            sa.Column("auth_id", UBIGINT, nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("task_name", sa.String(length=128), nullable=False),
            sa.Column("created_by_user_id", UBIGINT, nullable=True),
            sa.Column(
                "created_at",
                DT6,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column("expires_at", DT6, nullable=False),
        )
        op.create_index(
            "idx_gmvmax_task_owner_scope",
            "gmvmax_task_ownership",
            ["workspace_id", "auth_id", "created_at"],
        )
        op.create_index(
            "idx_gmvmax_task_owner_expiry",
            "gmvmax_task_ownership",
            ["expires_at"],
        )

    if "gmvmax_sync_schedules" not in existing:
        op.create_table(
            "gmvmax_sync_schedules",
            sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
            sa.Column("workspace_id", UBIGINT, nullable=False),
            sa.Column("auth_id", UBIGINT, nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=64), nullable=True),
            sa.Column(
                "interval_minutes",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("10"),
            ),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column("next_run_at", DT6, nullable=False),
            sa.Column("last_enqueued_at", DT6, nullable=True),
            sa.Column("last_task_id", sa.String(length=64), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_by_user_id", UBIGINT, nullable=True),
            sa.Column("updated_by_user_id", UBIGINT, nullable=True),
            sa.Column(
                "created_at",
                DT6,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column(
                "updated_at",
                DT6,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.UniqueConstraint(
                "workspace_id",
                "auth_id",
                "provider",
                name="uq_gmvmax_sync_schedule_scope",
            ),
        )
        op.create_index(
            "idx_gmvmax_sync_schedule_due",
            "gmvmax_sync_schedules",
            ["enabled", "next_run_at"],
        )

    if "gmvmax_campaign_manual_overrides" not in existing:
        op.create_table(
            "gmvmax_campaign_manual_overrides",
            sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
            sa.Column("workspace_id", UBIGINT, nullable=False),
            sa.Column("auth_id", UBIGINT, nullable=False),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=64), nullable=True),
            sa.Column("campaign_id", sa.String(length=64), nullable=False),
            sa.Column("override_type", sa.String(length=32), nullable=False),
            sa.Column(
                "active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column("reason", sa.String(length=500), nullable=True),
            sa.Column("actor", sa.String(length=191), nullable=True),
            sa.Column("expires_at", DT6, nullable=True),
            sa.Column(
                "created_at",
                DT6,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column(
                "updated_at",
                DT6,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.UniqueConstraint(
                "workspace_id",
                "auth_id",
                "advertiser_id",
                "store_id",
                "campaign_id",
                name="uq_gmvmax_manual_override_campaign",
            ),
        )
        op.create_index(
            "idx_gmvmax_manual_override_active",
            "gmvmax_campaign_manual_overrides",
            ["workspace_id", "auth_id", "active", "override_type"],
        )

    if "gmvmax_guard_action_leases" not in existing:
        op.create_table(
            "gmvmax_guard_action_leases",
            sa.Column("lease_name", sa.String(length=128), primary_key=True),
            sa.Column("owner_token", sa.String(length=128), nullable=True),
            sa.Column(
                "fencing_token",
                UBIGINT,
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("acquired_at", DT6, nullable=True),
            sa.Column("expires_at", DT6, nullable=True),
            sa.Column(
                "updated_at",
                DT6,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
        )

    if "gmvmax_manual_creative_uploads" not in existing:
        op.create_table(
            "gmvmax_manual_creative_uploads",
            sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
            sa.Column("workspace_id", UBIGINT, nullable=False),
            sa.Column("auth_id", UBIGINT, nullable=False),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=64), nullable=False),
            sa.Column("item_group_id", sa.String(length=64), nullable=True),
            sa.Column("upload_id", sa.String(length=64), nullable=False),
            sa.Column("title", sa.String(length=500), nullable=True),
            sa.Column("file_name", sa.String(length=500), nullable=True),
            sa.Column("mime_type", sa.String(length=128), nullable=True),
            sa.Column("file_size", UBIGINT, nullable=True),
            sa.Column("file_path", sa.Text(), nullable=False),
            sa.Column(
                "upload_status",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'LOCAL_ONLY'"),
            ),
            sa.Column("tiktok_account_id", UBIGINT, nullable=True),
            sa.Column("tiktok_business_id", sa.String(length=128), nullable=True),
            sa.Column("publish_id", sa.String(length=191), nullable=True),
            sa.Column("tiktok_item_id", sa.String(length=64), nullable=True),
            sa.Column("tiktok_video_id", sa.String(length=128), nullable=True),
            sa.Column("tiktok_material_id", sa.String(length=128), nullable=True),
            sa.Column("tiktok_preview_url", sa.Text(), nullable=True),
            sa.Column("tiktok_video_cover_url", sa.Text(), nullable=True),
            sa.Column("identity_id", sa.String(length=128), nullable=True),
            sa.Column("identity_type", sa.String(length=64), nullable=True),
            sa.Column("identity_info_json", sa.JSON(), nullable=True),
            sa.Column("anchor_status", sa.String(length=64), nullable=True),
            sa.Column("public_url", sa.Text(), nullable=True),
            sa.Column("upload_error", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("raw_json", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                DT6,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column(
                "updated_at",
                DT6,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.UniqueConstraint(
                "workspace_id",
                "auth_id",
                "advertiser_id",
                "store_id",
                "upload_id",
                name="uq_gmvmax_manual_upload_scope",
            ),
        )
        op.create_index(
            "idx_gmvmax_manual_upload_product",
            "gmvmax_manual_creative_uploads",
            [
                "workspace_id",
                "auth_id",
                "advertiser_id",
                "store_id",
                "item_group_id",
            ],
        )
    else:
        _repair_existing_manual_upload_table()

    # Preserve the former default periodic behavior while moving ownership to
    # account-scoped persisted schedules. Explicit user updates supersede it.
    if (
        op.get_bind().dialect.name == "mysql"
        and "ttb_binding_configs" in _table_names()
    ):
        op.execute(
            """
            insert ignore into gmvmax_sync_schedules (
                workspace_id, auth_id, provider, advertiser_id, store_id,
                interval_minutes, enabled, next_run_at, created_at, updated_at
            )
            select workspace_id, auth_id, 'tiktok-business', advertiser_id, store_id,
                   10, 1, date_add(utc_timestamp(6), interval 10 minute),
                   utc_timestamp(6), utc_timestamp(6)
            from ttb_binding_configs
            where advertiser_id is not null and advertiser_id <> ''
            """
        )
        op.execute(
            """
            insert ignore into gmvmax_guard_action_leases (
                lease_name, owner_token, fencing_token, acquired_at, expires_at, updated_at
            ) values (
                'gmvmax:guard-actions:cycle', null, 0, null, null, utc_timestamp(6)
            )
            """
        )


def downgrade() -> None:
    for table_name in (
        "gmvmax_manual_creative_uploads",
        "gmvmax_guard_action_leases",
        "gmvmax_campaign_manual_overrides",
        "gmvmax_sync_schedules",
        "gmvmax_task_ownership",
    ):
        if table_name in _table_names():
            op.drop_table(table_name)

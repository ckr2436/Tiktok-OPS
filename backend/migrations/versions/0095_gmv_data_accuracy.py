"""Add GMV fact provenance, settlement state, and scoped 10-minute identity.

Revision ID: 0095_gmv_data_accuracy
Revises: 0094_website_ads_learning_control
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0095_gmv_data_accuracy"
down_revision = "0094_website_ads_learning_control"
branch_labels = None
depends_on = None


CAMPAIGN_FACT_TABLES = (
    "gmvmax_product_campaign_metrics_daily",
    "gmvmax_product_campaign_metrics_hourly",
    "gmvmax_live_campaign_metrics_daily",
    "gmvmax_live_campaign_metrics_hourly",
)
PROVENANCE_TABLES = (
    *CAMPAIGN_FACT_TABLES,
    "gmvmax_product_creative_metrics_daily",
    "gmv_product_metrics_daily",
    "gmv_product_metrics_hourly",
    "gmv_overview_metrics_daily",
    "gmv_overview_metrics_hourly",
    "gmv_creative_metrics_10min",
)

PROVENANCE_COLUMNS = {
    "source_observed_at": sa.Column(
        "source_observed_at", mysql.DATETIME(fsp=6), nullable=True
    ),
    "ingested_at": sa.Column(
        "ingested_at", mysql.DATETIME(fsp=6), nullable=True
    ),
    "is_final": sa.Column(
        "is_final", sa.Boolean(), nullable=False, server_default=sa.text("0")
    ),
    "settled_at": sa.Column(
        "settled_at", mysql.DATETIME(fsp=6), nullable=True
    ),
}

TEN_MINUTE_UNIQUE_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "campaign_id",
    "item_group_id",
    "creative_id",
    "stat_time_day",
    "snapshot_at",
]
TEN_MINUTE_UNIQUE_NAME = "uk_creative_10min_scope_item"
TEN_MINUTE_QUARANTINE_TABLE = "gmv_creative_metrics_10min_quarantine"
TEN_MINUTE_REQUIRED_IDENTITY_COLUMNS = (
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "campaign_id",
    "item_group_id",
    "creative_id",
    "stat_time_day",
    "snapshot_at",
)


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _ordered_column_names(table_name: str) -> list[str]:
    return [
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    ]


def _add_missing_columns(table_name: str, columns: dict[str, sa.Column]) -> None:
    if table_name not in _table_names():
        return
    existing = _column_names(table_name)
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table_name, column.copy())


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    names = {
        str(index["name"])
        for index in inspector.get_indexes(table_name)
        if index.get("name")
    }
    names.update(
        str(item["name"])
        for item in inspector.get_unique_constraints(table_name)
        if item.get("name")
    )
    return names


def _backfill_ten_minute_item_groups() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    # Prefer the exact creative/day fact.  Only unambiguous one-product matches
    # are accepted; no tenant or item identity is guessed.
    op.execute(
        """
        update gmv_creative_metrics_10min t
        join (
            select workspace_id, auth_id, advertiser_id, store_id,
                   campaign_id, creative_id, stat_time_day,
                   min(item_group_id) as item_group_id
            from gmvmax_product_creative_metrics_daily
            group by workspace_id, auth_id, advertiser_id, store_id,
                     campaign_id, creative_id, stat_time_day
            having count(distinct item_group_id) = 1
        ) d
          on d.workspace_id=t.workspace_id
         and d.auth_id=t.auth_id
         and d.advertiser_id=t.advertiser_id
         and d.store_id=t.store_id
         and d.campaign_id=t.campaign_id
         and d.creative_id=t.creative_id
         and d.stat_time_day=t.stat_time_day
        set t.item_group_id=d.item_group_id
        where t.item_group_id is null
        """
    )
    # Status-only snapshots might not have a dated creative fact.  A campaign
    # mapping is safe only when exactly one product is mapped in that scope.
    op.execute(
        """
        update gmv_creative_metrics_10min t
        join (
            select workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                   min(item_group_id) as item_group_id
            from gmvmax_product_campaign_item_groups
            group by workspace_id, auth_id, advertiser_id, store_id, campaign_id
            having count(distinct item_group_id) = 1
        ) m
          on m.workspace_id=t.workspace_id
         and m.auth_id=t.auth_id
         and m.advertiser_id=t.advertiser_id
         and m.store_id=t.store_id
         and m.campaign_id=t.campaign_id
        set t.item_group_id=m.item_group_id
        where t.item_group_id is null
        """
    )


def _quarantine_unscoped_ten_minute_rows() -> None:
    """Move legacy rows without a complete identity out of the active fact."""

    table_name = "gmv_creative_metrics_10min"
    if table_name not in _table_names():
        return

    invalid_identity = """
        workspace_id is null
        or auth_id is null
        or advertiser_id is null or trim(advertiser_id) = ''
        or store_id is null or trim(store_id) = ''
        or campaign_id is null or trim(campaign_id) = ''
        or item_group_id is null or trim(item_group_id) = ''
        or creative_id is null or trim(creative_id) = ''
        or stat_time_day is null
        or snapshot_at is null
    """
    table_columns = _ordered_column_names(table_name)
    quoted_columns = ", ".join(f"`{name}`" for name in table_columns)
    selected_columns = ", ".join(f"t.`{name}`" for name in table_columns)

    op.execute(
        f"""
        create table if not exists {TEN_MINUTE_QUARANTINE_TABLE}
        as select * from {table_name} where 1=0
        """
    )
    quarantine_columns = _column_names(TEN_MINUTE_QUARANTINE_TABLE)
    # MySQL DDL is non-transactional. If a prior attempt stopped after the
    # quarantine table was created, make it match the active table before the
    # retry copies rows.
    for column in sa.inspect(op.get_bind()).get_columns(table_name):
        column_name = str(column["name"])
        if column_name in quarantine_columns:
            continue
        op.add_column(
            TEN_MINUTE_QUARANTINE_TABLE,
            sa.Column(
                column_name,
                column["type"],
                nullable=True,
            ),
        )
        quarantine_columns.add(column_name)
    if "quarantine_reason" not in quarantine_columns:
        op.add_column(
            TEN_MINUTE_QUARANTINE_TABLE,
            sa.Column("quarantine_reason", sa.String(length=64), nullable=True),
        )
    if "quarantined_at" not in quarantine_columns:
        op.add_column(
            TEN_MINUTE_QUARANTINE_TABLE,
            sa.Column(
                "quarantined_at",
                mysql.DATETIME(fsp=6),
                nullable=True,
            ),
        )

    timestamp_expression = (
        "CURRENT_TIMESTAMP(6)"
        if op.get_bind().dialect.name == "mysql"
        else "CURRENT_TIMESTAMP"
    )
    op.execute(
        f"""
        insert into {TEN_MINUTE_QUARANTINE_TABLE}
            ({quoted_columns}, quarantine_reason, quarantined_at)
        select {selected_columns}, 'INCOMPLETE_SCOPE', {timestamp_expression}
        from {table_name} t
        where ({invalid_identity})
          and not exists (
              select 1
              from {TEN_MINUTE_QUARANTINE_TABLE} q
              where q.id = t.id
          )
        """
    )
    op.execute(f"delete from {table_name} where {invalid_identity}")


def _require_ten_minute_identity() -> None:
    table_name = "gmv_creative_metrics_10min"
    if table_name not in _table_names():
        return
    columns = {
        str(column["name"]): column
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }
    alterations = [
        (column_name, columns[column_name]["type"])
        for column_name in TEN_MINUTE_REQUIRED_IDENTITY_COLUMNS
        if column_name in columns
        and columns[column_name].get("nullable", True)
    ]
    if not alterations:
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch_op:
            for column_name, existing_type in alterations:
                batch_op.alter_column(
                    column_name,
                    existing_type=existing_type,
                    nullable=False,
                )
        return
    if op.get_bind().dialect.name == "mysql":
        # A separate MODIFY rebuilds the table once per column.  Apply the
        # complete identity contract in one DDL so the production write pause
        # and metadata-lock window stay bounded.
        dialect = op.get_bind().dialect
        preparer = dialect.identifier_preparer
        clauses = [
            (
                f"MODIFY COLUMN {preparer.quote(column_name)} "
                f"{existing_type.compile(dialect=dialect)} NOT NULL"
            )
            for column_name, existing_type in alterations
        ]
        op.execute(
            "ALTER TABLE "
            f"{preparer.quote(table_name)} "
            + ", ".join(clauses)
            + ", ALGORITHM=COPY, LOCK=EXCLUSIVE"
        )
        return
    for column_name, existing_type in alterations:
        op.alter_column(
            table_name,
            column_name,
            existing_type=existing_type,
            nullable=False,
        )


def _replace_ten_minute_unique_key() -> None:
    table_name = "gmv_creative_metrics_10min"
    if table_name not in _table_names():
        return
    inspector = sa.inspect(op.get_bind())
    constraints_to_drop: list[str] = []
    target_unique_exists = False
    for constraint in inspector.get_unique_constraints(table_name):
        name = constraint.get("name")
        columns = [str(item) for item in constraint.get("column_names") or []]
        if not name:
            continue
        if name == TEN_MINUTE_UNIQUE_NAME and columns == TEN_MINUTE_UNIQUE_COLUMNS:
            target_unique_exists = True
            continue
        if {
            "campaign_id",
            "creative_id",
            "stat_time_day",
            "snapshot_at",
        }.issubset(columns):
            constraints_to_drop.append(str(name))

    create_unique = not target_unique_exists
    if op.get_bind().dialect.name == "sqlite":
        if constraints_to_drop or create_unique:
            with op.batch_alter_table(table_name) as batch_op:
                for name in constraints_to_drop:
                    batch_op.drop_constraint(name, type_="unique")
                if create_unique:
                    batch_op.create_unique_constraint(
                        TEN_MINUTE_UNIQUE_NAME,
                        TEN_MINUTE_UNIQUE_COLUMNS,
                    )
    else:
        # Build the full-scope key first.  Keeping a unique key in place while
        # the legacy global key is removed prevents a concurrent writer from
        # introducing duplicates during an online migration.
        if create_unique:
            op.create_unique_constraint(
                TEN_MINUTE_UNIQUE_NAME,
                table_name,
                TEN_MINUTE_UNIQUE_COLUMNS,
            )
        for name in constraints_to_drop:
            op.drop_constraint(name, table_name, type_="unique")
    # A partial run of an earlier draft may have created a non-unique index
    # identical to the unique key. Remove it to avoid duplicate write cost.
    duplicate_index = "idx_creative_10min_scope_item"
    if duplicate_index in _index_names(table_name):
        op.drop_index(duplicate_index, table_name=table_name)


def upgrade() -> None:
    for table_name in PROVENANCE_TABLES:
        _add_missing_columns(table_name, PROVENANCE_COLUMNS)

    if "gmv_creative_metrics_10min" in _table_names():
        _add_missing_columns(
            "gmv_creative_metrics_10min",
            {
                "item_group_id": sa.Column(
                    "item_group_id", sa.String(length=64), nullable=True
                )
            },
        )
        _backfill_ten_minute_item_groups()
        _quarantine_unscoped_ten_minute_rows()
        _replace_ten_minute_unique_key()
        _require_ten_minute_identity()

def downgrade() -> None:
    # Provenance and scoped identity prevent cross-tenant collisions.  Removing
    # them, or restoring the former global unique key, can destroy valid rows
    # created after this revision; this repair migration is intentionally
    # non-destructive on downgrade.
    pass

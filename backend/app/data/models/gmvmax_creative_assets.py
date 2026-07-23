"""Normalized GMV Max creative-to-product relationship tables."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, Index, String, Table, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME

from app.data.db import Base


UBigInt = BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


# This relation is intentionally exposed as a Core Table rather than an ORM
# entity: it has a six-column unique business key but no surrogate database
# primary key.  Registering it in Base.metadata keeps schema tooling and test
# databases aligned with the raw-SQL readers/writers without inventing a
# primary key that does not exist in production.
GmvmaxCreativeAssetProduct = Table(
    "gmvmax_creative_asset_products",
    Base.metadata,
    Column("workspace_id", UBigInt, nullable=False),
    Column("auth_id", UBigInt, nullable=False),
    Column("advertiser_id", String(64), nullable=False),
    Column("store_id", String(64), nullable=False),
    Column("item_id", String(64), nullable=False),
    Column("item_group_id", String(64), nullable=False),
    Column(
        "created_at",
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    ),
    Column(
        "updated_at",
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    ),
    UniqueConstraint(
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "item_id",
        "item_group_id",
        name="uq_gmvmax_creative_asset_product_scope",
    ),
    Index(
        "idx_gmvmax_creative_asset_product_lookup",
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "item_group_id",
        "item_id",
    ),
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_0900_ai_ci",
)


__all__ = ["GmvmaxCreativeAssetProduct"]

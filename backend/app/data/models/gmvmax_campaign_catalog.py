"""GMV Max campaign catalog and mapping tables.

These models capture static/semistatic campaign attributes as returned by
TikTok ``campaign/get`` and ``campaign/info``. Metrics belong in the fact tables
defined in ``gmvmax_campaign_metrics.py`` while manual snapshot caches live in
``gmvmax_campaign_snapshots.py``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Index, JSON, Numeric, String, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.data.db import Base


UBigInt = BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class GmvmaxCampaignCreateIntent(Base):
    """Durable idempotency record for a Product GMV Max create request."""

    __tablename__ = "gmvmax_campaign_create_intents"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "idempotency_key",
            name="uk_gmvmax_create_intent_idem",
        ),
        Index(
            "idx_gmvmax_create_intent_scope",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
        ),
        Index(
            "idx_gmvmax_create_intent_state",
            "workspace_id",
            "auth_id",
            "state",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)

    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    client_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    official_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_name: Mapped[str] = mapped_column(String(255), nullable=False)
    replacement_campaign_id: Mapped[str | None] = mapped_column(String(64))
    campaign_id: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="PREPARED",
        server_default=text("'PREPARED'"),
    )

    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict | None] = mapped_column(JSON)
    error_json: Mapped[dict | None] = mapped_column(JSON)

    submitted_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    remote_created_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    finalized_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvmaxProductCampaignCatalog(Base):
    """Catalog cache for PRODUCT GMV Max campaigns (campaign/get + campaign/info)."""

    __tablename__ = "gmvmax_product_campaign_catalog"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "campaign_id",
            name="uk_prod_campaign_catalog",
        ),
        Index("idx_prod_campaign_catalog_ws_adv", "workspace_id", "advertiser_id"),
        Index("idx_prod_campaign_catalog_store", "store_id"),
        Index("idx_prod_campaign_catalog_modify", "modify_time_utc"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)

    campaign_name: Mapped[str | None] = mapped_column(String(255))
    operation_status: Mapped[str | None] = mapped_column(String(32))
    secondary_status: Mapped[str | None] = mapped_column(String(128))
    objective_type: Mapped[str | None] = mapped_column(String(64))
    create_time_utc: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    modify_time_utc: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    list_raw_json: Mapped[dict | None] = mapped_column(JSON)

    store_id: Mapped[str | None] = mapped_column(String(64))
    shopping_ads_type: Mapped[str] = mapped_column(String(16), default="PRODUCT", nullable=False)
    product_specific_type: Mapped[str | None] = mapped_column(String(64))
    optimization_goal: Mapped[str | None] = mapped_column(String(64))
    deep_bid_type: Mapped[str | None] = mapped_column(String(64))
    roas_bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    budget_cents: Mapped[int | None] = mapped_column(BigInteger)
    schedule_type: Mapped[str | None] = mapped_column(String(64))
    schedule_start_time_utc: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    schedule_end_time_utc: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    detail_raw_json: Mapped[dict | None] = mapped_column(JSON)

    list_synced_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    detail_synced_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvmaxLiveCampaignCatalog(Base):
    """Catalog cache for LIVE GMV Max campaigns (campaign/get + campaign/info)."""

    __tablename__ = "gmvmax_live_campaign_catalog"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "campaign_id",
            name="uk_live_campaign_catalog",
        ),
        Index("idx_live_campaign_catalog_ws_adv", "workspace_id", "advertiser_id"),
        Index("idx_live_campaign_catalog_store", "store_id"),
        Index("idx_live_campaign_catalog_modify", "modify_time_utc"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)

    campaign_name: Mapped[str | None] = mapped_column(String(255))
    operation_status: Mapped[str | None] = mapped_column(String(32))
    secondary_status: Mapped[str | None] = mapped_column(String(128))
    objective_type: Mapped[str | None] = mapped_column(String(64))
    create_time_utc: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    modify_time_utc: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    list_raw_json: Mapped[dict | None] = mapped_column(JSON)

    store_id: Mapped[str | None] = mapped_column(String(64))
    shopping_ads_type: Mapped[str] = mapped_column(String(16), default="LIVE", nullable=False)
    optimization_goal: Mapped[str | None] = mapped_column(String(64))
    deep_bid_type: Mapped[str | None] = mapped_column(String(64))
    roas_bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    budget_cents: Mapped[int | None] = mapped_column(BigInteger)
    schedule_type: Mapped[str | None] = mapped_column(String(64))
    schedule_start_time_utc: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    schedule_end_time_utc: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    detail_raw_json: Mapped[dict | None] = mapped_column(JSON)

    list_synced_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))
    detail_synced_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6))

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvmaxProductCampaignItemGroup(Base):
    """Mapping table between PRODUCT campaigns and item groups (campaign/info detail)."""

    __tablename__ = "gmvmax_product_campaign_item_groups"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "item_group_id",
            name="uk_prod_campaign_item",
        ),
        Index("idx_prod_item_group", "item_group_id"),
        Index("idx_prod_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_group_id: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class GmvmaxLiveCampaignIdentity(Base):
    """Mapping table between LIVE campaigns and identities (campaign/info identity_list)."""

    __tablename__ = "gmvmax_live_campaign_identities"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "campaign_id",
            "identity_id",
            "identity_type",
            name="uk_live_campaign_identity",
        ),
        Index("idx_live_identity", "identity_id"),
        Index("idx_live_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    auth_id: Mapped[int] = mapped_column(UBigInt, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(64))
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_authorized_bc_id: Mapped[str | None] = mapped_column(String(64))
    identity_authorized_shop_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


__all__ = [
    "GmvmaxCampaignCreateIntent",
    "GmvmaxProductCampaignCatalog",
    "GmvmaxLiveCampaignCatalog",
    "GmvmaxProductCampaignItemGroup",
    "GmvmaxLiveCampaignIdentity",
]

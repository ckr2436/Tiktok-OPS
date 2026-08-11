"""remove residual commercial references from product fact envelopes

Revision ID: 0118_product_library_remove_commercial_references
Revises: 0117_product_library_attributes_only
Create Date: 2026-07-23
"""

from __future__ import annotations

import json
import re
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0118_product_library_remove_commercial_references"
down_revision = "0117_product_library_attributes_only"
branch_labels = None
depends_on = None


_COMMERCIAL_TEXT = re.compile(
    r"[$€£¥]\s*\d|\b(?:usd|cny|rmb)\s*\d|"
    r"(?:价格|售价|新客|立减|促销|折扣|优惠券|包邮|满减|限时|特价|"
    r"\bprice\b|\bnew\s+customer\b|\bdiscount\b|\bcoupon\b|\bpromot\w*\b|\bpricing\b|"
    r"\bfree\s+shipping\b|\blimited[-\s]?time\b|\bsale\s+price\b)",
    re.I,
)
_VOLATILE_KEYS = {
    "price",
    "pricing",
    "promotion",
    "promotions",
    "discount",
    "discounts",
    "coupon",
    "coupons",
    "offer",
    "offers",
    "current promotion",
    "current price",
    "价格",
    "促销",
    "折扣",
    "优惠",
    "优惠券",
}
_DROP = object()


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"[_\-]+", " ", str(key)).strip().lower()
            if normalized in _VOLATILE_KEYS:
                continue
            child = _clean(item)
            if child is not _DROP:
                cleaned[str(key)] = child
        return cleaned
    if isinstance(value, list):
        return [child for item in value if (child := _clean(item)) is not _DROP]
    if isinstance(value, str) and _COMMERCIAL_TEXT.search(value):
        return _DROP
    return value


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, facts_json FROM hermes_content_products")
    ).mappings()
    for row in rows:
        raw = row.get("facts_json")
        parsed: Any = raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
        cleaned = _clean(parsed)
        if cleaned is _DROP or cleaned == parsed:
            continue
        bind.execute(
            sa.text(
                "UPDATE hermes_content_products "
                "SET facts_json = :facts_json WHERE id = :product_id"
            ),
            {
                "product_id": int(row["id"]),
                "facts_json": json.dumps(
                    cleaned,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )


def downgrade() -> None:
    pass

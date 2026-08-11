"""keep the shared product library limited to durable product attributes

Revision ID: 0117_product_library_attributes_only
Revises: 0116_content_producer_long_text
Create Date: 2026-07-23
"""

from __future__ import annotations

import json
import re
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0117_product_library_attributes_only"
down_revision = "0116_content_producer_long_text"
branch_labels = None
depends_on = None


_BRIEF_VOLATILE = re.compile(
    r"[$€£¥]\s*\d|\b(?:usd|cny|rmb)\s*\d|"
    r"(?:新客|立减|促销|折扣|优惠券|包邮|满减|限时|特价|"
    r"\bnew\s+customer\b|\bdiscount\b|\bcoupon\b|\bpromo(?:tion)?\b|"
    r"\bfree\s+shipping\b|\blimited[-\s]?time\b|\bsale\s+price\b)|"
    r"(?:\d+\s*秒|\d+\s*:\s*\d+|竖屏|横屏|节奏|钩子|对白|口型|快切|镜头|"
    r"\btiktok\b|\binstagram\b|\byoutube\b|\bvoiceover\b|\baspect\s+ratio\b)",
    re.I,
)
_COMMERCIAL_TEXT = re.compile(
    r"[$€£¥]\s*\d|\b(?:usd|cny|rmb)\s*\d|"
    r"(?:新客|立减|促销|折扣|优惠券|包邮|满减|限时|特价|"
    r"\bnew\s+customer\b|\bdiscount\b|\bcoupon\b|\bpromo(?:tion)?\b|"
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
        sa.text(
            "SELECT id, product_brief, facts_json FROM hermes_content_products"
        )
    ).mappings()
    for row in rows:
        updates: dict[str, Any] = {}
        brief = str(row.get("product_brief") or "").strip()
        if brief and _BRIEF_VOLATILE.search(brief):
            updates["product_brief"] = None
        raw_facts = row.get("facts_json")
        parsed: Any = raw_facts
        if isinstance(raw_facts, str):
            try:
                parsed = json.loads(raw_facts)
            except json.JSONDecodeError:
                parsed = raw_facts
        cleaned = _clean(parsed)
        if cleaned is not _DROP and cleaned != parsed:
            updates["facts_json"] = json.dumps(
                cleaned,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if updates:
            assignments = []
            params = {"product_id": int(row["id"])}
            for key, value in updates.items():
                assignments.append(f"{key} = :{key}")
                params[key] = value
            bind.execute(
                sa.text(
                    "UPDATE hermes_content_products SET "
                    + ", ".join(assignments)
                    + " WHERE id = :product_id"
                ),
                params,
            )


def downgrade() -> None:
    # Purged campaign data intentionally cannot be reconstructed.
    pass

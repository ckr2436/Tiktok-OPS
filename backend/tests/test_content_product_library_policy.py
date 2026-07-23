from __future__ import annotations

import pytest

from app.core.errors import APIError
from app.services.hermes_agent.content_factory import (
    create_product,
    normalize_product_attribute_brief,
    sanitize_product_facts_json,
)


@pytest.mark.parametrize(
    "brief",
    [
        "促销新客立减$5。",
        "$14.99 shipped for a limited time.",
        "生成15秒 9:16 TikTok 快节奏视频。",
    ],
)
def test_product_library_rejects_commercial_and_project_brief_content(brief):
    with pytest.raises(APIError) as exc:
        normalize_product_attribute_brief(brief)
    assert exc.value.code == "CONTENT_PRODUCT_ATTRIBUTES_ONLY"


def test_product_library_keeps_only_stable_attribute_notes(db_session):
    product = create_product(
        db_session,
        workspace_id=7,
        user_id=19,
        brand_name="MYUPONA",
        product_name="Sleep Ease Gummies",
        market="US",
        product_brief=(
            "Blue bottle with a purple label. Blueberry flavor. "
            "Serving size is two gummies."
        ),
        facts_json={
            "identity": {"brand": "MYUPONA"},
            "price": "$7.99",
            "CURRENT PROMOTION": "new customer discount",
        },
    )

    assert "Blue bottle" in product.product_brief
    assert product.facts_json == {"identity": {"brand": "MYUPONA"}}


def test_product_fact_sanitizer_recurses_without_changing_attributes():
    assert sanitize_product_facts_json({
        "result": {
            "product_name": "Gummies",
            "offers": [{"price": "$5"}],
            "label": {"serving_size": "2 gummies"},
        }
    }) == {
        "result": {
            "product_name": "Gummies",
            "label": {"serving_size": "2 gummies"},
        }
    }

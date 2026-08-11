from app.gmvmax.creative_status import (
    OFFICIAL_CREATIVE_DELIVERY_STATUSES,
    canonicalize_creative_delivery_status,
)


def test_live_official_creative_delivery_statuses_are_preserved() -> None:
    for status in OFFICIAL_CREATIVE_DELIVERY_STATUSES:
        assert canonicalize_creative_delivery_status(status) == status


def test_legacy_not_delivering_aliases_use_official_api_spelling() -> None:
    assert canonicalize_creative_delivery_status("NOT_DELIVERING") == "NOT_DELIVERYING"
    assert canonicalize_creative_delivery_status("NOT_DELIVERED") == "NOT_DELIVERYING"


def test_future_official_status_is_not_collapsed_or_rejected() -> None:
    assert canonicalize_creative_delivery_status(" exploring ") == "EXPLORING"
    assert canonicalize_creative_delivery_status(None) is None

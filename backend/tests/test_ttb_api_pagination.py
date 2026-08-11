from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.services import ttb_api
from app.services.ttb_api import TTBApiClient, TTBPaginationError


_MISSING = object()


class StubTTBApiClient(TTBApiClient):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def _request_json(  # type: ignore[override]
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": dict(params or {}),
            }
        )
        if not self.responses:
            raise AssertionError("pagination requested an unexpected extra page")
        return self.responses.pop(0)


def _payload(
    items: list[dict[str, Any]],
    *,
    key: str = "list",
    page_info: Any = _MISSING,
) -> dict[str, Any]:
    data: dict[str, Any] = {key: items}
    if page_info is not _MISSING:
        data["page_info"] = page_info
    return {"code": 0, "message": "OK", "data": data}


async def _collect(iterator: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item async for item in iterator]


def test_numbered_pagination_obeys_totals_when_first_page_is_short() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"id": "one"}],
                page_info={
                    "page": 1,
                    "page_size": 50,
                    "total_number": 3,
                    "total_page": 2,
                },
            ),
            _payload(
                [{"id": "two"}, {"id": "three"}],
                page_info={
                    "page": 2,
                    "page_size": 50,
                    "total_number": 3,
                    "total_page": 2,
                },
            ),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_by_page(
                method="GET",
                path="/bc/get/",
                page_size=50,
                extractor="list",
            )
        )
    )

    assert [row["id"] for row in rows] == ["one", "two", "three"]
    assert [call["params"]["page"] for call in client.calls] == [1, 2]


def test_business_center_pagination_uses_nested_official_key() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"bc_info": {"bc_id": "bc-one", "name": "Primary"}}],
                page_info={
                    "page": 1,
                    "page_size": 50,
                    "total_number": 1,
                    "total_page": 1,
                },
            )
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_by_page(
                method="GET",
                path="/bc/get/",
                page_size=50,
                extractor="list",
            )
        )
    )

    assert rows == [{"bc_info": {"bc_id": "bc-one", "name": "Primary"}}]


def test_business_center_pagination_rejects_duplicate_nested_key() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [
                    {"bc_info": {"bc_id": "bc-one", "name": "Primary"}},
                    {"bc_info": {"bc_id": "bc-one", "name": "Duplicate"}},
                ],
                page_info={"page": 1, "total_number": 2, "total_page": 1},
            ),
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_by_page(
                    method="GET",
                    path="/bc/get/",
                    page_size=50,
                    extractor="list",
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_ITEM_DUPLICATE"


def test_httpx_request_log_redacts_tiktok_query_credentials() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s',
        args=("GET", "https://example.test/path?app_id=public&secret=do-not-log"),
        exc_info=None,
    )

    assert ttb_api._HttpxCredentialRedactionFilter().filter(record) is True
    rendered = record.getMessage()
    assert "do-not-log" not in rendered
    assert "secret=<redacted>" in rendered
    assert "app_id=public" in rendered


def test_numbered_pagination_item_key_rejects_partial_page_overlap() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"item_group_id": "one"}, {"item_group_id": "two"}],
                key="store_products",
                page_info={
                    "page": 1,
                    "page_size": 100,
                    "total_number": 3,
                    "total_page": 2,
                },
            ),
            _payload(
                [{"item_group_id": "two"}, {"item_group_id": "three"}],
                key="store_products",
                page_info={
                    "page": 2,
                    "page_size": 100,
                    "total_number": 3,
                    "total_page": 2,
                },
            ),
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_by_page(
                    method="GET",
                    path="/store/product/get/",
                    page_size=100,
                    extractor="products",
                    item_key=lambda item: item.get("item_group_id"),
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_ITEM_DUPLICATE"


def test_numbered_pagination_uses_endpoint_key_without_caller_opt_in() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"store_id": "one"}, {"store_id": "two"}],
                key="stores",
                page_info={
                    "page": 1,
                    "page_size": 50,
                    "total_number": 3,
                    "total_page": 2,
                },
            ),
            _payload(
                [{"store_id": "two"}, {"store_id": "three"}],
                key="stores",
                page_info={
                    "page": 2,
                    "page_size": 50,
                    "total_number": 3,
                    "total_page": 2,
                },
            ),
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_by_page(
                    method="GET",
                    path="/store/list/",
                    page_size=50,
                    extractor="stores",
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_ITEM_DUPLICATE"


def test_cursor_pagination_uses_endpoint_key_without_caller_opt_in() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"advertiser_id": "one"}, {"advertiser_id": "two"}],
                page_info={
                    "page": 1,
                    "total_number": 3,
                    "total_page": 2,
                    "cursor": "next",
                },
            ),
            _payload(
                [{"advertiser_id": "two"}, {"advertiser_id": "three"}],
                page_info={
                    "page": 2,
                    "total_number": 3,
                    "total_page": 2,
                    "cursor": "terminal",
                },
            ),
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_cursor(
                    method="GET",
                    path="/oauth2/advertiser/get/",
                    page_size=50,
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_ITEM_DUPLICATE"


def test_numbered_pagination_item_key_requires_a_stable_key() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"title": "missing product ID"}],
                key="store_products",
                page_info={
                    "page": 1,
                    "page_size": 100,
                    "total_number": 1,
                    "total_page": 1,
                },
            )
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_by_page(
                    method="GET",
                    path="/store/product/get/",
                    page_size=100,
                    extractor="products",
                    item_key=lambda item: item.get("item_group_id"),
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_ITEM_KEY_INVALID"


def test_numbered_pagination_total_number_uses_unique_item_count() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"item_group_id": "one"}],
                key="store_products",
                page_info={
                    "page": 1,
                    "page_size": 100,
                    "total_number": 2,
                    "total_page": 2,
                },
            ),
            _payload(
                [{"item_group_id": "two"}],
                key="store_products",
                page_info={
                    "page": 2,
                    "page_size": 100,
                    "total_number": 2,
                    "total_page": 2,
                },
            ),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_by_page(
                method="GET",
                path="/store/product/get/",
                page_size=100,
                extractor="products",
                item_key=lambda item: item.get("item_group_id"),
            )
        )
    )

    assert [row["item_group_id"] for row in rows] == ["one", "two"]


def test_numbered_pagination_obeys_total_number_without_total_page() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"store_id": "one"}],
                key="stores",
                page_info={"page": 1, "total_number": 2},
            ),
            _payload(
                [{"store_id": "two"}],
                key="stores",
                page_info={"page": 2, "total_number": 2},
            ),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_by_page(
                method="GET",
                path="/store/list/",
                page_size=50,
                extractor="stores",
            )
        )
    )

    assert [row["store_id"] for row in rows] == ["one", "two"]


def test_numbered_pagination_obeys_has_more_on_a_short_page() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"item_group_id": "one"}],
                key="store_products",
                page_info={"page": 1, "has_more": True},
            ),
            _payload(
                [{"item_group_id": "two"}],
                key="store_products",
                page_info={"page": 2, "has_more": False},
            ),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_by_page(
                method="GET",
                path="/store/product/get/",
                page_size=100,
                extractor="products",
            )
        )
    )

    assert [row["item_group_id"] for row in rows] == ["one", "two"]


def test_numbered_pagination_positive_signal_wins_over_stale_negative() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"id": "one"}],
                page_info={
                    "page": 1,
                    "total_page": 2,
                    "total_number": 2,
                    "has_more": False,
                },
            ),
            _payload(
                [{"id": "two"}],
                page_info={
                    "page": 2,
                    "total_page": 2,
                    "total_number": 2,
                    "has_more": False,
                },
            ),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_by_page(
                method="GET",
                path="/bc/get/",
                page_size=50,
                extractor="list",
            )
        )
    )

    assert [row["id"] for row in rows] == ["one", "two"]


def test_numbered_pagination_rejects_response_page_stall() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"id": "one"}],
                page_info={"page": 2, "total_page": 2},
            )
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_by_page(
                    method="GET",
                    path="/bc/get/",
                    page_size=50,
                    extractor="list",
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_PAGE_STALLED"


def test_numbered_pagination_rejects_empty_non_terminal_page() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [],
                page_info={"page": 1, "has_more": True},
            )
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_by_page(
                    method="GET",
                    path="/bc/get/",
                    page_size=50,
                    extractor="list",
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_METADATA_CONFLICT"


def test_numbered_pagination_rejects_repeated_legacy_page() -> None:
    client = StubTTBApiClient(
        [
            _payload([{"id": "same"}]),
            _payload([{"id": "same"}]),
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_by_page(
                    method="GET",
                    path="/bc/get/",
                    page_size=1,
                    extractor="list",
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_PAGE_STALLED"


def test_numbered_pagination_probes_after_metadata_free_short_page() -> None:
    client = StubTTBApiClient(
        [
            _payload([{"id": "one"}, {"id": "two"}]),
            _payload([{"id": "three"}]),
            _payload([]),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_by_page(
                method="GET",
                path="/bc/get/",
                page_size=2,
                extractor="list",
            )
        )
    )

    assert [row["id"] for row in rows] == ["one", "two", "three"]
    assert len(client.calls) == 3


def test_numbered_pagination_does_not_stop_on_first_metadata_free_short_page() -> None:
    client = StubTTBApiClient(
        [
            _payload([{"id": "one"}]),
            _payload([{"id": "two"}]),
            _payload([]),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_by_page(
                method="GET",
                path="/bc/get/",
                page_size=50,
                extractor="list",
            )
        )
    )

    assert [row["id"] for row in rows] == ["one", "two"]
    assert [call["params"]["page"] for call in client.calls] == [1, 2, 3]


def test_store_list_without_page_info_is_one_official_snapshot() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [
                    {"store_id": "one"},
                    {"store_id": "two"},
                    {"store_id": "three"},
                ],
                key="stores",
            ),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_by_page(
                method="GET",
                path="/store/list/",
                page_size=2,
                extractor="stores",
            )
        )
    )

    assert len(rows) == 3
    assert len(client.calls) == 1


def test_numbered_pagination_raises_when_safety_limit_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ttb_api, "_MAX_PAGINATION_PAGES", 2)
    client = StubTTBApiClient(
        [
            _payload([{"id": "one"}]),
            _payload([{"id": "two"}]),
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_by_page(
                    method="GET",
                    path="/bc/get/",
                    page_size=50,
                    extractor="list",
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_LIMIT_EXCEEDED"


def test_store_product_pagination_emits_official_page_size_of_one_hundred() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"item_group_id": "one"}],
                key="store_products",
            ),
            _payload([], key="store_products"),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_by_page(
                method="GET",
                path="/store/product/get/",
                page_size=100,
                extractor="products",
            )
        )
    )

    assert rows == [{"item_group_id": "one"}]
    assert client.calls[0]["params"]["page_size"] == 100


def test_cursor_pagination_obeys_totals_and_short_pages() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"advertiser_id": "one"}],
                page_info={
                    "page": 1,
                    "total_number": 2,
                    "total_page": 2,
                    "has_more": True,
                    "cursor": "next",
                },
            ),
            _payload(
                [{"advertiser_id": "two"}],
                page_info={
                    "page": 2,
                    "total_number": 2,
                    "total_page": 2,
                    "has_more": False,
                    "cursor": "terminal",
                },
            ),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_cursor(
                method="GET",
                path="/oauth2/advertiser/get/",
                page_size=50,
            )
        )
    )

    assert [row["advertiser_id"] for row in rows] == ["one", "two"]
    assert "cursor" not in client.calls[0]["params"]
    assert client.calls[1]["params"]["cursor"] == "next"


def test_cursor_pagination_treats_zero_as_a_valid_cursor() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"advertiser_id": "one"}],
                page_info={"has_more": True, "cursor": 0},
            ),
            _payload(
                [{"advertiser_id": "two"}],
                page_info={"has_more": False, "cursor": 1},
            ),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_cursor(
                method="GET",
                path="/oauth2/advertiser/get/",
                page_size=50,
            )
        )
    )

    assert len(rows) == 2
    assert client.calls[1]["params"]["cursor"] == "0"


def test_cursor_pagination_rejects_a_non_advancing_cursor() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"advertiser_id": "one"}],
                page_info={"has_more": True, "cursor": "same"},
            ),
            _payload(
                [{"advertiser_id": "two"}],
                page_info={"has_more": True, "cursor": "same"},
            ),
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_cursor(
                    method="GET",
                    path="/oauth2/advertiser/get/",
                    page_size=50,
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_CURSOR_STALLED"


def test_cursor_pagination_positive_flag_wins_over_stale_negative() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"advertiser_id": "one"}],
                page_info={
                    "has_more": True,
                    "has_next": False,
                    "cursor": "next",
                },
            ),
            _payload(
                [{"advertiser_id": "two"}],
                page_info={
                    "has_more": False,
                    "has_next": False,
                    "cursor": "terminal",
                },
            ),
        ]
    )

    rows = asyncio.run(
        _collect(
            client._paged_cursor(
                method="GET",
                path="/oauth2/advertiser/get/",
                page_size=50,
            )
        )
    )

    assert [row["advertiser_id"] for row in rows] == ["one", "two"]


def test_cursor_pagination_rejects_missing_cursor_when_metadata_requires_more() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [{"advertiser_id": "one"}],
                page_info={"total_number": 2},
            )
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_cursor(
                    method="GET",
                    path="/oauth2/advertiser/get/",
                    page_size=50,
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_CURSOR_MISSING"


def test_cursor_pagination_rejects_empty_non_terminal_page() -> None:
    client = StubTTBApiClient(
        [
            _payload(
                [],
                page_info={"has_more": True, "cursor": "next"},
            )
        ]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_cursor(
                    method="GET",
                    path="/oauth2/advertiser/get/",
                    page_size=50,
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_METADATA_CONFLICT"


def test_cursor_pagination_preserves_unpaginated_short_page_fallback() -> None:
    client = StubTTBApiClient(
        [_payload([{"advertiser_id": "one"}])]
    )

    rows = asyncio.run(
        _collect(
            client._paged_cursor(
                method="GET",
                path="/oauth2/advertiser/get/",
                page_size=50,
            )
        )
    )

    assert rows == [{"advertiser_id": "one"}]
    assert len(client.calls) == 1


def test_cursor_pagination_rejects_ambiguous_full_page_without_cursor() -> None:
    client = StubTTBApiClient(
        [_payload([{"advertiser_id": "one"}])]
    )

    with pytest.raises(TTBPaginationError) as exc_info:
        asyncio.run(
            _collect(
                client._paged_cursor(
                    method="GET",
                    path="/oauth2/advertiser/get/",
                    page_size=1,
                )
            )
        )

    assert exc_info.value.code == "PAGINATION_CURSOR_MISSING"

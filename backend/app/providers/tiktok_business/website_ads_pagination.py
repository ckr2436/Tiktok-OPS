"""Strict numbered pagination for TikTok Website Ads endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Any


DEFAULT_WEBSITE_ADS_MAX_PAGES = 1000


class WebsiteAdsPaginationError(RuntimeError):
    """Base error for a Website Ads result that cannot be proven complete."""


class WebsiteAdsPaginationLimitError(WebsiteAdsPaginationError):
    """Raised instead of returning a result truncated by a safety cap."""


class WebsiteAdsPaginationStalledError(WebsiteAdsPaginationError):
    """Raised when TikTok returns the wrong page or repeats page content."""


class WebsiteAdsPaginationInvariantError(WebsiteAdsPaginationError):
    """Raised when official pagination metadata contradicts the response."""


@dataclass(frozen=True)
class WebsiteAdsPage:
    page: int
    response: dict[str, Any]
    data: dict[str, Any]
    rows: list[Any]
    page_info: dict[str, Any]
    has_more: bool


def report_payload_has_complete_pagination(payload: Mapping[str, Any] | None) -> bool:
    """Return true only for a report assembled from every numbered page/chunk."""

    if not isinstance(payload, Mapping):
        return False
    data = payload.get("data")
    rows = data.get("list") if isinstance(data, Mapping) else None
    pagination = payload.get("_report_pagination")
    if not isinstance(rows, list) or not isinstance(pagination, Mapping):
        return False
    try:
        pages_fetched = int(pagination.get("pages_fetched") or 0)
        chunks_fetched = int(pagination.get("chunks_fetched") or 0)
        rows_returned = int(pagination.get("rows_returned"))
    except (TypeError, ValueError):
        return False
    source_pages = pagination.get("source_pages")
    return bool(
        pages_fetched > 0
        and chunks_fetched > 0
        and rows_returned == len(rows)
        and isinstance(source_pages, list)
        and len(source_pages) == pages_fetched
        and all(isinstance(page_info, Mapping) for page_info in source_pages)
    )


def _parse_int(
    value: Any,
    *,
    field: str,
    endpoint: str,
    minimum: int,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise WebsiteAdsPaginationInvariantError(
            f"{endpoint} returned boolean {field}={value!r}"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WebsiteAdsPaginationInvariantError(
            f"{endpoint} returned invalid {field}={value!r}"
        ) from exc
    if parsed < minimum:
        raise WebsiteAdsPaginationInvariantError(
            f"{endpoint} returned invalid {field}={parsed}; minimum is {minimum}"
        )
    return parsed


def _parse_bool(value: Any, *, field: str, endpoint: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise WebsiteAdsPaginationInvariantError(
        f"{endpoint} returned invalid {field}={value!r}"
    )


def _page_info(data: Mapping[str, Any], *, endpoint: str) -> dict[str, Any]:
    raw = data.get("page_info")
    if raw is None:
        if any(
            key in data
            for key in (
                "page",
                "page_size",
                "total_page",
                "total_number",
                "has_more",
                "has_next",
            )
        ):
            return dict(data)
        return {}
    if not isinstance(raw, Mapping):
        raise WebsiteAdsPaginationInvariantError(
            f"{endpoint} returned non-object data.page_info"
        )
    return dict(raw)


def _page_signature(rows: Sequence[Any]) -> str:
    serialized = json.dumps(
        list(rows),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _item_key_signature(
    row: Any,
    *,
    item_key: Callable[[Any], Any],
    endpoint: str,
    page: int,
    index: int,
) -> str:
    try:
        value = item_key(row)
    except Exception as exc:  # noqa: BLE001 - normalize endpoint extractors
        raise WebsiteAdsPaginationInvariantError(
            f"{endpoint} could not extract a stable item key "
            f"on page {page} at index {index}"
        ) from exc
    if value is None or (isinstance(value, str) and not value.strip()):
        raise WebsiteAdsPaginationInvariantError(
            f"{endpoint} returned a row without a stable item key "
            f"on page {page} at index {index}"
        )
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _continuation(
    data: Mapping[str, Any],
    *,
    page_info: Mapping[str, Any],
    endpoint: str,
    requested_page: int,
    requested_page_size: int,
    items_seen: int,
    row_count: int,
    trust_terminal_signals: bool,
) -> bool:
    response_page = _parse_int(
        page_info.get("page"),
        field="page",
        endpoint=endpoint,
        minimum=1,
    )
    if response_page is not None and response_page != requested_page:
        raise WebsiteAdsPaginationStalledError(
            f"{endpoint} returned page {response_page} for requested page {requested_page}"
        )
    response_page_size = _parse_int(
        page_info.get("page_size"),
        field="page_size",
        endpoint=endpoint,
        minimum=1,
    )
    if response_page_size is not None and row_count > response_page_size:
        raise WebsiteAdsPaginationInvariantError(
            f"{endpoint} returned {row_count} rows with page_size={response_page_size}"
        )
    if response_page_size is not None and response_page_size > requested_page_size:
        raise WebsiteAdsPaginationInvariantError(
            f"{endpoint} returned page_size={response_page_size} after requesting "
            f"{requested_page_size}"
        )

    signals: list[bool] = []
    total_page = _parse_int(
        page_info.get("total_page"),
        field="total_page",
        endpoint=endpoint,
        minimum=0,
    )
    if total_page is not None:
        if total_page == 0:
            if row_count and trust_terminal_signals:
                raise WebsiteAdsPaginationInvariantError(
                    f"{endpoint} returned rows with total_page=0 on page {requested_page}"
                )
            signals.append(False)
        elif requested_page > total_page:
            if trust_terminal_signals:
                raise WebsiteAdsPaginationInvariantError(
                    f"{endpoint} returned total_page={total_page} on page {requested_page}"
                )
            signals.append(False)
        else:
            signals.append(requested_page < total_page)

    total_number = _parse_int(
        page_info.get("total_number"),
        field="total_number",
        endpoint=endpoint,
        minimum=0,
    )
    if total_number is not None:
        if items_seen > total_number and trust_terminal_signals:
            raise WebsiteAdsPaginationInvariantError(
                f"{endpoint} returned {items_seen} rows but total_number={total_number}"
            )
        signals.append(items_seen < total_number)

    for container in (page_info, data):
        for field in ("has_more", "has_next"):
            if field not in container:
                continue
            signal = _parse_bool(container.get(field), field=field, endpoint=endpoint)
            if signal is not None:
                signals.append(signal)

    # TikTok endpoints occasionally return a stale terminal field alongside a
    # positive continuation field. A positive signal must always win.
    if any(signals):
        if row_count == 0:
            raise WebsiteAdsPaginationInvariantError(
                f"{endpoint} returned an empty non-terminal page {requested_page}"
            )
        return True
    if row_count == 0:
        return False
    if signals and trust_terminal_signals:
        return False

    # Missing metadata is not proof of completion. Probe once more and require
    # a real empty page; repeated-content and max-page guards make this finite.
    return True


async def collect_all_numbered_pages(
    fetch_page: Callable[[int], Awaitable[dict[str, Any]]],
    *,
    endpoint: str,
    list_key: str,
    requested_page_size: int,
    item_key: Callable[[Any], Any] | None = None,
    max_pages: int = DEFAULT_WEBSITE_ADS_MAX_PAGES,
    trust_terminal_signals: bool = True,
) -> list[WebsiteAdsPage]:
    """Fetch all pages or raise when completeness cannot be established."""

    if requested_page_size < 1:
        raise ValueError("requested_page_size must be positive")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")

    pages: list[WebsiteAdsPage] = []
    signatures: set[str] = set()
    seen_item_keys: set[str] = set()
    items_seen = 0
    expected_total_number: int | None = None
    for requested_page in range(1, max_pages + 1):
        response = await fetch_page(requested_page)
        if not isinstance(response, Mapping):
            raise WebsiteAdsPaginationInvariantError(
                f"{endpoint} returned a non-object response on page {requested_page}"
            )
        raw_data = response.get("data")
        if not isinstance(raw_data, Mapping):
            raise WebsiteAdsPaginationInvariantError(
                f"{endpoint} returned non-object data on page {requested_page}"
            )
        data = dict(raw_data)
        raw_rows = data.get(list_key)
        if raw_rows is None:
            rows: list[Any] = []
        elif isinstance(raw_rows, list):
            rows = list(raw_rows)
        else:
            raise WebsiteAdsPaginationInvariantError(
                f"{endpoint} returned non-list data.{list_key} on page {requested_page}"
            )

        current_page_info = _page_info(data, endpoint=endpoint)
        if item_key is None:
            items_seen += len(rows)
        else:
            page_item_keys: set[str] = set()
            for index, row in enumerate(rows):
                signature = _item_key_signature(
                    row,
                    item_key=item_key,
                    endpoint=endpoint,
                    page=requested_page,
                    index=index,
                )
                if signature in page_item_keys or signature in seen_item_keys:
                    raise WebsiteAdsPaginationStalledError(
                        f"{endpoint} repeated a stable item key "
                        f"on page {requested_page} at index {index}"
                    )
                page_item_keys.add(signature)
            seen_item_keys.update(page_item_keys)
            items_seen = len(seen_item_keys)

        declared_total_number = _parse_int(
            current_page_info.get("total_number"),
            field="total_number",
            endpoint=endpoint,
            minimum=0,
        )
        if trust_terminal_signals and declared_total_number is not None:
            if expected_total_number is None:
                expected_total_number = declared_total_number
            elif declared_total_number != expected_total_number:
                raise WebsiteAdsPaginationInvariantError(
                    f"{endpoint} changed total_number across pages "
                    f"({expected_total_number} -> {declared_total_number})"
                )

        has_more = _continuation(
            data,
            page_info=current_page_info,
            endpoint=endpoint,
            requested_page=requested_page,
            requested_page_size=requested_page_size,
            items_seen=items_seen,
            row_count=len(rows),
            trust_terminal_signals=trust_terminal_signals,
        )
        if expected_total_number is not None:
            if items_seen > expected_total_number:
                raise WebsiteAdsPaginationInvariantError(
                    f"{endpoint} returned {items_seen} unique rows but "
                    f"total_number={expected_total_number}"
                )
            if items_seen < expected_total_number:
                if not rows:
                    raise WebsiteAdsPaginationInvariantError(
                        f"{endpoint} terminated with {items_seen} unique rows but "
                        f"total_number={expected_total_number}"
                    )
                has_more = True

        if rows:
            signature = _page_signature(rows)
            if signature in signatures:
                raise WebsiteAdsPaginationStalledError(
                    f"{endpoint} repeated page content on page {requested_page}"
                )
            signatures.add(signature)

        pages.append(
            WebsiteAdsPage(
                page=requested_page,
                response=dict(response),
                data=data,
                rows=rows,
                page_info=current_page_info,
                has_more=has_more,
            )
        )
        if not has_more:
            if (
                expected_total_number is not None
                and items_seen != expected_total_number
            ):
                raise WebsiteAdsPaginationInvariantError(
                    f"{endpoint} unique row count {items_seen} does not match "
                    f"total_number={expected_total_number}"
                )
            return pages

    raise WebsiteAdsPaginationLimitError(
        f"{endpoint} exceeded the configured pagination limit of {max_pages} pages"
    )


def merge_numbered_pages(
    pages: Sequence[WebsiteAdsPage],
    *,
    list_key: str,
) -> dict[str, Any]:
    """Merge complete pages into the same envelope shape as a single page."""

    if not pages:
        return {
            "data": {
                list_key: [],
                "page_info": {
                    "page": 1,
                    "page_size": 0,
                    "total_number": 0,
                    "total_page": 1,
                },
            },
            "_website_ads_pagination": {
                "pages_fetched": 0,
                "rows_returned": 0,
                "source_pages": [],
            },
        }

    merged = dict(pages[0].response)
    merged_data = dict(pages[0].data)
    rows = [row for page in pages for row in page.rows]
    merged_data[list_key] = rows
    merged_data["page_info"] = {
        "page": 1,
        "page_size": len(rows),
        "total_number": len(rows),
        "total_page": 1,
    }
    merged["data"] = merged_data
    merged["_website_ads_pagination"] = {
        "pages_fetched": len(pages),
        "rows_returned": len(rows),
        "source_pages": [dict(page.page_info) for page in pages],
    }
    return merged

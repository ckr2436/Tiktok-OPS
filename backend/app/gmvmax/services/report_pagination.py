"""Pagination rules shared by numbered GMV Max list/report consumers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import json
from math import ceil
from typing import Any, Generic, Mapping, Sequence, TypeVar


# Official report/get accepts up to 1000 rows. Some responses are still
# observed to clamp pages to 50, so completeness must never be inferred from
# the requested size (see ``report_page_has_more`` below).
OFFICIAL_REPORT_PAGE_SIZE = 1000
REPORT_FILTER_ID_LIMIT = 100
DEFAULT_NUMBERED_PAGE_LIMIT = 200

ResponseT = TypeVar("ResponseT")


class NumberedPaginationError(RuntimeError):
    """Base class for pagination failures that must not be hidden."""


class NumberedPaginationLimitError(NumberedPaginationError):
    """Raised instead of silently returning a truncated official result set."""


class NumberedPaginationStalledError(NumberedPaginationError):
    """Raised when an endpoint repeats a page while claiming more data exists."""


class NumberedPaginationInvariantError(NumberedPaginationError):
    """Raised when rows and official pagination metadata contradict each other."""


@dataclass(frozen=True)
class NumberedPage(Generic[ResponseT]):
    """One fetched page together with its normalized rows and continuation state."""

    page: int
    response: ResponseT
    data: Any
    rows: list[Any]
    has_more: bool


@dataclass
class ReportPaginationState:
    """Cross-page integrity state for raw report pagination loops."""

    page_signatures: set[str] = field(default_factory=set)
    dimension_signatures: set[str] = field(default_factory=set)
    items_seen: int = 0
    require_dimensions: bool = False

    def validate(self, *, page: int, rows: Sequence[Any]) -> None:
        if not rows:
            return

        page_signature = _row_signature(rows)
        if page_signature in self.page_signatures:
            raise NumberedPaginationStalledError(
                "official GMV Max report pagination repeated a page "
                f"(page {page})"
            )

        page_dimension_signatures: set[str] = set()
        for row in rows:
            dimensions = _field(row, "dimensions")
            if dimensions is None:
                if self.require_dimensions:
                    raise NumberedPaginationInvariantError(
                        "official GMV Max report returned a row without "
                        f"dimensions on page {page}"
                    )
                continue
            if hasattr(dimensions, "model_dump"):
                dimensions = dimensions.model_dump(mode="json", exclude_none=False)
            if not isinstance(dimensions, Mapping):
                raise NumberedPaginationInvariantError(
                    "official GMV Max report returned non-object dimensions "
                    f"on page {page}"
                )
            if self.require_dimensions and not dimensions:
                raise NumberedPaginationInvariantError(
                    "official GMV Max report returned empty dimensions "
                    f"on page {page}"
                )
            signature = hashlib.sha256(
                json.dumps(
                    dict(dimensions),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if (
                signature in self.dimension_signatures
                or signature in page_dimension_signatures
            ):
                raise NumberedPaginationStalledError(
                    "official GMV Max report pagination repeated a dimension "
                    f"key on page {page}"
                )
            page_dimension_signatures.add(signature)

        self.page_signatures.add(page_signature)
        self.dimension_signatures.update(page_dimension_signatures)
        self.items_seen += len(rows)


def chunk_report_filter_ids(values: Sequence[str]) -> list[list[str]]:
    """Split official campaign/item report filters at their 100-ID limit."""

    normalized = list(dict.fromkeys(str(value) for value in values if value))
    return [
        normalized[offset : offset + REPORT_FILTER_ID_LIMIT]
        for offset in range(0, len(normalized), REPORT_FILTER_ID_LIMIT)
    ]


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _optional_bool(value: Any) -> bool | None:
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
    raise NumberedPaginationInvariantError(
        f"official GMV Max pagination returned an invalid boolean value {value!r}"
    )


def _optional_int(value: Any, *, field: str, minimum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise NumberedPaginationInvariantError(
            f"official GMV Max pagination returned boolean {field}={value!r}"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise NumberedPaginationInvariantError(
            f"official GMV Max pagination returned invalid {field}={value!r}"
        ) from exc
    if parsed < minimum:
        raise NumberedPaginationInvariantError(
            "official GMV Max pagination returned invalid "
            f"{field}={parsed}; minimum is {minimum}"
        )
    return parsed


def _numbered_page_info(data: Any) -> Any:
    page_info = _field(data, "page_info")
    if page_info is not None and not isinstance(page_info, Mapping) and not hasattr(
        page_info, "__dict__"
    ):
        raise NumberedPaginationInvariantError(
            "official GMV Max pagination returned non-object page_info"
        )
    if page_info is None and any(
        _field(data, field) is not None
        for field in (
            "page",
            "has_more",
            "has_next",
            "total_page",
            "total_number",
            "page_size",
        )
    ):
        page_info = data
    return page_info


def _validate_response_page(data: Any, *, current_page: int) -> None:
    page_info = _numbered_page_info(data)
    response_page = _field(page_info, "page") if page_info is not None else None
    parsed_response_page = _optional_int(response_page, field="page", minimum=1)
    if parsed_response_page is None:
        return
    if parsed_response_page != current_page:
        raise NumberedPaginationStalledError(
            "official GMV Max pagination returned an unexpected page "
            f"(requested page {current_page}, response page {response_page})"
        )


def _explicit_numbered_continuation(
    data: Any,
    *,
    current_page: int,
    rows: Sequence[Any],
    requested_page_size: int | None,
    items_seen: int | None,
) -> bool | None:
    page_info = _numbered_page_info(data)
    if page_info is None:
        return None

    signals: list[bool] = []
    row_count = len(rows)
    response_page_size = _optional_int(
        _field(page_info, "page_size"),
        field="page_size",
        minimum=1,
    )
    if response_page_size is not None:
        if row_count > response_page_size:
            raise NumberedPaginationInvariantError(
                "official GMV Max pagination returned "
                f"{row_count} rows with page_size={response_page_size}"
            )
        if (
            requested_page_size is not None
            and response_page_size > requested_page_size
        ):
            raise NumberedPaginationInvariantError(
                "official GMV Max pagination returned "
                f"page_size={response_page_size} after requesting "
                f"{requested_page_size}"
            )

    total_page = _optional_int(
        _field(page_info, "total_page"),
        field="total_page",
        minimum=0,
    )
    if total_page is not None:
        if total_page == 0:
            if row_count:
                raise NumberedPaginationInvariantError(
                    "official GMV Max pagination returned rows with "
                    f"total_page=0 on page {current_page}"
                )
            signals.append(False)
        elif current_page > total_page:
            raise NumberedPaginationInvariantError(
                "official GMV Max pagination advanced beyond total_page "
                f"(page {current_page}, total_page {total_page})"
            )
        else:
            signals.append(current_page < total_page)

    total_number = _optional_int(
        _field(page_info, "total_number"),
        field="total_number",
        minimum=0,
    )
    if total_number is not None:
        observed_count = items_seen if items_seen is not None else row_count
        if observed_count > total_number:
            raise NumberedPaginationInvariantError(
                "official GMV Max pagination returned more rows than "
                f"total_number ({observed_count} > {total_number})"
            )
        # A bare total_number has proven stale on report responses. It is
        # useful for rejecting impossible counts, but is terminal only when
        # the response also declares its effective page size. Otherwise the
        # report path keeps probing until an empty page.
        if response_page_size is not None:
            if items_seen is not None:
                signals.append(items_seen < total_number)
            else:
                signals.append(
                    current_page < ceil(total_number / response_page_size)
                )

    for field_name in ("has_more", "has_next"):
        signal = _optional_bool(_field(page_info, field_name))
        if signal is not None:
            signals.append(signal)

    if any(signals):
        return True
    if signals:
        return False
    return None


def numbered_page_has_more(
    data: Any,
    *,
    current_page: int,
    rows: Sequence[Any],
    requested_page_size: int | None = None,
    probe_on_missing_metadata: bool = False,
    items_seen: int | None = None,
) -> bool:
    """Return whether another numbered page must be fetched.

    TikTok list endpoints do not use one consistent continuation field.  Some
    return ``has_more``, some only ``total_page`` or
    ``total_number/page_size``.  A positive continuation signal wins over a
    contradictory terminal signal.

    ``probe_on_missing_metadata`` is intentionally opt-in.  Report endpoints
    need an empty-page probe because their metadata has occasionally been
    omitted.  Unpaginated endpoints such as the official session list must
    stop after their single response instead of repeatedly fetching page one.
    """

    if current_page < 1:
        raise ValueError("current_page must be positive")
    if requested_page_size is not None and requested_page_size < 1:
        raise ValueError("requested_page_size must be positive")
    if items_seen is not None and items_seen < len(rows):
        raise ValueError("items_seen cannot be smaller than the current page")

    _validate_response_page(data, current_page=current_page)
    explicit = _explicit_numbered_continuation(
        data,
        current_page=current_page,
        rows=rows,
        requested_page_size=requested_page_size,
        items_seen=items_seen,
    )
    if not rows:
        if explicit is True:
            raise NumberedPaginationInvariantError(
                "official GMV Max pagination returned an empty page while "
                f"metadata requires a continuation (page {current_page})"
            )
        return False
    if explicit is not None:
        return explicit
    return bool(rows) if probe_on_missing_metadata else False


def _row_signature(rows: Sequence[Any]) -> str:
    normalized: list[Any] = []
    for row in rows:
        if hasattr(row, "model_dump"):
            normalized.append(row.model_dump(mode="json", exclude_none=False))
        elif isinstance(row, Mapping):
            normalized.append(dict(row))
        else:
            normalized.append(repr(row))
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _item_key_signature(value: Any, *, page: int) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise NumberedPaginationInvariantError(
            f"official pagination returned a row without a stable item key on page {page}"
        )
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError) as exc:
        raise NumberedPaginationInvariantError(
            f"official pagination returned an invalid stable item key on page {page}"
        ) from exc
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def iter_numbered_pages(
    fetch_page: Callable[[int], Awaitable[ResponseT]],
    *,
    rows_from_data: Callable[[Any], Sequence[Any] | None],
    data_from_response: Callable[[ResponseT], Any] | None = None,
    item_key: Callable[[Any], Any] | None = None,
    start_page: int = 1,
    requested_page_size: int | None = None,
    max_pages: int = DEFAULT_NUMBERED_PAGE_LIMIT,
    probe_on_missing_metadata: bool = False,
) -> AsyncIterator[NumberedPage[ResponseT]]:
    """Fetch a complete numbered result set without hiding a safety-cap cut-off."""

    if start_page < 1:
        raise ValueError("start_page must be positive")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")

    extract_data = data_from_response or (lambda response: _field(response, "data"))
    seen_signatures: set[str] = set()
    seen_item_keys: set[str] = set()
    items_seen = 0
    expected_total_number: int | None = None
    for page in range(start_page, start_page + max_pages):
        response = await fetch_page(page)
        data = extract_data(response)
        rows = list(rows_from_data(data) or [])

        if item_key is None:
            items_seen += len(rows)
        else:
            page_item_keys: set[str] = set()
            for row in rows:
                signature = _item_key_signature(item_key(row), page=page)
                if signature in page_item_keys or signature in seen_item_keys:
                    raise NumberedPaginationStalledError(
                        "official pagination repeated a stable item key "
                        f"on page {page}"
                    )
                page_item_keys.add(signature)
            seen_item_keys.update(page_item_keys)
            items_seen = len(seen_item_keys)

        page_info = _numbered_page_info(data)
        declared_total = _optional_int(
            _field(page_info, "total_number") if page_info is not None else None,
            field="total_number",
            minimum=0,
        )
        if declared_total is not None:
            if expected_total_number is None:
                expected_total_number = declared_total
            elif declared_total != expected_total_number:
                raise NumberedPaginationInvariantError(
                    "official pagination changed total_number across pages "
                    f"({expected_total_number} -> {declared_total})"
                )

        has_more = numbered_page_has_more(
            data,
            current_page=page,
            rows=rows,
            requested_page_size=requested_page_size,
            probe_on_missing_metadata=probe_on_missing_metadata,
            items_seen=items_seen,
        )
        if expected_total_number is not None:
            if items_seen > expected_total_number:
                raise NumberedPaginationInvariantError(
                    "official pagination returned more unique items than "
                    f"total_number ({items_seen} > {expected_total_number})"
                )
            if items_seen < expected_total_number:
                if not rows:
                    raise NumberedPaginationInvariantError(
                        "official pagination terminated before every unique item "
                        f"was returned ({items_seen} < {expected_total_number})"
                    )
                has_more = True

        signature = _row_signature(rows) if rows else None
        if signature is not None and signature in seen_signatures:
            raise NumberedPaginationStalledError(
                "official GMV Max pagination repeated a page "
                f"(page {page})"
            )
        if signature is not None:
            seen_signatures.add(signature)
        yield NumberedPage(
            page=page,
            response=response,
            data=data,
            rows=rows,
            has_more=has_more,
        )
        if not has_more:
            if (
                expected_total_number is not None
                and items_seen != expected_total_number
            ):
                raise NumberedPaginationInvariantError(
                    "official pagination unique item count does not match "
                    f"total_number ({items_seen} != {expected_total_number})"
                )
            return

    raise NumberedPaginationLimitError(
        "official GMV Max pagination exceeded the configured safety limit "
        f"({max_pages} pages starting at page {start_page})"
    )


def report_page_has_more(
    data: Any,
    *,
    current_page: int,
    rows: Sequence[Any],
    state: ReportPaginationState | None = None,
) -> bool:
    """Backward-compatible report continuation rule.

    Report consumers deliberately probe until an empty page when TikTok omits
    pagination metadata, preventing absence reconciliation from using a
    truncated key set.
    """

    if state is not None:
        state.validate(page=current_page, rows=rows)
    return numbered_page_has_more(
        data,
        current_page=current_page,
        rows=rows,
        requested_page_size=OFFICIAL_REPORT_PAGE_SIZE,
        probe_on_missing_metadata=True,
        items_seen=state.items_seen if state is not None else None,
    )


__all__ = [
    "OFFICIAL_REPORT_PAGE_SIZE",
    "REPORT_FILTER_ID_LIMIT",
    "DEFAULT_NUMBERED_PAGE_LIMIT",
    "NumberedPage",
    "NumberedPaginationError",
    "NumberedPaginationLimitError",
    "NumberedPaginationStalledError",
    "NumberedPaginationInvariantError",
    "ReportPaginationState",
    "chunk_report_filter_ids",
    "iter_numbered_pages",
    "numbered_page_has_more",
    "report_page_has_more",
]

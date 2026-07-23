"""Safe reconciliation for facts omitted by a complete official report.

TikTok report/get omits dimensions whose metrics have fallen back to no data.
An upsert-only sync therefore leaves stale, non-final facts behind forever.
This module stages the complete set of keys returned for one exact API window
and removes only non-final local rows that are absent from that set.

The staged object is deliberately fail closed: callers must explicitly mark
pagination complete, and any unparseable/out-of-scope response row invalidates
the reconciliation without preventing ordinary upserts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Collection, Mapping, Sequence

from sqlalchemy import delete, or_, select, tuple_
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session


_DELETE_CHUNK_SIZE = 1000
_REQUIRED_SCOPE_COLUMNS = {
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
}


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class StagedFactKeySet:
    """Keys observed for one fully scoped, half-open report window."""

    model: Any
    time_column: str
    range_start: Any
    range_end_exclusive: Any
    key_columns: Sequence[str]
    scope_equals: Mapping[str, Any]
    scope_in: Mapping[str, Collection[Any]] = field(default_factory=dict)
    _seen_keys: set[tuple[Any, ...]] = field(default_factory=set, init=False)
    _pagination_complete: bool = field(default=False, init=False)
    _safe: bool = field(default=True, init=False)
    _reconciliation_started_at: datetime = field(
        default_factory=_utc_now_naive,
        init=False,
    )

    def __post_init__(self) -> None:
        self.key_columns = tuple(self.key_columns)
        self.scope_equals = dict(self.scope_equals)
        self.scope_in = {
            column_name: tuple(dict.fromkeys(values))
            for column_name, values in self.scope_in.items()
        }
        model_columns = set(self.model.__table__.columns.keys())
        required = {
            *self.scope_equals.keys(),
            *self.scope_in.keys(),
            self.time_column,
            *self.key_columns,
            "is_final",
            "source_observed_at",
        }
        missing = required - model_columns
        if missing:
            raise ValueError(
                f"{self.model.__name__} is missing reconciliation columns: "
                f"{sorted(missing)}"
            )
        missing_scope = _REQUIRED_SCOPE_COLUMNS - set(self.scope_equals)
        if missing_scope:
            raise ValueError(
                "fact reconciliation requires exact tenant/account/store scope: "
                f"{sorted(missing_scope)}"
            )
        if self.range_start >= self.range_end_exclusive:
            raise ValueError("reconciliation range must be a non-empty half-open window")
        if not self.key_columns:
            raise ValueError("at least one fact key column is required")
        for column_name, values in self.scope_in.items():
            if not values:
                raise ValueError(f"scope_in[{column_name!r}] must not be empty")
        for column_name, value in self.scope_equals.items():
            if value is None or (
                isinstance(value, str) and not value.strip()
            ):
                raise ValueError(f"scope_equals[{column_name!r}] must be exact")

    @property
    def can_reconcile(self) -> bool:
        return self._pagination_complete and self._safe

    @property
    def seen_keys(self) -> frozenset[tuple[Any, ...]]:
        return frozenset(self._seen_keys)

    @property
    def reconciliation_started_at(self) -> datetime:
        """UTC fence separating pre-existing facts from newer sync writes."""

        return self._reconciliation_started_at

    def add(self, *key_values: Any) -> None:
        if len(key_values) != len(self.key_columns):
            self.invalidate()
            return
        if any(value is None or (isinstance(value, str) and not value.strip()) for value in key_values):
            self.invalidate()
            return
        self._seen_keys.add(tuple(key_values))

    def contains_time(self, value: Any) -> bool:
        return self.range_start <= value < self.range_end_exclusive

    def invalidate(self) -> None:
        """Prevent absence deletion when a response row cannot be scoped safely."""

        self._safe = False

    def mark_pagination_complete(self) -> None:
        self._pagination_complete = True

    def reconcile(self, session: Session) -> int:
        """Delete stale non-final rows only after a complete, safe page walk."""

        if not self.can_reconcile:
            return 0

        mapper = inspect(self.model)
        primary_keys = list(mapper.primary_key)
        if len(primary_keys) != 1:
            raise ValueError("fact reconciliation requires a single-column primary key")
        primary_key = primary_keys[0]

        conditions = [
            getattr(self.model, column_name) == value
            for column_name, value in self.scope_equals.items()
        ]
        conditions.extend(
            getattr(self.model, column_name).in_(tuple(values))
            for column_name, values in self.scope_in.items()
        )
        time_attr = getattr(self.model, self.time_column)
        source_observed_at = self.model.source_observed_at
        conditions.extend(
            [
                time_attr >= self.range_start,
                time_attr < self.range_end_exclusive,
                self.model.is_final.is_(False),
                # A sync that started after this official snapshot must win,
                # even if it writes before this older cycle reaches DELETE.
                # Legacy rows without provenance remain eligible for the
                # first complete reconciliation.
                or_(
                    source_observed_at.is_(None),
                    source_observed_at <= self._reconciliation_started_at,
                ),
            ]
        )

        key_attrs = [getattr(self.model, name) for name in self.key_columns]
        candidates = session.execute(
            select(primary_key, source_observed_at, *key_attrs)
            .where(*conditions)
            .with_for_update()
        ).all()
        absent_rows = [
            (row[0], row[1])
            for row in candidates
            if tuple(row[2:]) not in self._seen_keys
        ]

        deleted_rows = 0
        for offset in range(0, len(absent_rows), _DELETE_CHUNK_SIZE):
            chunk = absent_rows[offset : offset + _DELETE_CHUNK_SIZE]
            null_source_ids = [
                row_id for row_id, observed_at in chunk if observed_at is None
            ]
            versioned_rows = [
                (row_id, observed_at)
                for row_id, observed_at in chunk
                if observed_at is not None
            ]
            version_conditions = []
            if null_source_ids:
                version_conditions.append(
                    (
                        primary_key.in_(null_source_ids)
                        & source_observed_at.is_(None)
                    )
                )
            if versioned_rows:
                version_conditions.append(
                    tuple_(primary_key, source_observed_at).in_(versioned_rows)
                )
            if not version_conditions:
                continue
            result = session.execute(
                delete(self.model).where(
                    *conditions,
                    or_(*version_conditions),
                )
            )
            if result.rowcount and result.rowcount > 0:
                deleted_rows += int(result.rowcount)
        session.flush()
        return deleted_rows


__all__ = ["StagedFactKeySet"]

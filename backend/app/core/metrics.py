"""Lightweight wrappers for optional Prometheus metrics."""
from __future__ import annotations

"""Thin wrappers providing optional Prometheus metrics primitives."""

from typing import Dict, Iterable, Tuple

try:  # pragma: no cover - optional dependency
    from prometheus_client import Counter as _PromCounter  # type: ignore
    from prometheus_client import Histogram as _PromHistogram  # type: ignore
except Exception:  # pragma: no cover - fallback when prometheus_client unavailable
    _PromCounter = None  # type: ignore
    _PromHistogram = None  # type: ignore


class _NoopMetric:
    def labels(self, *args: object, **kwargs: object) -> "_NoopMetric":  # pragma: no cover - trivial
        return self

    def inc(self, amount: float = 1.0) -> None:  # pragma: no cover - trivial
        return None

    def observe(self, value: float) -> None:  # pragma: no cover - trivial
        return None


_counters: Dict[Tuple[str, Tuple[str, ...]], object] = {}
_histograms: Dict[Tuple[str, Tuple[str, ...]], object] = {}


def get_counter(name: str, documentation: str, *, labelnames: Iterable[str] = ()) -> object:
    key = (name, tuple(labelnames))
    if key in _counters:
        return _counters[key]

    if _PromCounter is None:
        metric = _NoopMetric()
    else:  # pragma: no branch
        metric = _PromCounter(name, documentation, labelnames=list(labelnames))
    _counters[key] = metric
    return metric


def get_histogram(name: str, documentation: str, *, labelnames: Iterable[str] = (), buckets: Iterable[float] | None = None) -> object:
    key = (name, tuple(labelnames))
    if key in _histograms:
        return _histograms[key]

    if _PromHistogram is None:
        metric = _NoopMetric()
    else:  # pragma: no branch
        if buckets is not None:
            metric = _PromHistogram(name, documentation, labelnames=list(labelnames), buckets=list(buckets))
        else:
            metric = _PromHistogram(name, documentation, labelnames=list(labelnames))
    _histograms[key] = metric
    return metric

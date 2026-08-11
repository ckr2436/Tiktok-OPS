from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.data.models.hermes_agent import HermesContentSeriesSlate
from app.services.hermes_agent.content_director import (
    DirectorSeriesBrief,
)
from app.services.hermes_agent.content_series_runtime import (
    SeriesSlateLoopResult,
)


def _canonical_sha256(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def persist_approved_series_slate(
    db,
    *,
    project,
    brief: DirectorSeriesBrief,
    result: SeriesSlateLoopResult,
) -> HermesContentSeriesSlate:
    """Persist one immutable approved slate; caller owns the transaction."""
    if result.status != "approved" or result.final_slate is None:
        raise ValueError(
            "only an independently approved series slate may be persisted"
        )
    slate = result.final_slate
    brief_json = brief.model_dump(mode="json")
    slate_json = slate.model_dump(mode="json")
    identity = {
        "project_id": int(project.id),
        "series_id": brief.series_id,
        "series_version": int(brief.series_version),
    }
    existing = db.scalar(
        select(HermesContentSeriesSlate).where(
            HermesContentSeriesSlate.project_id == identity["project_id"],
            HermesContentSeriesSlate.series_id == identity["series_id"],
            HermesContentSeriesSlate.series_version
            == identity["series_version"],
        )
    )
    expected = {
        "brief_sha256": _canonical_sha256(brief_json),
        "slate_sha256": slate.slate_sha256,
    }
    if existing is not None:
        if (
            existing.brief_sha256 != expected["brief_sha256"]
            or existing.slate_sha256 != expected["slate_sha256"]
            or dict(existing.brief_json or {}) != brief_json
            or dict(existing.slate_json or {}) != slate_json
        ):
            raise ValueError(
                "series slate identity conflicts with an immutable project "
                "version"
            )
        return existing

    row = HermesContentSeriesSlate(
        project_id=identity["project_id"],
        workspace_id=int(project.workspace_id),
        user_id=(
            int(project.user_id)
            if project.user_id is not None
            else None
        ),
        series_id=identity["series_id"],
        series_version=identity["series_version"],
        status="approved",
        brief_sha256=expected["brief_sha256"],
        slate_sha256=expected["slate_sha256"],
        brief_json=brief_json,
        slate_json=slate_json,
        attempts_json=[
            item.model_dump(mode="json")
            for item in result.attempts
        ],
        reviews_json=[
            item.model_dump(mode="json")
            for item in result.reviews
        ],
        reason=result.reason,
    )
    db.add(row)
    db.flush()
    return row


__all__ = ["persist_approved_series_slate"]

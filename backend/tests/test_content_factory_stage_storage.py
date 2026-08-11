import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.dialects import mysql

from app.data.models.hermes_agent import HermesContentFactoryStage
from app.tasks.hermes_agent.content_factory_tasks import (
    _persist_completed_stage_capture,
)


def test_content_factory_stage_response_uses_mysql_mediumtext() -> None:
    column_type = HermesContentFactoryStage.__table__.c.response_text.type

    assert isinstance(column_type.dialect_impl(mysql.dialect()), mysql.MEDIUMTEXT)


def test_completed_stage_capture_preserves_entire_model_output() -> None:
    large_evidence = "e" * 240_000
    raw = json.dumps({
        "schema_version": "1.0",
        "project_id": "cf_complete",
        "stage": "DIRECTOR",
        "status": "PASS",
        "result": {"large_evidence": large_evidence},
        "evidence": [],
        "issues": [],
        "next_stage": "PRODUCTION_PLAN",
    })
    db = MagicMock()
    row = SimpleNamespace(
        response_text=None,
        chat_url=None,
        output_json={},
    )

    stored = _persist_completed_stage_capture(
        db,
        row,
        project_key="cf_complete",
        stage="DIRECTOR",
        response_text=raw,
        chat_url="https://example.invalid/complete",
    )

    assert stored == raw
    assert row.response_text == raw
    assert json.loads(row.response_text)["result"]["large_evidence"] == large_evidence
    assert row.output_json["durable_response_capture"]["validated_envelope"] is True
    db.commit.assert_called_once_with()

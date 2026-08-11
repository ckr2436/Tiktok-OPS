from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.hermes_agent import service


class _FakeDb:
    def get(self, *_args, **_kwargs):
        return None

    def flush(self) -> None:
        return None


@pytest.mark.anyio
async def test_generic_hermes_run_preserves_complete_model_output(monkeypatch):
    complete = "A" * 240_000 + "<END_OF_ACTUAL_OUTPUT>"
    run = SimpleNamespace(
        id=7,
        run_id="run-complete-output",
        workspace_id=3,
        user_id=9,
        conversation_id=None,
        task_type="content_analysis",
        status="pending",
        input_text="analyze",
        instructions="return the complete result",
        hermes_conversation=None,
    )
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        service.repository,
        "get_run",
        lambda *_args, **_kwargs: run,
    )
    monkeypatch.setattr(
        service.repository,
        "mark_run_processing",
        lambda _db, value: setattr(value, "status", "processing"),
    )

    def _mark_success(_db, value, *, result_text, **_kwargs):
        captured["result_text"] = result_text
        value.status = "success"

    monkeypatch.setattr(service.repository, "mark_run_success", _mark_success)
    monkeypatch.setattr(service, "log_event", lambda *_args, **_kwargs: None)

    async def _create_response(_self, **_kwargs):
        return {
            "id": "resp-complete-output",
            "output_text": complete,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }, 5

    monkeypatch.setattr(
        service.HermesAgentClient,
        "create_response",
        _create_response,
    )

    result = await service.execute_run(
        _FakeDb(),
        workspace_id=3,
        run_id=run.run_id,
    )

    assert result.status == "success"
    assert captured["result_text"] == complete
    assert captured["result_text"].endswith("<END_OF_ACTUAL_OUTPUT>")
    assert "TRUNCATED_BY_GMV_OPS" not in captured["result_text"]

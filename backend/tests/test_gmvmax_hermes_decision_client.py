from __future__ import annotations

import asyncio

from app.services import gmvmax_hermes_decision as decision_service


def test_daily_report_decision_uses_review_agent(monkeypatch):
    calls: list[dict] = []

    class FakeReviewClient:
        async def create_response(self, **kwargs):
            calls.append(kwargs)
            return {"output_text": '{"decision":"REJECT"}'}, 12

    monkeypatch.setattr(decision_service, "HermesAdsReviewClient", FakeReviewClient)

    response, output_text = asyncio.run(
        decision_service._call_gpt(
            {"report": {"id": 10}},
            instructions="review",
            source="gmvmax_hermes_plan_decision",
        )
    )

    assert response["output_text"] == '{"decision":"REJECT"}'
    assert output_text == '{"decision":"REJECT"}'
    assert calls == [
        {
            "input_text": '{"report":{"id":10}}',
            "instructions": "review",
            "metadata": {
                "source": "gmvmax_hermes_plan_decision",
                "prompt_version": "gmvmax_plan_decision_v2",
            },
        }
    ]

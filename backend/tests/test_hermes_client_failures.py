from app.services.hermes_agent.client import hermes_response_failure


def test_legacy_completed_quota_envelope_is_not_treated_as_director_copy():
    failure = hermes_response_failure({
        "status": "completed",
        "output": [{
            "type": "message",
            "content": [{
                "type": "output_text",
                "text": "Error code: 403 - insufficient_user_quota",
            }],
        }],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    })
    assert failure is not None
    assert failure[0] == "HERMES_UPSTREAM_QUOTA"


def test_normal_copy_that_mentions_quota_remains_valid_content():
    assert hermes_response_failure({
        "status": "completed",
        "output_text": '{"line":"A quota is not a creative strategy."}',
        "usage": {"total_tokens": 12},
    }) is None


def test_structured_failed_envelope_is_classified():
    failure = hermes_response_failure({
        "status": "failed",
        "error": {"message": "All eligible AI routes failed"},
        "usage": {"total_tokens": 0},
    })
    assert failure is not None
    assert failure[0] == "HERMES_UPSTREAM_EXECUTION_FAILED"


def test_structured_policy_rejection_is_not_reported_as_a_connection_failure():
    failure = hermes_response_failure({
        "status": "failed",
        "error": {
            "type": "POLICY",
            "message": "Provider explicitly rejected the prompt under its content policy",
        },
        "usage": {"total_tokens": 0},
    })
    assert failure is not None
    assert failure[0] == "HERMES_PROMPT_POLICY_VIOLATION"

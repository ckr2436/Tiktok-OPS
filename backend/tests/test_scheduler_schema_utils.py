from app.core.errors import APIError
from app.services.scheduler_schema_utils import validate_params_or_raise


def test_validate_params_success():
    schema = {
        "type": "object",
        "required": ["foo", "bar"],
        "properties": {
            "foo": {"type": "string", "minLength": 1},
            "bar": {"type": "integer", "minimum": 1},
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        },
    }
    params = {"foo": "x", "bar": 2, "items": ["a", "b"]}

    # Should not raise
    validate_params_or_raise(schema, params)


def test_validate_params_failure():
    schema = {
        "type": "object",
        "required": ["foo"],
        "properties": {
            "foo": {"type": "string", "pattern": "^x+$"},
            "bar": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    }
    params = {"foo": "abc", "bar": 0, "extra": True}

    try:
        validate_params_or_raise(schema, params)
    except APIError as exc:
        assert exc.code == "PARAMS_INVALID"
        assert "foo" in exc.message
        assert "bar" in exc.message
        assert "extra" in exc.message
    else:
        raise AssertionError("validation should have failed")

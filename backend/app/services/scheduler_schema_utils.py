"""JSON-Schema style parameter validation helpers for scheduler inputs."""
from __future__ import annotations

from typing import Any, List
import re

from app.core.errors import APIError

_JSON_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _type_matches(pyval: Any, schema_type: Any) -> bool:
    """Support string or list-based `type` definitions; treat bool separately from numbers."""

    def _single(tname: str) -> bool:
        pytype = _JSON_TYPE_MAP.get(tname)
        if pytype is None:
            return True  # unknown types are not strictly validated
        if tname in ("integer", "number") and isinstance(pyval, bool):
            return False
        return isinstance(pyval, pytype)

    if isinstance(schema_type, list):
        return any(_single(t) for t in schema_type)
    if isinstance(schema_type, str):
        return _single(schema_type)
    return True


def _validate_number_constraints(val: float, schema: dict, path: str, errors: List[str]):
    if "minimum" in schema and val < schema["minimum"]:
        errors.append(f"{path}: must be >= {schema['minimum']}")
    if "maximum" in schema and val > schema["maximum"]:
        errors.append(f"{path}: must be <= {schema['maximum']}")
    if "exclusiveMinimum" in schema and val <= schema["exclusiveMinimum"]:
        errors.append(f"{path}: must be > {schema['exclusiveMinimum']}")
    if "exclusiveMaximum" in schema and val >= schema["exclusiveMaximum"]:
        errors.append(f"{path}: must be < {schema['exclusiveMaximum']}")


def _validate_string_constraints(val: str, schema: dict, path: str, errors: List[str]):
    if "minLength" in schema and len(val) < schema["minLength"]:
        errors.append(f"{path}: length < {schema['minLength']}")
    if "maxLength" in schema and len(val) > schema["maxLength"]:
        errors.append(f"{path}: length > {schema['maxLength']}")
    if "pattern" in schema:
        try:
            if not re.fullmatch(schema["pattern"], val):
                errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
        except re.error:
            # ignore invalid regex patterns
            pass


def _validate_array(val: list, schema: dict, path: str, errors: List[str]):
    if "minItems" in schema and len(val) < schema["minItems"]:
        errors.append(f"{path}: items < {schema['minItems']}")
    if "maxItems" in schema and len(val) > schema["maxItems"]:
        errors.append(f"{path}: items > {schema['maxItems']}")
    items_schema = schema.get("items")
    if isinstance(items_schema, dict):
        for i, it in enumerate(val):
            _validate(items_schema, it, f"{path}[{i}]", errors)


def _validate_object(val: dict, schema: dict, path: str, errors: List[str]):
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    addl = schema.get("additionalProperties", True)

    for k in required:
        if k not in val:
            errors.append(f"{path}.{k}: is required")

    for k, v in val.items():
        if k in props:
            _validate(props[k], v, f"{path}.{k}", errors)
        else:
            if addl is False:
                errors.append(f"{path}.{k}: additional property not allowed")


def _validate(schema: dict, value: Any, path: str, errors: List[str]):
    if not isinstance(schema, dict) or not schema:
        return

    stype = schema.get("type")
    if stype is not None and not _type_matches(value, stype):
        errors.append(f"{path}: type mismatch (expected {stype})")
        return

    if "enum" in schema:
        enum_vals = schema["enum"]
        if value not in enum_vals:
            errors.append(f"{path}: must be one of {enum_vals!r}")
            return

    if stype in ("number", "integer") or (stype is None and isinstance(value, (int, float))):
        if isinstance(value, bool):
            errors.append(f"{path}: type mismatch (bool is not {stype or 'number'})")
            return
        _validate_number_constraints(float(value), schema, path, errors)

    if stype == "string" or (stype is None and isinstance(value, str)):
        _validate_string_constraints(str(value), schema, path, errors)

    if stype == "array" or (stype is None and isinstance(value, list)):
        if not isinstance(value, list):
            errors.append(f"{path}: type mismatch (expected array)")
        else:
            _validate_array(value, schema, path, errors)

    if stype == "object" or (stype is None and isinstance(value, dict)):
        if not isinstance(value, dict):
            errors.append(f"{path}: type mismatch (expected object)")
        else:
            _validate_object(value, schema, path, errors)


def validate_params_or_raise(input_schema_json: dict, params_json: dict) -> None:
    """Validate task params against a subset of JSON-Schema semantics.

    This helper mirrors the lightweight validation originally embedded in the
    scheduler catalog and raises ``APIError`` with code ``PARAMS_INVALID`` on
    failure.
    """
    if not input_schema_json:
        return
    if not isinstance(params_json, dict):
        raise APIError("PARAMS_INVALID", "params_json must be an object", 400)

    errors: List[str] = []
    _validate(input_schema_json, params_json, "$", errors)

    if errors:
        msg = "; ".join(errors[:10])
        if len(errors) > 10:
            msg += f" (and {len(errors)-10} more)"
        raise APIError("PARAMS_INVALID", msg, 400)

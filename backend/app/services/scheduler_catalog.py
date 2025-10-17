# app/services/scheduler_catalog.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Any
import re

from app.core.errors import APIError


@dataclass(frozen=True)
class PeriodicTaskSpec:
    name: str
    task: str
    crontab: Optional[str] = None   # e.g. "*/15 * * * *"
    interval_seconds: Optional[int] = None
    args: List = None
    kwargs: Dict = None
    queue: Optional[str] = None
    description: Optional[str] = None


# 建议的绑定维度周期任务（示例；真正启用由你方 ops 决定）
CATALOG: List[PeriodicTaskSpec] = [
    PeriodicTaskSpec(
        name="ttb:products:incremental",
        task="tenant.ttb.sync.products",
        crontab="*/30 * * * *",  # 每 30 分钟
        args=[],
        kwargs={"mode": "incremental", "limit": 500},
        queue="gmv.tasks.events",
        description="Incremental products sync per binding",
    ),
    PeriodicTaskSpec(
        name="ttb:shops:incremental",
        task="tenant.ttb.sync.shops",
        crontab="0 * * * *",  # 每小时
        args=[],
        kwargs={"mode": "incremental", "limit": 200},
        queue="gmv.tasks.events",
        description="Incremental shops sync per binding",
    ),
    PeriodicTaskSpec(
        name="ttb:advertisers:incremental",
        task="tenant.ttb.sync.advertisers",
        crontab="0 */6 * * *",  # 每 6 小时
        args=[],
        kwargs={"mode": "incremental", "limit": 200},
        queue="gmv.tasks.events",
        description="Incremental advertisers sync per binding",
    ),
    PeriodicTaskSpec(
        name="ttb:bc:incremental",
        task="tenant.ttb.sync.bc",
        crontab="0 3 * * *",  # 每天 03:00
        args=[],
        kwargs={"mode": "incremental", "limit": 200},
        queue="gmv.tasks.events",
        description="Incremental business centers sync per binding",
    ),
]


# -----------------------------
# JSON-Schema(精简版) 参数校验
# -----------------------------

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
    """支持 type 为字符串或字符串数组；number 不把 bool 当作 number；integer 不把 bool 当作 int。"""
    def _single(tname: str) -> bool:
        pytype = _JSON_TYPE_MAP.get(tname)
        if pytype is None:
            return True  # 未知类型不强校
        # 避免 bool 被当作 int/number
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
            # 正则有误时，忽略 pattern
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

    # required
    for k in required:
        if k not in val:
            errors.append(f"{path}.{k}: is required")

    # known properties
    for k, v in val.items():
        if k in props:
            _validate(props[k], v, f"{path}.{k}", errors)
        else:
            if addl is False:
                errors.append(f"{path}.{k}: additional property not allowed")

def _validate(schema: dict, value: Any, path: str, errors: List[str]):
    if not isinstance(schema, dict) or not schema:
        # 无 schema 不强校
        return

    # type 校验
    stype = schema.get("type")
    if stype is not None and not _type_matches(value, stype):
        errors.append(f"{path}: type mismatch (expected {stype})")
        return  # 类型不匹配就不再往下递归

    # enum
    if "enum" in schema:
        enum_vals = schema["enum"]
        if value not in enum_vals:
            errors.append(f"{path}: must be one of {enum_vals!r}")
            return

    # 分类型细化校验
    # number / integer
    if stype in ("number", "integer") or (stype is None and isinstance(value, (int, float))):
        # 避免 bool 被当作 number
        if isinstance(value, bool):
            errors.append(f"{path}: type mismatch (bool is not {stype or 'number'})")
            return
        _validate_number_constraints(float(value), schema, path, errors)

    # string
    if stype == "string" or (stype is None and isinstance(value, str)):
        _validate_string_constraints(str(value), schema, path, errors)

    # array
    if stype == "array" or (stype is None and isinstance(value, list)):
        if not isinstance(value, list):
            errors.append(f"{path}: type mismatch (expected array)")
        else:
            _validate_array(value, schema, path, errors)

    # object
    if stype == "object" or (stype is None and isinstance(value, dict)):
        if not isinstance(value, dict):
            errors.append(f"{path}: type mismatch (expected object)")
        else:
            _validate_object(value, schema, path, errors)


def validate_params_or_raise(input_schema_json: dict, params_json: dict) -> None:
    """
    对照 TaskCatalog.input_schema_json 校验 params_json。
    - 支持 JSON-Schema 常用子集：type/required/properties/additionalProperties/enum/
      minimum/maximum/exclusiveMinimum/exclusiveMaximum/minLength/maxLength/pattern/
      minItems/maxItems/items
    - 不依赖第三方库；若 schema 为空/无效则直接通过。
    - 失败抛 APIError("PARAMS_INVALID", msg, 400)
    """
    if not input_schema_json:
        return
    if not isinstance(params_json, dict):
        raise APIError("PARAMS_INVALID", "params_json must be an object", 400)

    errors: List[str] = []
    _validate(input_schema_json, params_json, "$", errors)

    if errors:
        # 拼接首 10 条，避免返回过长
        msg = "; ".join(errors[:10])
        if len(errors) > 10:
            msg += f" (and {len(errors)-10} more)"
        raise APIError("PARAMS_INVALID", msg, 400)


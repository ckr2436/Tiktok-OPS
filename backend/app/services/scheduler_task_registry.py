from __future__ import annotations

from typing import Any, Mapping

# Static registry of supported scheduled tasks.
SCHEDULED_TASKS: dict[tuple[str, str], dict[str, Any]] = {
    ("GMVMAX", "gmvmax.strategy"): {
        "kind": "gmvmax_strategy",
        "input_schema": {},
    },
    ("TTB_BASE", "ttb.sync.meta"): {
        "kind": "celery_task",
        "celery_task": "ttb.sync.meta",
        "queue": "gmv.tasks.events",
        "default_scope": "meta",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["incremental", "full"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                "scope": {"type": "string"},
                "workspace_id": {"type": "integer", "minimum": 1},
                "auth_id": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": True,
        },
    },
    ("TTB_BASE", "ttb.sync.bc"): {
        "kind": "celery_task",
        "celery_task": "ttb.sync.bc",
        "queue": "gmv.tasks.events",
        "default_scope": "bc",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["incremental", "full"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                "scope": {"type": "string"},
                "workspace_id": {"type": "integer", "minimum": 1},
                "auth_id": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": True,
        },
    },
    ("TTB_BASE", "ttb.sync.advertisers"): {
        "kind": "celery_task",
        "celery_task": "ttb.sync.advertisers",
        "queue": "gmv.tasks.events",
        "default_scope": "advertisers",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["incremental", "full"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                "scope": {"type": "string"},
                "workspace_id": {"type": "integer", "minimum": 1},
                "auth_id": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": True,
        },
    },
    ("TTB_BASE", "ttb.sync.stores"): {
        "kind": "celery_task",
        "celery_task": "ttb.sync.stores",
        "queue": "gmv.tasks.events",
        "default_scope": "stores",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["incremental", "full"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                "scope": {"type": "string"},
                "workspace_id": {"type": "integer", "minimum": 1},
                "auth_id": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": True,
        },
    },
    ("TTB_BASE", "ttb.sync.products"): {
        "kind": "celery_task",
        "celery_task": "ttb.sync.products",
        "queue": "gmv.tasks.events",
        "default_scope": "products",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["incremental", "full"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                "scope": {"type": "string"},
                "workspace_id": {"type": "integer", "minimum": 1},
                "auth_id": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": True,
        },
    },
}


def get_task_config(category: str | None, task_name: str | None) -> Mapping[str, Any] | None:
    if not category or not task_name:
        return None
    return SCHEDULED_TASKS.get((category, task_name))

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class HermesCapabilitiesResponse(BaseModel):
    enabled: bool
    model: str
    task_types: dict[str, str]
    max_input_chars: int
    allow_member: bool
    require_explicit_permission: bool
    can_use_task: bool | None = None


class HermesRunRequest(BaseModel):
    task_type: str = Field(default="general", max_length=64)
    title: str | None = Field(default=None, max_length=255)
    input: str | None = Field(default=None, max_length=30000)
    input_json: Any | None = None
    workspace_context: dict[str, Any] | None = None
    conversation_key: str | None = Field(default=None, max_length=64)
    async_mode: bool = Field(default=False)


class HermesRunResponse(BaseModel):
    run_id: str
    workspace_id: int
    user_id: int | None
    task_type: str
    title: str | None
    status: str
    result_text: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    hermes_response_id: str | None = None
    hermes_conversation: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    celery_task_id: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    class Config:
        from_attributes = True


class HermesRunListResponse(BaseModel):
    items: list[HermesRunResponse]


class HermesConversationResponse(BaseModel):
    id: int
    conversation_key: str
    workspace_id: int
    user_id: int | None
    task_type: str
    title: str | None
    last_response_id: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class HermesConversationListResponse(BaseModel):
    items: list[HermesConversationResponse]


class HermesMessageResponse(BaseModel):
    id: int
    conversation_id: int
    workspace_id: int
    user_id: int | None
    role: str
    content_text: str | None
    content_json: Any | None
    run_id: str | None
    created_at: datetime | None = None

    class Config:
        from_attributes = True


class HermesMessageListResponse(BaseModel):
    items: list[HermesMessageResponse]


class FeaturePermissionIn(BaseModel):
    user_id: int = Field(ge=1)
    feature_key: str = Field(min_length=1, max_length=128)
    is_enabled: bool = True


class FeaturePermissionOut(BaseModel):
    id: int
    workspace_id: int
    user_id: int
    feature_key: str
    is_enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class FeaturePermissionListResponse(BaseModel):
    items: list[FeaturePermissionOut]


class SpecializedHermesRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    input: str | None = Field(default=None, max_length=30000)
    input_json: Any | None = None
    workspace_context: dict[str, Any] | None = None
    conversation_key: str | None = Field(default=None, max_length=64)
    async_mode: bool = False


class HermesHealthResponse(BaseModel):
    ok: bool
    detail: dict[str, Any] | None = None

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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

    model_config = ConfigDict(from_attributes=True)


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

    model_config = ConfigDict(from_attributes=True)


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

    model_config = ConfigDict(from_attributes=True)


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

    model_config = ConfigDict(from_attributes=True)


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


class ContentFactoryPublishingProfile(BaseModel):
    platform: str | None = Field(default=None, max_length=64)
    channel_label: str | None = Field(default=None, max_length=128)
    hashtags: list[str] = Field(default_factory=list, max_length=5)
    title_suffix: str | None = Field(default=None, max_length=128)


class ContentFactoryCreativeCopyContract(BaseModel):
    product_reveal_segment: int | None = Field(default=None, ge=1, le=12)
    product_free_through_segment: int | None = Field(default=None, ge=0, le=12)
    conversion_segment: int | None = Field(default=None, ge=1, le=12)
    bridge_source_segment: int | None = Field(default=None, ge=1, le=12)
    tieback_source_segments: list[int] = Field(default_factory=list, max_length=12)
    require_causal_product_bridge: bool | None = None
    require_opening_tieback: bool | None = None
    product_role_terms: list[str] = Field(default_factory=list, max_length=20)
    pre_reveal_forbidden_terms: list[str] = Field(default_factory=list, max_length=40)
    require_post_cta_agency_ending: bool | None = None
    post_cta_agency_terms: list[str] = Field(default_factory=list, max_length=30)
    required_post_cta_agency_term_count: int | None = Field(
        default=None, ge=0, le=10
    )
    minimum_post_cta_word_count: int | None = Field(
        default=None, ge=0, le=50
    )


class ContentFactoryCreativeCastPolicy(BaseModel):
    allow_minor_story_characters: bool | None = None
    minimum_product_actor_age: int | None = Field(default=None, ge=0, le=120)
    max_spoken_voices: int | None = Field(default=None, ge=1, le=20)
    max_principal_characters: int | None = Field(default=None, ge=1, le=20)
    instructions: list[str] = Field(default_factory=list, max_length=20)


class ContentFactoryProductPresentationPolicy(BaseModel):
    authority_mode: Literal["uploaded_source_only", "generated_allowed"] | None = None
    presentation_instructions: list[str] = Field(default_factory=list, max_length=20)
    forbidden_interaction_categories: list[
        Literal[
            "open_package",
            "expose_loose_contents",
            "consume_product",
            "minor_product_interaction",
        ]
    ] = Field(default_factory=list, max_length=4)


class ContentFactoryDirectorLoopPolicy(BaseModel):
    maximum_revisions: int = Field(ge=0, le=10)
    maximum_contract_repairs_per_revision: int = Field(ge=0, le=3)
    series_page_size: int = Field(default=10, ge=1, le=100)


class ContentFactoryProducerTurnRequest(BaseModel):
    session_key: str | None = Field(default=None, max_length=48)
    client_turn_id: str | None = Field(default=None, min_length=8, max_length=32)
    message: str = Field(min_length=1, max_length=50000)
    product_id: int | None = Field(default=None, ge=1)


class ContentFactoryProducerTurnResponse(BaseModel):
    session_key: str
    status: Literal["needs_input", "proposal_ready"]
    assistant_message: str
    missing_information: list[str] = Field(default_factory=list)
    proposal: dict[str, Any] | None = None
    proposal_sha256: str | None = None
    selected_product_id: int | None = None
    authoritative_script_message_id: int | None = None
    authoritative_script: str | None = None
    authoritative_script_version: int | None = None
    revised_authoritative_script: str | None = None
    intent_spec: dict[str, Any] | None = None
    pending_decision_id: str | None = None


class ContentFactoryProducerMessageOut(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    client_turn_id: str | None = None
    created_at: datetime


class ContentFactoryProducerAttachmentOut(BaseModel):
    attachment_key: str
    kind: Literal[
        "reference_video", "character_reference", "brief_document", "creative_reference"
    ]
    original_name: str
    mime_type: str | None = None
    size_bytes: int
    analysis_status: str
    analysis: dict[str, Any] = Field(default_factory=dict)
    character_name: str | None = None
    character_description: str | None = None
    locked: bool = False
    active: bool = True
    created_at: datetime


class ContentFactoryProducerReferenceLinkRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    context_message: str | None = Field(default=None, max_length=50000)
    product_id: int | None = Field(default=None, ge=1)


class ContentFactoryProducerSessionOut(BaseModel):
    session_key: str
    status: str
    draft_message: str | None = None
    source_context: dict[str, Any] | None = None
    messages: list[ContentFactoryProducerMessageOut] = Field(default_factory=list)
    attachments: list[ContentFactoryProducerAttachmentOut] = Field(default_factory=list)
    proposal: dict[str, Any] | None = None
    proposal_sha256: str | None = None
    selected_product_id: int | None = None
    authoritative_script_message_id: int | None = None
    authoritative_script: str | None = None
    authoritative_script_version: int | None = None
    intent_spec: dict[str, Any] | None = None
    pending_decision_id: str | None = None
    created_project_key: str | None = None


class ContentFactoryProducerConfirmRequest(BaseModel):
    proposal_sha256: str = Field(min_length=64, max_length=64)
    pending_decision_id: str | None = Field(default=None, min_length=10, max_length=80)


class ContentFactoryProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content_objective: str | None = Field(default=None, max_length=255)
    target_audience: str | None = Field(default=None, max_length=1000)
    content_mode: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$",
    )
    product_use_mode: Literal["required", "context_only", "none"] | None = None
    product_required: bool | None = None
    product_brief: str | None = Field(default=None, max_length=30000)
    video_count: int | None = Field(default=None, ge=1, le=50)
    max_api_video_variants_in_flight: int | None = Field(
        default=None,
        ge=1,
        le=16,
    )
    video_duration_min_seconds: int | None = Field(default=None, ge=1, le=120)
    video_duration_max_seconds: int | None = Field(default=None, ge=1, le=120)
    video_model: Literal["omni_flash", "seedance_2_0_mini"] | None = None
    video_duration_strategy: Literal[
        "creative_flexibility", "cross_provider_portable"
    ] | None = None
    video_resolution: Literal["480p", "720p"] | None = None
    video_aspect_ratio: Literal["9:16", "16:9", "1:1"] | None = None
    video_language: Literal["en-US", "zh-CN"] | None = None
    video_reference_limit: int | None = Field(default=None, ge=1, le=10)
    video_frame_mode: Literal["reference", "first_last"] | None = None
    allow_reference_video: bool | None = None
    video_generation_mode: Literal[
        "text_to_video", "image_to_video", "video_to_video"
    ] | None = None
    visual_reference_generation_mode: Literal["individual", "board"] | None = None
    visual_image_model_chain: list[
        Literal[
            "gpt-image-2",
            "gpt-image-2.0",
            "nano_banana_pro",
            "nano_banana_2",
        ]
    ] | None = Field(default=None, min_length=1, max_length=4)
    preferred_browser_device_id: str | None = Field(default=None, max_length=128)
    confirmed_claims: str | None = Field(default=None, max_length=30000)
    confirmed_selling_points: str | None = Field(default=None, max_length=30000)
    confirmed_promotions: str | None = Field(default=None, max_length=5000)
    promotion_cta: str | None = Field(default=None, max_length=500)
    allow_promotional_cta: bool | None = None
    publishing_profile: ContentFactoryPublishingProfile | None = None
    creative_copy_contract: ContentFactoryCreativeCopyContract | None = None
    creative_cast_policy: ContentFactoryCreativeCastPolicy | None = None
    product_presentation_policy: ContentFactoryProductPresentationPolicy | None = None
    content_director_mode: Literal["enforce"] | None = None
    director_series_brief: dict[str, Any] | None = None
    director_briefs_by_variant: dict[str, dict[str, Any]] | None = Field(
        default=None,
        max_length=50,
    )
    director_loop_policy: ContentFactoryDirectorLoopPolicy | None = None
    director_creative_constraints: list[str] | None = Field(
        default=None,
        max_length=128,
    )
    director_copy_review_criteria: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=64,
    )
    director_series_page_review_criteria: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=64,
    )
    director_series_global_review_criteria: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=64,
    )
    director_diversity_requirements: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=64,
    )
    director_structured_intent_contract_required: bool | None = None
    auto_run: bool | None = None


class ContentFactoryProjectRestart(BaseModel):
    stage: Literal["SERIES_DIRECTOR", "DIRECTOR", "PRODUCTION_PLAN", "VISUAL_PREVIEW", "CREATIVE_REVIEW", "FINAL_ASSETS", "VIDEO_PROMPTS", "EDIT_PACKAGE"] = "DIRECTOR"
    instruction: str | None = Field(default=None, max_length=30000)
    allowed_audio_modes: list[
        Literal["spoken", "silent", "music_only", "sound_design"]
    ] | None = Field(default=None, min_length=1, max_length=4)
    replace_completed: bool = False
    auto_run: bool = True


class ContentFactoryProductCreate(BaseModel):
    brand_name: str = Field(min_length=1, max_length=255)
    product_name: str = Field(min_length=1, max_length=255)
    market: str = Field(default="US", min_length=1, max_length=64)
    product_brief: str | None = Field(default=None, max_length=30000)
    facts_json: dict[str, Any] | None = None


class ContentFactoryProductUpdate(BaseModel):
    brand_name: str | None = Field(default=None, min_length=1, max_length=255)
    product_name: str | None = Field(default=None, min_length=1, max_length=255)
    market: str | None = Field(default=None, min_length=1, max_length=64)
    product_brief: str | None = Field(default=None, max_length=30000)


class ContentFactoryProductAssetOut(BaseModel):
    id: int
    kind: str
    original_name: str
    mime_type: str | None = None
    size_bytes: int | None = None
    meta_json: dict[str, Any] | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ContentFactoryProductOut(BaseModel):
    id: int
    product_key: str
    workspace_id: int
    user_id: int | None = None
    brand_name: str
    product_name: str
    market: str
    product_brief: str | None = None
    facts_json: dict[str, Any] | None = None
    status: str
    meta_json: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    assets: list[ContentFactoryProductAssetOut] = Field(default_factory=list)


class ContentFactoryProductList(BaseModel):
    items: list[ContentFactoryProductOut]


class ContentFactoryCommand(BaseModel):
    instruction: str | None = Field(default=None, max_length=30000)
    stage: Literal["FACTS", "SERIES_DIRECTOR", "DIRECTOR", "PRODUCTION_PLAN", "VISUAL_PREVIEW", "CREATIVE_REVIEW", "FINAL_ASSETS", "VIDEO_PROMPTS", "EDIT_PACKAGE"] | None = None
    run_mode: Literal["continue", "single"] = "continue"


class ContentFactoryPauseRequest(BaseModel):
    note: str | None = Field(default=None, max_length=5000)


class ContentFactoryRolloutGateRequest(BaseModel):
    authorized_variant_indices: list[int] = Field(min_length=1, max_length=50)
    batch_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    pause_when_complete: bool = True


class ContentFactoryAssetOut(BaseModel):
    id: int
    stage: str | None = None
    kind: str
    original_name: str
    mime_type: str | None = None
    size_bytes: int | None = None
    meta_json: dict[str, Any] | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ContentFactoryStageOut(BaseModel):
    id: int
    stage: str
    attempt: int
    status: str
    instruction: str | None = None
    output_json: dict[str, Any] | None = None
    response_text: str | None = None
    chat_url: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ContentFactoryProjectOut(BaseModel):
    project_key: str
    workspace_id: int
    user_id: int | None = None
    product_id: int | None = None
    title: str
    product_name: str
    market: str
    status: str
    current_stage: str
    product_brief: str | None = None
    state_json: dict[str, Any] | None = None
    config_json: dict[str, Any] | None = None
    browser_slot: str | int | None = None
    browser_cdp_url: str | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    stages: list[ContentFactoryStageOut] = Field(default_factory=list)
    assets: list[ContentFactoryAssetOut] = Field(default_factory=list)
    deliverables: dict[str, Any] | None = None


class ContentFactoryProjectList(BaseModel):
    items: list[ContentFactoryProjectOut]


class ContentFactoryAdminProjectSummary(BaseModel):
    project_key: str
    workspace_id: int
    user_id: int
    created_by_label: str | None = None
    created_by_usercode: str | None = None
    title: str
    product_name: str
    market: str
    status: str
    current_stage: str
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deliverables: dict[str, Any] | None = None


class ContentFactoryAdminProjectList(BaseModel):
    items: list[ContentFactoryAdminProjectSummary]


class ContentFactoryBridgeStatus(BaseModel):
    connected: bool
    browser: str | None = None
    detail: str | None = None
    slots: list[dict[str, Any]] = Field(default_factory=list)
    capacity: int | None = None
    active_slots: int | None = None
    load: dict[str, Any] | None = None
    mode: str | None = None
    devices: list[dict[str, Any]] = Field(default_factory=list)
    selected_device_id: str | None = None
    selection_required: bool = False
    server_agent_version: str | None = None


class ContentFactoryBridgeDeviceAction(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)


class ContentFactoryBridgeAgentSlotStatus(BaseModel):
    bridge_id: str = Field(min_length=1, max_length=64)
    connected: bool = False
    mode: Literal["active", "dormant"] = "active"
    browser: str | None = Field(default=None, max_length=255)
    error: str | None = Field(default=None, max_length=1000)
    auth_status: str | None = Field(default=None, max_length=32)
    account_name: str | None = Field(default=None, max_length=120)
    page_url: str | None = Field(default=None, max_length=1000)
    purpose: Literal["content_factory", "flow_account", "jimeng_lab", "doubao_lab", "yt_dlp_account"] = "content_factory"
    flow_status: str | None = Field(default=None, max_length=32)
    capture_id: str | None = Field(default=None, max_length=64)
    session_diagnostics: dict[str, Any] = Field(default_factory=dict)
    profile_reset: bool = False
    synced_files: list[dict[str, Any]] = Field(default_factory=list)
    last_sync_at: str | None = Field(default=None, max_length=64)
    sync_error: str | None = Field(default=None, max_length=1000)


class ContentFactoryBridgeAgentHeartbeat(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    device_name: str = Field(default="Windows device", max_length=255)
    agent_version: str = Field(default="legacy", max_length=64)
    public_key: str = Field(min_length=40, max_length=2048)
    inbox_root: str = Field(min_length=3, max_length=1024)
    local_capacity: int = Field(default=4, ge=1, le=8)
    profile_capacity: int = Field(default=64, ge=1, le=128)
    host_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    installed_bindings: list[Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")]] = Field(
        default_factory=list, max_length=64
    )
    update_state: Literal["current", "installing", "failed"] | None = None
    update_error: str | None = Field(default=None, max_length=1000)
    slots: list[ContentFactoryBridgeAgentSlotStatus] = Field(default_factory=list)


class ContentFactoryBridgeFlowCapture(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    bridge_id: str = Field(min_length=1, max_length=64)
    capture_id: str = Field(min_length=1, max_length=64)
    session_token: str = Field(min_length=20, max_length=20_000)
    session_tokens: list[Annotated[str, Field(min_length=20, max_length=20_000)]] = Field(
        default_factory=list, max_length=8
    )
    session_diagnostics: dict[str, Any] = Field(default_factory=dict)
    profile_id: str = Field(min_length=1, max_length=255)
    fingerprint: dict[str, Any]


class ContentFactoryBridgeJimengCapture(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    bridge_id: str = Field(min_length=1, max_length=64)
    capture_id: str = Field(min_length=1, max_length=64)
    session_token: str = Field(min_length=20, max_length=20_000)
    session_tokens: list[Annotated[str, Field(min_length=1, max_length=20_000)]] = Field(
        default_factory=list, max_length=8
    )
    session_diagnostics: dict[str, Any] = Field(default_factory=dict)
    session_cookies: list[dict[str, Any]] = Field(default_factory=list, max_length=80)
    profile_id: str = Field(min_length=1, max_length=255)
    fingerprint: dict[str, Any]


class ContentFactoryBridgeDoubaoCapture(ContentFactoryBridgeJimengCapture):
    """Purpose-scoped Doubao browser context; credentials never reach the UI."""


class ContentFactoryBridgeYtDlpCapture(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    bridge_id: str = Field(min_length=1, max_length=64)
    capture_id: str = Field(min_length=1, max_length=64)
    session_cookies: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    profile_id: str = Field(min_length=1, max_length=255)

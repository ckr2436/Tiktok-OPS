"""Typed TikTok Business GMV Max client built on top of :mod:`app.services.ttb_api`."""

from __future__ import annotations

from typing import Any, Dict, Generic, Iterable, List, Mapping, Optional, Sequence, Type, TypeVar

import json

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.services import ttb_api as _ttb_api
from app.services.ttb_api import TTBApiClient


class PageInfo(BaseModel):
    """Common pagination block returned by GMV Max endpoints."""

    page: Optional[int] = None
    page_size: Optional[int] = None
    total_number: Optional[int] = None
    total_page: Optional[int] = None
    cursor: Optional[str] = None
    has_more: Optional[bool] = None
    has_next: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaign(BaseModel):
    """Minimal view of a GMV Max campaign entry."""

    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    advertiser_id: Optional[str] = None
    operation_status: Optional[str] = None
    secondary_status: Optional[str] = None
    objective_type: Optional[str] = None
    gmv_max_promotion_type: Optional[str] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None
    create_time: Optional[str] = None
    modify_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignListData(BaseModel):
    """Envelope describing the payload of :func:`gmv_max_campaign_get`."""

    list: List[GMVMaxCampaign] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None
    links: Optional[Dict[str, Any]] = None
    stores: Optional[List[Dict[str, Any]]] = None

    model_config = ConfigDict(extra="allow")


class CampaignStatusEntry(BaseModel):
    """Individual campaign status returned by /campaign/status/update/."""

    campaign_id: Optional[str] = None
    status: Optional[str] = None
    postback_window_mode: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class CampaignStatusUpdateData(BaseModel):
    """Response payload returned by /campaign/status/update/."""

    campaign_ids: Optional[List[str]] = None
    status: Optional[str] = None
    campaign_list: Optional[List[CampaignStatusEntry]] = None

    model_config = ConfigDict(extra="allow")


class PromotionDaysSetting(BaseModel):
    """Promotion days configuration summary."""

    is_enabled: Optional[bool] = None
    auto_schedule_enabled: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxIdentityInfo(BaseModel):
    """Identity descriptor returned by GMV Max endpoints."""

    identity_id: Optional[str] = None
    identity_type: Optional[str] = None
    user_name: Optional[str] = None
    profile_image: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxVideoInfo(BaseModel):
    """Video metadata used in video/custom anchor listings."""

    video_id: Optional[str] = None
    preview_url: Optional[str] = None
    video_cover_url: Optional[str] = None
    duration: Optional[float] = None
    size: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignInfoData(BaseModel):
    """Detailed campaign information returned by ``campaign/gmv_max/info``."""

    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    advertiser_id: Optional[str] = None
    store_id: Optional[str] = None
    store_authorized_bc_id: Optional[str] = None
    shopping_ads_type: Optional[str] = None
    optimization_goal: Optional[str] = None
    budget: Optional[float] = None
    roas_bid: Optional[float] = None
    promotion_days: Optional[PromotionDaysSetting] = None
    auto_budget_enabled: Optional[bool] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None
    identity_list: Optional[List[GMVMaxIdentityInfo]] = None
    custom_anchor_video_list: Optional[List[Dict[str, Any]]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxSessionSettings(BaseModel):
    """Core session level settings for creative boost / max delivery."""

    budget: Optional[float] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxSessionProduct(BaseModel):
    """Product level session settings."""

    spu_id: Optional[str] = None
    item_id: Optional[str] = None
    budget: Optional[float] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxSession(BaseModel):
    """Session summary returned by list/get endpoints."""

    session_id: Optional[str] = None
    campaign_id: Optional[str] = None
    store_id: Optional[str] = None
    bid_type: Optional[str] = None
    status: Optional[str] = None
    session: Optional[GMVMaxSessionSettings] = None
    product_list: Optional[List[GMVMaxSessionProduct]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxSessionListData(BaseModel):
    """Payload returned by ``campaign/gmv_max/session/list``."""

    list: List[GMVMaxSession] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxIdentity(BaseModel):
    """Identity result returned by ``gmv_max/identity/get``."""

    identity_info: Optional[GMVMaxIdentityInfo] = None
    identity_authorized_bc_id: Optional[str] = None
    identity_authorized_bc_name: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxIdentityListData(BaseModel):
    """Payload for identity listing."""

    identity_list: List[GMVMaxIdentity] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class GMVMaxOccupiedAd(BaseModel):
    """Represents an occupied ad entity returned by occupancy checks."""

    advertiser_id: Optional[str] = None
    campaign_id: Optional[str] = None
    adgroup_id: Optional[str] = None
    ad_id: Optional[str] = None
    create_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxOccupiedListData(BaseModel):
    """Payload for ``gmv_max/occupied_custom_shop_ads/list``."""

    occupied_custom_shop_ads: List[GMVMaxOccupiedAd] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class GMVMaxVideo(BaseModel):
    """Entry returned by ``gmv_max/video/get``."""

    item_id: Optional[str] = None
    spu_id_list: Optional[List[str]] = None
    identity_info: Optional[GMVMaxIdentityInfo] = None
    video_info: Optional[GMVMaxVideoInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxVideoListData(BaseModel):
    """Payload for video listing endpoints."""

    video_list: List[GMVMaxVideo] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCustomAnchorVideo(BaseModel):
    """Customized anchor video record."""

    item_id: Optional[str] = None
    identity_info: Optional[GMVMaxIdentityInfo] = None
    spu_id_list: Optional[List[str]] = None
    video_info: Optional[GMVMaxVideoInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCustomAnchorVideoListData(BaseModel):
    """Response body for ``gmv_max/custom_anchor_video_list/get``."""

    custom_anchor_video_list: List[GMVMaxCustomAnchorVideo] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxExclusiveAuthorizationData(BaseModel):
    """Authorization state for a TikTok Shop store."""

    store_id: Optional[str] = None
    store_authorized_bc_id: Optional[str] = None
    is_authorized: Optional[bool] = None
    authorized_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxBidRecommendation(BaseModel):
    """Recommended bid target and budget."""

    roas_bid: Optional[float] = None
    budget: Optional[float] = None
    recommendation: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxReportEntry(BaseModel):
    """Single row returned from the GMV Max report endpoint."""

    metrics: Optional[Dict[str, Any]] = None
    dimensions: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxReportData(BaseModel):
    """Report payload including data rows and pagination info."""

    list: List[GMVMaxReportEntry] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None
    summary: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


# ------------------------- Request models -------------------------


class GMVMaxCampaignFiltering(BaseModel):
    """Filtering block accepted by campaign list/report endpoints."""

    gmv_max_promotion_types: List[str]
    store_ids: Optional[List[str]] = None
    campaign_ids: Optional[List[str]] = None
    campaign_name: Optional[str] = None
    primary_status: Optional[str] = None
    creation_filter_start_time: Optional[str] = None
    creation_filter_end_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignGetRequest(BaseModel):
    advertiser_id: str
    filtering: GMVMaxCampaignFiltering
    fields: Optional[List[str]] = None
    page: Optional[int] = None
    page_size: Optional[int] = None

    model_config = ConfigDict(extra="forbid")


class GMVMaxCampaignInfoRequest(BaseModel):
    advertiser_id: str
    campaign_id: str


class GMVMaxCampaignCreateBody(BaseModel):
    request_id: Optional[str] = None
    store_id: str
    store_authorized_bc_id: Optional[str] = None
    shopping_ads_type: str
    optimization_goal: str
    campaign_name: str
    budget: Optional[float] = None
    roas_bid: Optional[float] = None
    promotion_days: Optional[PromotionDaysSetting] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None
    identity_list: Optional[List[GMVMaxIdentityInfo]] = None
    product_video_specific_type: Optional[str] = None
    custom_anchor_video_list: Optional[List[Dict[str, Any]]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignCreateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxCampaignCreateBody


class GMVMaxCampaignUpdateBody(BaseModel):
    campaign_id: str
    campaign_name: Optional[str] = None
    budget: Optional[float] = None
    roas_bid: Optional[float] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None
    promotion_days: Optional[PromotionDaysSetting] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignUpdateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxCampaignUpdateBody


class CampaignStatusUpdateRequest(BaseModel):
    """Request payload for /campaign/status/update/."""

    advertiser_id: str
    campaign_ids: List[str]
    operation_status: str
    postback_window_mode: Optional[str] = None


class GMVMaxCampaignActionApplyBody(BaseModel):
    campaign_id: str
    action_type: str
    creative_id: Optional[str] = None
    mode: Optional[str] = None
    target_daily_budget: Optional[float] = None
    budget_delta: Optional[float] = None
    currency: Optional[str] = None
    max_duration_minutes: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignActionApplyRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxCampaignActionApplyBody


class GMVMaxCampaignActionApplyData(BaseModel):
    model_config = ConfigDict(extra="allow")


class GMVMaxSessionCreateBody(BaseModel):
    campaign_id: str
    store_id: str
    bid_type: Optional[str] = None
    session: GMVMaxSessionSettings
    product_list: List[GMVMaxSessionProduct]

    model_config = ConfigDict(extra="allow")


class GMVMaxSessionCreateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxSessionCreateBody


class GMVMaxSessionUpdateBody(BaseModel):
    campaign_id: str
    session_id: str
    store_id: Optional[str] = None
    session: Optional[GMVMaxSessionSettings] = None
    product_list: Optional[List[GMVMaxSessionProduct]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxSessionUpdateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxSessionUpdateBody


class GMVMaxSessionListRequest(BaseModel):
    advertiser_id: str
    campaign_id: str
    page: Optional[int] = None
    page_size: Optional[int] = None


class GMVMaxIdentityGetRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str


class GMVMaxOccupiedCustomShopAdsListRequest(BaseModel):
    advertiser_id: str
    store_id: str
    occupied_asset_type: str
    asset_ids: List[str]


class GMVMaxVideoGetRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str
    spu_id_list: Optional[List[str]] = None
    custom_posts_eligible: Optional[bool] = None
    sort_field: Optional[str] = None
    sort_order: Optional[str] = None
    page: Optional[int] = None
    page_size: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCustomAnchorVideoQuery(BaseModel):
    item_id: Optional[str] = None
    spu_id_list: Optional[List[str]] = None
    identity_info: Optional[GMVMaxIdentityInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCustomAnchorVideoListGetRequest(BaseModel):
    advertiser_id: str
    campaign_id: Optional[str] = None
    campaign_custom_anchor_video_id: Optional[str] = None
    custom_anchor_video_list: Optional[List[GMVMaxCustomAnchorVideoQuery]] = None
    page: Optional[int] = None
    page_size: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxExclusiveAuthorizationGetRequest(BaseModel):
    advertiser_id: str
    store_id: str


class GMVMaxExclusiveAuthorizationCreateRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str


class GMVMaxBidRecommendRequest(BaseModel):
    advertiser_id: str
    store_id: str
    shopping_ads_type: str
    optimization_goal: str
    item_group_ids: Sequence[str]
    identity_id: Optional[str] = None


class GMVMaxReportFiltering(BaseModel):
    """Filtering block for GMV Max report."""

    gmv_max_promotion_types: Optional[List[str]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxReportGetRequest(BaseModel):
    advertiser_id: str
    store_ids: Sequence[str]
    start_date: str
    end_date: str
    metrics: Sequence[str]
    dimensions: Sequence[str]
    enable_total_metrics: Optional[bool] = None
    filtering: Optional[GMVMaxReportFiltering] = None
    page: Optional[int] = None
    page_size: Optional[int] = None
    sort_field: Optional[str] = None
    sort_type: Optional[str] = None

    model_config = ConfigDict(extra="allow")


# ------------------------- Response envelope -------------------------


T = TypeVar("T", bound=BaseModel)


class GMVMaxResponse(BaseModel, Generic[T]):
    """Standard TikTok Business response wrapper."""

    code: int
    message: str
    request_id: Optional[str] = None
    data: T

    model_config = ConfigDict(extra="allow")


def _coerce_store_ids(value: Any) -> List[str]:
    """Normalize store identifiers into a list of non-empty strings."""

    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Sequence):
        results: List[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                results.append(text)
        return results
    text = str(value).strip()
    return [text] if text else []


# ------------------------- Client implementation -------------------------


class TikTokBusinessGMVMaxClient(TTBApiClient):
    """High level typed wrappers for TikTok Business GMV Max endpoints."""

    def __init__(
        self,
        *,
        access_token: str,
        app_id: str | None = None,
        app_secret: str | None = None,
        qps: float | None = None,
        timeout: float | None = None,
        headers: Optional[Dict[str, str]] = None,
        http_client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            access_token=access_token,
            app_id=app_id,
            app_secret=app_secret,
            qps=qps,
            timeout=timeout,
            headers=headers,
            **kwargs,
        )
        if http_client is not None:
            self._client = http_client

    def _parse_response(self, payload: Mapping[str, Any], data_type: Type[T]) -> GMVMaxResponse[T]:
        try:
            raw_code = payload.get("code", 0)
            code = int(raw_code) if isinstance(raw_code, (int, float, str)) else 0
        except ValueError:
            code = 0
        message = payload.get("message") or ""
        request_id = payload.get("request_id")
        data_payload = payload.get("data") or {}
        data_model = data_type.model_validate(data_payload)
        return GMVMaxResponse[T](code=code, message=str(message), request_id=request_id, data=data_model)

    async def gmv_max_campaign_get(
        self, request: GMVMaxCampaignGetRequest
    ) -> GMVMaxResponse[GMVMaxCampaignListData]:
        params = request.model_dump(exclude_none=True)
        filtering_payload = params.get("filtering")
        if isinstance(filtering_payload, dict):
            normalized_store_ids: List[str] = []
            normalized_store_ids.extend(
                _coerce_store_ids(filtering_payload.pop("store_ids", None))
            )
            normalized_store_ids.extend(
                _coerce_store_ids(filtering_payload.pop("store_id", None))
            )
        else:
            filtering_payload = None
            normalized_store_ids = []

        # TikTok expects store_ids as a top-level JSON array instead of inside the
        # filtering object. Preserve compatibility by accepting store_ids from
        # either location but always emit a single normalized payload.
        normalized_store_ids.extend(_coerce_store_ids(params.pop("store_ids", None)))
        if normalized_store_ids:
            unique_ids = list(dict.fromkeys(normalized_store_ids))
            params["store_ids"] = json.dumps(unique_ids, ensure_ascii=False)
        elif filtering_payload is not None:
            filtering_payload.pop("store_ids", None)

        _ttb_api._ensure_gmvmax_campaign_filters(params, promotion_type_format="campaign")
        cleaned = _ttb_api._clean_params_map(params)
        payload = await self._request_json("GET", "/gmv_max/campaign/get/", params=cleaned)
        return self._parse_response(payload, GMVMaxCampaignListData)

    async def gmv_max_campaign_info(
        self, request: GMVMaxCampaignInfoRequest
    ) -> GMVMaxResponse[GMVMaxCampaignInfoData]:
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json("GET", "/campaign/gmv_max/info/", params=params)
        return self._parse_response(payload, GMVMaxCampaignInfoData)

    async def gmv_max_campaign_create(
        self, request: GMVMaxCampaignCreateRequest
    ) -> GMVMaxResponse[GMVMaxCampaignInfoData]:
        params = {"advertiser_id": request.advertiser_id}
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/create/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxCampaignInfoData)

    async def gmv_max_campaign_update(
        self, request: GMVMaxCampaignUpdateRequest
    ) -> GMVMaxResponse[GMVMaxCampaignInfoData]:
        params = {"advertiser_id": request.advertiser_id}
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/update/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxCampaignInfoData)

    async def campaign_status_update(
        self, request: CampaignStatusUpdateRequest
    ) -> GMVMaxResponse[CampaignStatusUpdateData]:
        params = {"advertiser_id": request.advertiser_id}
        body = {
            "advertiser_id": request.advertiser_id,
            "campaign_ids": [str(c) for c in request.campaign_ids if str(c).strip()],
            "operation_status": str(request.operation_status),
            "postback_window_mode": request.postback_window_mode,
        }
        payload = await self._request_json(
            "POST",
            "/campaign/status/update/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, CampaignStatusUpdateData)

    async def gmv_max_campaign_action_apply(
        self, request: GMVMaxCampaignActionApplyRequest
    ) -> GMVMaxResponse[GMVMaxCampaignActionApplyData]:
        params = {"advertiser_id": request.advertiser_id}
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/action/apply/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxCampaignActionApplyData)

    async def gmv_max_session_create(
        self, request: GMVMaxSessionCreateRequest
    ) -> GMVMaxResponse[GMVMaxSessionListData]:
        params = {"advertiser_id": request.advertiser_id}
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/session/create/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxSessionListData)

    async def gmv_max_session_update(
        self, request: GMVMaxSessionUpdateRequest
    ) -> GMVMaxResponse[GMVMaxSessionListData]:
        params = {"advertiser_id": request.advertiser_id}
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/session/update/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxSessionListData)

    async def gmv_max_session_list(
        self, request: GMVMaxSessionListRequest
    ) -> GMVMaxResponse[GMVMaxSessionListData]:
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/campaign/gmv_max/session/list/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxSessionListData)

    async def gmv_max_identity_get(
        self, request: GMVMaxIdentityGetRequest
    ) -> GMVMaxResponse[GMVMaxIdentityListData]:
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/gmv_max/identity/get/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxIdentityListData)

    async def gmv_max_occupied_custom_shop_ads_list(
        self, request: GMVMaxOccupiedCustomShopAdsListRequest
    ) -> GMVMaxResponse[GMVMaxOccupiedListData]:
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/gmv_max/occupied_custom_shop_ads/list/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxOccupiedListData)

    async def gmv_max_video_get(
        self, request: GMVMaxVideoGetRequest
    ) -> GMVMaxResponse[GMVMaxVideoListData]:
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/gmv_max/video/get/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxVideoListData)

    async def gmv_max_custom_anchor_video_list_get(
        self, request: GMVMaxCustomAnchorVideoListGetRequest
    ) -> GMVMaxResponse[GMVMaxCustomAnchorVideoListData]:
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/gmv_max/custom_anchor_video_list/get/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxCustomAnchorVideoListData)

    async def gmv_max_exclusive_authorization_get(
        self, request: GMVMaxExclusiveAuthorizationGetRequest
    ) -> GMVMaxResponse[GMVMaxExclusiveAuthorizationData]:
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/gmv_max/exclusive_authorization/get/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxExclusiveAuthorizationData)

    async def gmv_max_exclusive_authorization_create(
        self, request: GMVMaxExclusiveAuthorizationCreateRequest
    ) -> GMVMaxResponse[GMVMaxExclusiveAuthorizationData]:
        params = {"advertiser_id": request.advertiser_id}
        body = {
            "advertiser_id": request.advertiser_id,
            "store_id": request.store_id,
            "store_authorized_bc_id": request.store_authorized_bc_id,
        }
        payload = await self._request_json(
            "POST",
            "/gmv_max/exclusive_authorization/create/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxExclusiveAuthorizationData)

    async def gmv_max_bid_recommend(
        self, request: GMVMaxBidRecommendRequest
    ) -> GMVMaxResponse[GMVMaxBidRecommendation]:
        params = request.model_dump(exclude_none=True)
        params["item_group_ids"] = [str(item) for item in request.item_group_ids]
        payload = await self._request_json(
            "GET",
            "/gmv_max/bid/recommend/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxBidRecommendation)

    async def gmv_max_report_get(
        self, request: GMVMaxReportGetRequest
    ) -> GMVMaxResponse[GMVMaxReportData]:
        store_ids = [str(store) for store in request.store_ids]
        params: Dict[str, Any] = {
            "advertiser_id": request.advertiser_id,
            # TikTok API requires store_ids to be encoded as an array field even on GET.
            # Passing repeated query params (store_ids=123&store_ids=456) causes the
            # API to treat the value as a scalar string and respond with
            # "store_ids: Field must be set to array". Encoding the list as a JSON
            # array matches the API expectation.
            "store_ids": json.dumps(store_ids, ensure_ascii=False),
            "start_date": request.start_date,
            "end_date": request.end_date,
            # Dimensions/metrics must also be sent as JSON arrays even though the
            # endpoint is a GET request. Sending repeated query params leads to
            # errors such as "dimensions: error unmarshaling parameter
            # \"dimensions\"" from the TikTok API.
            "metrics": json.dumps(list(request.metrics), ensure_ascii=False),
            "dimensions": json.dumps(list(request.dimensions), ensure_ascii=False),
        }
        if request.enable_total_metrics is not None:
            params["enable_total_metrics"] = bool(request.enable_total_metrics)
        if request.filtering is not None:
            params["filtering"] = json.dumps(
                request.filtering.model_dump(exclude_none=True), ensure_ascii=False
            )
        for key in ("page", "page_size", "sort_field", "sort_type"):
            value = getattr(request, key)
            if value is not None:
                params[key] = value
        _ttb_api._ensure_gmvmax_campaign_filters(params, promotion_type_format="report")
        payload = await self._request_json(
            "GET",
            "/gmv_max/report/get/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxReportData)


__all__ = [
    "TikTokBusinessGMVMaxClient",
    "GMVMaxResponse",
    "GMVMaxCampaignGetRequest",
    "GMVMaxCampaignInfoRequest",
    "GMVMaxCampaignCreateRequest",
    "GMVMaxCampaignUpdateRequest",
    "GMVMaxSessionCreateRequest",
    "GMVMaxSessionUpdateRequest",
    "GMVMaxSessionListRequest",
    "GMVMaxIdentityGetRequest",
    "GMVMaxOccupiedCustomShopAdsListRequest",
    "GMVMaxVideoGetRequest",
    "GMVMaxCustomAnchorVideoListGetRequest",
    "GMVMaxExclusiveAuthorizationGetRequest",
    "GMVMaxExclusiveAuthorizationCreateRequest",
    "GMVMaxBidRecommendRequest",
    "GMVMaxReportGetRequest",
]

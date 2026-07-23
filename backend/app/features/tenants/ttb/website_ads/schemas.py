from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class MagentoConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    base_url: HttpUrl
    access_token: str = Field(min_length=8, max_length=4096)
    is_enabled: bool = True


class MagentoConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: HttpUrl | None = None
    access_token: str | None = Field(default=None, min_length=8, max_length=4096)
    is_enabled: bool | None = None


class TargetingConfig(BaseModel):
    location_ids: list[str] = Field(min_length=1, max_length=3000)
    zipcode_ids: list[str] = Field(default_factory=list, max_length=3000)
    gender: Literal["GENDER_UNLIMITED", "GENDER_FEMALE", "GENDER_MALE"] = "GENDER_UNLIMITED"
    age_groups: list[str] = Field(
        default_factory=lambda: ["AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"]
    )
    languages: list[str] = Field(default_factory=list)
    interest_category_ids: list[str] = Field(default_factory=list)
    audience_ids: list[str] = Field(default_factory=list)
    excluded_audience_ids: list[str] = Field(default_factory=list)
    placement_type: Literal["PLACEMENT_TYPE_AUTOMATIC", "PLACEMENT_TYPE_NORMAL"] = "PLACEMENT_TYPE_NORMAL"
    placements: list[str] = Field(default_factory=lambda: ["PLACEMENT_TIKTOK"])

    @field_validator("location_ids", "zipcode_ids", "languages", "interest_category_ids", "audience_ids", "excluded_audience_ids", "placements")
    @classmethod
    def normalize_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))

    @model_validator(mode="after")
    def require_defined_audience(self):
        if not self.interest_category_ids and not self.audience_ids:
            raise ValueError("At least one verified interest or custom audience is required")
        if self.placement_type != "PLACEMENT_TYPE_NORMAL" or self.placements != ["PLACEMENT_TIKTOK"]:
            raise ValueError("Website Ads delivery is restricted to TikTok placement")
        return self


class GuardConfig(BaseModel):
    enabled: bool = True
    target_roas: float | None = Field(default=None, gt=0)
    max_unprofitable_spend: float | None = Field(default=None, gt=0)
    min_clicks_before_action: int | None = Field(default=None, ge=1)
    min_ctr: float = Field(default=0.04, gt=0, le=1)
    max_cpc: float = Field(default=0.30, gt=0)
    min_impressions_before_action: int = Field(default=100, ge=20, le=1000000)
    min_clicks_for_cpc: int = Field(default=3, ge=1, le=100000)
    min_spend_before_action: float = Field(default=0.90, gt=0)
    min_video_2s_rate: float = Field(default=0.20, gt=0, le=1)
    min_video_6s_rate: float = Field(default=0.06, gt=0, le=1)
    min_video_impressions_before_action: int = Field(default=150, ge=50, le=1000000)
    min_video_spend_before_action: float = Field(default=0.75, gt=0)
    qualified_click_override_ctr: float = Field(default=0.04, gt=0, le=1)
    qualified_click_override_cpc: float = Field(default=0.30, gt=0)
    min_runtime_minutes: int = Field(default=0, ge=0, le=1440)
    pause_minutes: int = Field(default=60, ge=15, le=10080)


class WebsiteAdLaunchRequest(BaseModel):
    request_key: str | None = Field(default=None, max_length=64)
    advertiser_id: str | None = None
    landing_page_id: int
    campaign_name: str = Field(min_length=1, max_length=512)
    adgroup_name: str = Field(min_length=1, max_length=512)
    ad_name: str = Field(min_length=1, max_length=512)
    pixel_id: str = Field(min_length=1, max_length=64)
    identity_type: Literal["CUSTOMIZED_USER", "AUTH_CODE", "TT_USER", "BC_AUTH_TT"]
    identity_id: str = Field(min_length=1, max_length=128)
    identity_authorized_bc_id: str | None = Field(default=None, max_length=128)
    video_id: str = Field(min_length=1, max_length=128)
    image_ids: list[str] = Field(default_factory=list, max_length=10)
    ad_text: str = Field(min_length=1, max_length=100)
    call_to_action: str = Field(default="SHOP_NOW", max_length=64)
    daily_budget: float = Field(gt=0)
    budget_mode: Literal["BUDGET_MODE_DAY", "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"] = "BUDGET_MODE_DAY"
    bid_strategy: Literal["LOWEST_COST", "COST_CAP"] = "LOWEST_COST"
    conversion_bid_price: float | None = Field(default=None, gt=0)
    schedule_start_time: datetime | None = None
    targeting: TargetingConfig
    guard: GuardConfig = Field(default_factory=GuardConfig)
    activate_after_create: bool = False

    @model_validator(mode="after")
    def validate_bid(self):
        if self.bid_strategy == "COST_CAP" and self.conversion_bid_price is None:
            raise ValueError("conversion_bid_price is required for COST_CAP")
        if self.identity_type == "BC_AUTH_TT" and not self.identity_authorized_bc_id:
            raise ValueError("identity_authorized_bc_id is required for BC_AUTH_TT")
        return self


class StatusUpdateRequest(BaseModel):
    operation_status: Literal["ENABLE", "DISABLE"]
    reason: str | None = Field(default=None, max_length=1024)


class AdGroupDeliveryUpdateRequest(BaseModel):
    daily_budget: float | None = Field(default=None, gt=0)
    conversion_bid_price: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_change(self):
        if self.daily_budget is None and self.conversion_bid_price is None:
            raise ValueError("At least one delivery setting is required")
        return self


class VideoUploadByUrlRequest(BaseModel):
    video_url: HttpUrl
    file_name: str = Field(min_length=1, max_length=128)
    flaw_detect: bool = False
    auto_fix_enabled: bool = False


class TargetingSearchRequest(BaseModel):
    advertiser_id: str | None = None
    targeting_type: str = "INTEREST_AND_BEHAVIOR"
    search_keywords: list[str] = Field(default_factory=list, max_length=20)


class LocationSearchRequest(BaseModel):
    advertiser_id: str | None = None
    search_keyword: str = Field(min_length=1, max_length=128)
    objective_type: str = "WEB_CONVERSIONS"
    promotion_type: str = "WEBSITE"
    region_codes: list[str] = Field(default_factory=list)
    geo_types: list[str] = Field(default_factory=list)


class WebsiteAdsListQuery(BaseModel):
    status: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class ManualLandingPageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    landing_url: HttpUrl
    product_id: str | None = Field(default=None, max_length=128)
    reference_price: float | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    image_url: HttpUrl | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class ProductProfileUpdate(BaseModel):
    content_product_id: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    landing_url: HttpUrl | None = None
    reference_price: float | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=8)
    image_url: HttpUrl | None = None
    seller_profile: str | None = Field(default=None, max_length=8000)
    promotion_text: str | None = Field(default=None, max_length=8000)
    product_details: str | None = Field(default=None, max_length=20000)

    model_config = ConfigDict(str_strip_whitespace=True)


class CreativeAssetUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    landing_page_id: int | None = None
    user_notes: str | None = Field(default=None, max_length=8000)
    tags: list[str] | None = Field(default=None, max_length=50)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return list(dict.fromkeys(str(value).strip()[:64] for value in values if str(value).strip()))


class MediaPlanGenerateRequest(BaseModel):
    landing_page_id: int
    creative_asset_ids: list[int] | None = Field(default=None, max_length=30)
    daily_budget: float = Field(ge=60, le=100000)
    activate_after_create: bool = False
    request_notes: str | None = Field(default=None, max_length=4000)

    @field_validator("creative_asset_ids")
    @classmethod
    def normalize_asset_ids(cls, values: list[int] | None) -> list[int] | None:
        if values is None:
            return None
        return list(dict.fromkeys(int(value) for value in values))

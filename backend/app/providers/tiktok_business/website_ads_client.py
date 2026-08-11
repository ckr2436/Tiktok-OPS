from __future__ import annotations

import json
from typing import Any, Mapping

from app.core.config import settings
from app.providers.tiktok_business.website_ads_pagination import (
    DEFAULT_WEBSITE_ADS_MAX_PAGES,
    WebsiteAdsPaginationInvariantError,
    collect_all_numbered_pages,
    merge_numbered_pages,
)
from app.services.ttb_api import TTBApiClient
from app.services.ttb_api import TTBBusinessError


INTEGRATED_REPORT_FILTER_ID_LIMIT = 99
REPORT_PAGE_SIZE = 1000


def _resource_identifier(row: Any, *fields: str) -> Any:
    if not isinstance(row, Mapping):
        return None
    for field in fields:
        value = row.get(field)
        if value is not None and (not isinstance(value, str) or value.strip()):
            return value
    return None


def _pixel_item_key(row: Any) -> Any:
    return _resource_identifier(row, "pixel_id", "pixel_code", "id")


def _identity_item_key(row: Any) -> Any:
    return _resource_identifier(row, "identity_id", "id")


def _video_item_key(row: Any) -> Any:
    return _resource_identifier(row, "video_id", "material_id", "id")


def _spark_video_item_key(row: Any) -> Any:
    item_id = _resource_identifier(row, "item_id", "video_id", "id")
    if item_id is not None:
        return item_id
    if isinstance(row, Mapping):
        return _resource_identifier(row.get("item_info"), "item_id", "video_id", "id")
    return None


class TikTokWebsiteAdsClient:
    """Thin typed boundary for TikTok Business API v1.3 Website Ads endpoints."""

    def __init__(self, api: TTBApiClient) -> None:
        self._api = api

    async def aclose(self) -> None:
        await self._api.aclose()

    async def create_campaign(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self._api._request_json("POST", "/campaign/create/", json_body=dict(body))

    async def create_adgroup(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self._api._request_json("POST", "/adgroup/create/", json_body=dict(body))

    async def create_ads(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self._api._request_json("POST", "/ad/create/", json_body=dict(body))

    async def update_campaign(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self._api._request_json("POST", "/campaign/update/", json_body=dict(body))

    async def update_adgroup(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self._api._request_json("POST", "/adgroup/update/", json_body=dict(body))

    async def update_adgroup_budget(self, advertiser_id: str, adgroup_id: str, budget: float) -> dict[str, Any]:
        return await self._api._request_json(
            "POST",
            "/adgroup/budget/update/",
            json_body={
                "advertiser_id": advertiser_id,
                "budget": [{"adgroup_id": adgroup_id, "budget": budget}],
            },
        )

    async def update_ads(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self._api._request_json("POST", "/ad/update/", json_body=dict(body))

    async def upload_video_by_url(
        self,
        advertiser_id: str,
        video_url: str,
        file_name: str,
        *,
        flaw_detect: bool = False,
        auto_fix_enabled: bool = False,
    ) -> dict[str, Any]:
        return await self._api._request_json(
            "POST",
            "/file/video/ad/upload/",
            json_body={
                "advertiser_id": advertiser_id,
                "file_name": file_name,
                "upload_type": "UPLOAD_BY_URL",
                "video_url": video_url,
                "flaw_detect": flaw_detect,
                "auto_fix_enabled": auto_fix_enabled,
            },
        )

    async def upload_video_file(
        self,
        advertiser_id: str,
        file_name: str,
        video_file: Any,
        video_signature: str,
        *,
        content_type: str = "video/mp4",
        flaw_detect: bool = False,
        auto_fix_enabled: bool = False,
    ) -> dict[str, Any]:
        return await self._api._request_json(
            "POST",
            "/file/video/ad/upload/",
            multipart_body={
                "advertiser_id": advertiser_id,
                "file_name": file_name,
                "upload_type": "UPLOAD_BY_FILE",
                "video_signature": video_signature,
                "flaw_detect": flaw_detect,
                "auto_fix_enabled": auto_fix_enabled,
            },
            multipart_files={
                "video_file": (file_name, video_file, content_type),
            },
            request_timeout=float(settings.WEBSITE_ADS_VIDEO_UPLOAD_TIMEOUT_SECONDS),
        )

    async def update_campaign_status(self, advertiser_id: str, campaign_ids: list[str], status: str) -> dict[str, Any]:
        return await self._api._request_json(
            "POST",
            "/campaign/status/update/",
            json_body={"advertiser_id": advertiser_id, "campaign_ids": campaign_ids, "operation_status": status},
        )

    async def update_adgroup_status(self, advertiser_id: str, adgroup_ids: list[str], status: str) -> dict[str, Any]:
        return await self._api._request_json(
            "POST",
            "/adgroup/status/update/",
            json_body={"advertiser_id": advertiser_id, "adgroup_ids": adgroup_ids, "operation_status": status},
        )

    async def update_ad_status(self, advertiser_id: str, ad_ids: list[str], status: str) -> dict[str, Any]:
        return await self._api._request_json(
            "POST",
            "/ad/status/update/",
            json_body={"advertiser_id": advertiser_id, "ad_ids": ad_ids, "operation_status": status},
        )

    async def update_smart_campaign_status(
        self, advertiser_id: str, campaign_ids: list[str], status: str
    ) -> dict[str, Any]:
        return await self._api._request_json(
            "POST",
            "/smart_plus/campaign/status/update/",
            json_body={"advertiser_id": advertiser_id, "campaign_ids": campaign_ids, "operation_status": status},
        )

    async def update_smart_adgroup_status(
        self, advertiser_id: str, adgroup_ids: list[str], status: str
    ) -> dict[str, Any]:
        return await self._api._request_json(
            "POST",
            "/smart_plus/adgroup/status/update/",
            json_body={"advertiser_id": advertiser_id, "adgroup_ids": adgroup_ids, "operation_status": status},
        )

    async def update_smart_ad_status(
        self, advertiser_id: str, smart_plus_ad_ids: list[str], status: str
    ) -> dict[str, Any]:
        return await self._api._request_json(
            "POST",
            "/smart_plus/ad/status/update/",
            json_body={
                "advertiser_id": advertiser_id,
                "smart_plus_ad_ids": smart_plus_ad_ids,
                "operation_status": status,
            },
        )

    async def list_pixels(
        self,
        advertiser_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return await self._api._request_json(
            "GET",
            "/pixel/list/",
            params={
                "advertiser_id": advertiser_id,
                "page": max(1, int(page)),
                "page_size": min(20, max(1, int(page_size))),
            },
        )

    async def list_all_pixels(
        self,
        advertiser_id: str,
        *,
        max_pages: int = DEFAULT_WEBSITE_ADS_MAX_PAGES,
    ) -> dict[str, Any]:
        page_size = 20
        pages = await collect_all_numbered_pages(
            lambda page: self.list_pixels(advertiser_id, page=page, page_size=page_size),
            endpoint="/pixel/list/",
            list_key="pixels",
            requested_page_size=page_size,
            item_key=_pixel_item_key,
            max_pages=max_pages,
        )
        return merge_numbered_pages(pages, list_key="pixels")

    async def list_identities(
        self,
        advertiser_id: str,
        *,
        page: int = 1,
        page_size: int = 100,
        identity_type: str | None = None,
        identity_authorized_bc_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "advertiser_id": advertiser_id,
            "page": max(1, int(page)),
            "page_size": min(100, max(1, int(page_size))),
        }
        if identity_type:
            params["identity_type"] = identity_type
        if identity_authorized_bc_id:
            params["identity_authorized_bc_id"] = identity_authorized_bc_id
        return await self._api._request_json("GET", "/identity/get/", params=params)

    async def list_all_identities(
        self,
        advertiser_id: str,
        *,
        identity_type: str | None = None,
        identity_authorized_bc_id: str | None = None,
        max_pages: int = DEFAULT_WEBSITE_ADS_MAX_PAGES,
    ) -> dict[str, Any]:
        # TikTok's official contract says page_info is correct only when
        # identity_type is specified. Without it, negative terminal metadata
        # is advisory and a real empty page is required to prove completion.
        page_size = 100
        pages = await collect_all_numbered_pages(
            lambda page: self.list_identities(
                advertiser_id,
                page=page,
                page_size=page_size,
                identity_type=identity_type,
                identity_authorized_bc_id=identity_authorized_bc_id,
            ),
            endpoint="/identity/get/",
            list_key="identity_list",
            requested_page_size=page_size,
            item_key=_identity_item_key,
            max_pages=max_pages,
            trust_terminal_signals=bool(identity_type),
        )
        return merge_numbered_pages(pages, list_key="identity_list")

    async def list_videos(self, advertiser_id: str, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        return await self._api._request_json(
            "GET",
            "/file/video/ad/search/",
            params={
                "advertiser_id": advertiser_id,
                "page": max(1, int(page)),
                "page_size": min(100, max(1, int(page_size))),
            },
        )

    async def list_all_videos(
        self,
        advertiser_id: str,
        *,
        max_pages: int = DEFAULT_WEBSITE_ADS_MAX_PAGES,
    ) -> dict[str, Any]:
        page_size = 100
        pages = await collect_all_numbered_pages(
            lambda page: self.list_videos(advertiser_id, page=page, page_size=page_size),
            endpoint="/file/video/ad/search/",
            list_key="list",
            requested_page_size=page_size,
            item_key=_video_item_key,
            max_pages=max_pages,
        )
        return merge_numbered_pages(pages, list_key="list")

    async def list_spark_videos(
        self,
        advertiser_id: str,
        *,
        page: int = 1,
        page_size: int = 50,
        item_types: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._api._request_json(
            "GET",
            "/tt_video/list/",
            params={
                "advertiser_id": advertiser_id,
                "item_types": json.dumps(item_types or ["VIDEO"]),
                "page": max(1, int(page)),
                "page_size": min(50, max(1, int(page_size))),
            },
        )

    async def list_all_spark_videos(
        self,
        advertiser_id: str,
        *,
        max_pages: int = DEFAULT_WEBSITE_ADS_MAX_PAGES,
    ) -> list[dict[str, Any]]:
        page_size = 50
        pages = await collect_all_numbered_pages(
            lambda page: self.list_spark_videos(
                advertiser_id,
                page=page,
                page_size=page_size,
            ),
            endpoint="/tt_video/list/",
            list_key="list",
            requested_page_size=page_size,
            item_key=_spark_video_item_key,
            max_pages=max_pages,
        )
        return [page.response for page in pages]

    async def suggest_video_covers(self, advertiser_id: str, video_id: str) -> dict[str, Any]:
        return await self._api._request_json(
            "GET",
            "/file/video/suggestcover/",
            params={"advertiser_id": advertiser_id, "video_id": video_id},
        )

    async def search_targeting(
        self,
        advertiser_id: str,
        targeting_type: str,
        search_keywords: list[str],
        *,
        sub_targeting_types: list[str] | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"advertiser_id": advertiser_id, "targeting_type": targeting_type}
        if search_keywords:
            params["search_keywords"] = json.dumps(search_keywords, ensure_ascii=False)
        if sub_targeting_types:
            params["sub_targeting_types"] = json.dumps(sub_targeting_types, ensure_ascii=False)
        if language:
            params["language"] = language
        return await self._api._request_json("GET", "/targeting/search/", params=params)

    async def list_interest_categories(
        self,
        advertiser_id: str,
        *,
        version: int = 2,
        language: str = "en",
        placements: list[str] | None = None,
        special_industries: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "advertiser_id": advertiser_id,
            "version": int(version),
            "language": language,
        }
        if placements:
            params["placements"] = json.dumps(placements, ensure_ascii=False)
        if special_industries:
            params["special_industries"] = json.dumps(special_industries, ensure_ascii=False)
        return await self._api._request_json("GET", "/tool/interest_category/", params=params)

    async def search_locations(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self._api._request_json("POST", "/tool/targeting/search/", json_body=dict(body))

    async def estimate_audience_size(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Estimate the reach of one proposed ad-group targeting definition."""
        return await self._api._request_json(
            "POST",
            "/ad/audience_size/estimate/",
            json_body=dict(body),
        )

    async def get_campaigns(
        self,
        advertiser_id: str,
        campaign_ids: list[str],
    ) -> dict[str, Any]:
        normalized_ids = list(
            dict.fromkeys(
                str(campaign_id).strip()
                for campaign_id in campaign_ids
                if campaign_id is not None and str(campaign_id).strip()
            )
        )
        if not normalized_ids:
            raise ValueError("campaign_ids must contain at least one campaign ID")
        if len(normalized_ids) > 100:
            raise ValueError("campaign_ids cannot contain more than 100 IDs")
        return await self._api._request_json(
            "GET",
            "/campaign/get/",
            params={
                "advertiser_id": advertiser_id,
                "filtering": json.dumps(
                    {
                        "campaign_ids": normalized_ids,
                        # TikTok excludes deleted objects by default.  A status
                        # reconciliation must include terminal rows so a
                        # deleted campaign cannot remain locally ACTIVE.
                        "primary_status": "STATUS_ALL",
                    }
                ),
                "page": 1,
                "page_size": len(normalized_ids),
            },
        )

    async def get_ads(self, advertiser_id: str, ad_ids: list[str]) -> dict[str, Any]:
        normalized_ids = list(
            dict.fromkeys(
                str(ad_id).strip()
                for ad_id in ad_ids
                if ad_id is not None and str(ad_id).strip()
            )
        )
        if not normalized_ids:
            raise ValueError("ad_ids must contain at least one ad ID")
        if len(normalized_ids) > 100:
            raise ValueError("ad_ids cannot contain more than 100 IDs")
        return await self._api._request_json(
            "GET",
            "/ad/get/",
            params={
                "advertiser_id": advertiser_id,
                "filtering": json.dumps(
                    {
                        "ad_ids": normalized_ids,
                        # Deleted ads are hidden unless STATUS_ALL is explicit.
                        # Their operation_status can still be ENABLE, so the
                        # secondary deletion status must remain observable.
                        "primary_status": "STATUS_ALL",
                    }
                ),
                "page": 1,
                "page_size": len(normalized_ids),
            },
        )

    async def report_ads(
        self,
        advertiser_id: str,
        ad_ids_v2: list[str],
        start_date: str,
        end_date: str,
        *,
        hourly: bool,
    ) -> dict[str, Any]:
        dimensions = ["ad_id_v2", "stat_time_hour" if hourly else "stat_time_day"]
        delivery_metrics = [
            "spend",
            "impressions",
            "clicks",
            "cpc",
            "cpm",
            "ctr",
        ]
        video_metrics = [
            "video_play_actions",
            "video_watched_2s",
            "video_watched_6s",
            "video_views_p25",
            "video_views_p50",
            "video_views_p75",
            "video_views_p100",
            "average_video_play",
        ]
        conversion_metrics = [
            "conversion",
            "cost_per_conversion",
            "conversion_rate",
            "total_purchase_value",
        ]
        normalized_ad_ids = list(
            dict.fromkeys(
                str(ad_id).strip()
                for ad_id in ad_ids_v2
                if ad_id is not None and str(ad_id).strip()
            )
        )
        if not normalized_ad_ids:
            raise ValueError("ad_ids_v2 must contain at least one ad ID")
        ad_id_chunks = [
            normalized_ad_ids[
                offset : offset + INTEGRATED_REPORT_FILTER_ID_LIMIT
            ]
            for offset in range(
                0,
                len(normalized_ad_ids),
                INTEGRATED_REPORT_FILTER_ID_LIMIT,
            )
        ]
        base_params = {
            "advertiser_id": advertiser_id,
            "service_type": "AUCTION",
            "report_type": "BASIC",
            "data_level": "AUCTION_AD",
            "dimensions": json.dumps(dimensions),
            "start_date": start_date,
            "end_date": end_date,
            "page_size": REPORT_PAGE_SIZE,
        }
        attempts = (
            (delivery_metrics + video_metrics + conversion_metrics, "conversion_video"),
            (delivery_metrics + video_metrics, "video"),
            (delivery_metrics + conversion_metrics, "conversion"),
            (delivery_metrics, "delivery_only"),
        )
        last_error: Exception | None = None
        for metrics, fidelity in attempts:
            chunk_payloads: list[dict[str, Any]] = []
            try:
                for ad_id_chunk in ad_id_chunks:
                    params = dict(base_params)
                    params["metrics"] = json.dumps(metrics)
                    params["filtering"] = json.dumps(
                        [
                            {
                                "field_name": "ad_ids_v2",
                                "filter_type": "IN",
                                "filter_value": json.dumps(ad_id_chunk),
                            }
                        ]
                    )
                    chunk_payloads.append(await self._report_all_pages(params))
                payload = self._merge_report_chunks(chunk_payloads)
                payload["_metric_fidelity"] = fidelity
                return payload
            except TTBBusinessError as exc:
                # Metric fallback is valid only for an official parameter
                # rejection. Authentication, quota, transport, and server
                # errors must surface immediately; retrying four metric sets
                # multiplies load and can turn one quota error into a storm.
                if str(getattr(exc, "code", "")) != "40002":
                    raise
                last_error = exc
        if last_error:
            raise last_error
        raise RuntimeError("TikTok Website Ads report did not return a result")

    async def _report_all_pages(self, params: dict[str, Any]) -> dict[str, Any]:
        requested_page_size = max(1, int(params.get("page_size") or REPORT_PAGE_SIZE))
        pages = await collect_all_numbered_pages(
            lambda page: self._api._request_json(
                "GET",
                "/report/integrated/get/",
                params={**params, "page": page},
            ),
            endpoint="/report/integrated/get/",
            list_key="list",
            requested_page_size=requested_page_size,
            max_pages=DEFAULT_WEBSITE_ADS_MAX_PAGES,
        )

        rows_by_dimension: dict[str, dict[str, Any]] = {}
        for page in pages:
            for index, row in enumerate(page.rows):
                if not isinstance(row, dict):
                    raise WebsiteAdsPaginationInvariantError(
                        "/report/integrated/get/ returned a non-object report row "
                        f"on page {page.page} at index {index}"
                    )
                dimensions = row.get("dimensions")
                if isinstance(dimensions, dict) and dimensions:
                    row_key = json.dumps(dimensions, sort_keys=True, separators=(",", ":"))
                else:
                    raise WebsiteAdsPaginationInvariantError(
                        "/report/integrated/get/ returned a report row without "
                        f"canonical dimensions on page {page.page} at index {index}"
                    )
                if row_key in rows_by_dimension:
                    raise WebsiteAdsPaginationInvariantError(
                        "/report/integrated/get/ returned duplicate dimensions "
                        f"across pages: {row_key}"
                    )
                rows_by_dimension[row_key] = row

        merged = merge_numbered_pages(pages, list_key="list")
        merged_data = dict(merged.get("data") or {})
        merged_rows = list(rows_by_dimension.values())
        merged_data["list"] = merged_rows
        merged_data["page_info"] = {
            "page": 1,
            "page_size": len(merged_rows),
            "total_number": len(merged_rows),
            "total_page": 1,
        }
        merged["data"] = merged_data
        merged.pop("_website_ads_pagination", None)
        merged["_report_pagination"] = {
            "pages_fetched": len(pages),
            "rows_returned": len(merged_rows),
            "source_pages": [dict(page.page_info) for page in pages],
        }
        return merged

    @staticmethod
    def _merge_report_chunks(payloads: list[dict[str, Any]]) -> dict[str, Any]:
        if not payloads:
            raise WebsiteAdsPaginationInvariantError(
                "/report/integrated/get/ did not receive any report chunks"
            )

        merged = dict(payloads[0])
        rows_by_dimension: dict[str, dict[str, Any]] = {}
        source_pages: list[dict[str, Any]] = []
        pages_fetched = 0
        for chunk_index, payload in enumerate(payloads):
            data = payload.get("data")
            if not isinstance(data, Mapping):
                raise WebsiteAdsPaginationInvariantError(
                    "/report/integrated/get/ returned invalid merged chunk data"
                )
            rows = data.get("list")
            if not isinstance(rows, list):
                raise WebsiteAdsPaginationInvariantError(
                    "/report/integrated/get/ returned invalid merged chunk rows"
                )
            for row_index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise WebsiteAdsPaginationInvariantError(
                        "/report/integrated/get/ returned a non-object merged "
                        f"report row in chunk {chunk_index} at index {row_index}"
                    )
                dimensions = row.get("dimensions")
                if isinstance(dimensions, dict) and dimensions:
                    row_key = json.dumps(dimensions, sort_keys=True, separators=(",", ":"))
                else:
                    raise WebsiteAdsPaginationInvariantError(
                        "/report/integrated/get/ returned a merged report row "
                        "without canonical dimensions "
                        f"in chunk {chunk_index} at index {row_index}"
                    )
                if row_key in rows_by_dimension:
                    raise WebsiteAdsPaginationInvariantError(
                        "/report/integrated/get/ returned duplicate dimensions "
                        f"across ID chunks: {row_key}"
                    )
                rows_by_dimension[row_key] = row

            pagination = payload.get("_report_pagination")
            if not isinstance(pagination, Mapping):
                raise WebsiteAdsPaginationInvariantError(
                    "/report/integrated/get/ omitted chunk pagination evidence"
                )
            pages_fetched += int(pagination.get("pages_fetched") or 0)
            chunk_source_pages = pagination.get("source_pages")
            if isinstance(chunk_source_pages, list):
                source_pages.extend(
                    dict(page_info)
                    for page_info in chunk_source_pages
                    if isinstance(page_info, Mapping)
                )

        merged_rows = list(rows_by_dimension.values())
        merged_data = dict(merged.get("data") or {})
        merged_data["list"] = merged_rows
        merged_data["page_info"] = {
            "page": 1,
            "page_size": len(merged_rows),
            "total_number": len(merged_rows),
            "total_page": 1,
        }
        merged["data"] = merged_data
        merged["_report_pagination"] = {
            "chunks_fetched": len(payloads),
            "pages_fetched": pages_fetched,
            "rows_returned": len(merged_rows),
            "source_pages": source_pages,
        }
        return merged

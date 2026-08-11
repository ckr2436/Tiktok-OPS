from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopAccount
from app.services.oauth_tiktok_shop import (
    _account_credentials,
    _decrypt_app_secret,
    refresh_account_token,
    sign_api_request,
)


logger = logging.getLogger("gmv.tiktok_shop.creator_api")
CREATOR_VIDEO_SCOPE = "creator.video.write"
_TRANSIENT_CODES = {36009003, 38007001}
_TOKEN_CODES = {105001, 105002}
_FREQUENCY_CODES = {170001020, 170001060, 170001061, 170001062}
_NON_RETRYABLE_CODES = {
    105005,
    101000,
    16011007,
    16011009,
    16011069,
    170001002,
    170001003,
    170001004,
    170001005,
    170001006,
    170001007,
    170001008,
    170001009,
    170001010,
    170001011,
    170001012,
    170001013,
    170001014,
    170001015,
    170001018,
    170001024,
    170001025,
    170001030,
    170001031,
    170001040,
}


def _compact_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else ""


def _provider_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("code"))
    except (TypeError, ValueError):
        return None


def _request_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = str(payload.get("request_id") or payload.get("requestId") or "").strip()
    return value[:128] or None


def _message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "TikTok Shop returned an invalid response."
    return str(payload.get("message") or payload.get("msg") or "TikTok Shop request failed.").replace(
        "\r", " "
    ).replace("\n", " ")[:512]


@dataclass(slots=True)
class CreatorAPIResult:
    data: Any
    request_id: str | None
    provider_code: int = 0


class TikTokShopCreatorAPIClient:
    """Signed Creator OpenAPI client; it never accepts a seller token or shop cipher."""

    def __init__(
        self,
        *,
        db: Session,
        workspace_id: int,
        account: OAuthTikTokShopAccount,
        app_key: str,
        app_secret: str,
        access_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.db = db
        self.workspace_id = int(workspace_id)
        self.account = account
        self.app_key = str(app_key)
        self.app_secret = str(app_secret)
        self.access_token = str(access_token)
        self.base_url = str(settings.TT_SHOP_API_BASE).rstrip("/")
        self._http_client = http_client
        self._owns_http_client = http_client is None

    @classmethod
    async def create(
        cls,
        db: Session,
        *,
        workspace_id: int,
        account_id: int,
        http_client: httpx.AsyncClient | None = None,
    ) -> "TikTokShopCreatorAPIClient":
        account = await refresh_account_token(
            db,
            workspace_id=int(workspace_id),
            account_id=int(account_id),
            force=False,
        )
        account, app, access_token, _ = _account_credentials(
            db,
            workspace_id=int(workspace_id),
            account_id=int(account.id),
        )
        scopes = {str(value) for value in (account.granted_scopes_json or [])}
        if account.status != "active":
            raise APIError("CREATOR_AUTHORIZATION_INACTIVE", "Creator authorization is not active.", 409)
        if account.user_type != 1:
            raise APIError(
                "CREATOR_TOKEN_REQUIRED",
                "Content Posting requires a creator token (user_type=1), not a seller token.",
                409,
            )
        if CREATOR_VIDEO_SCOPE not in scopes:
            raise APIError(
                "CREATOR_VIDEO_SCOPE_REQUIRED",
                "Creator authorization is missing creator.video.write. Re-authorize the creator account.",
                409,
                data={"required_scope": CREATOR_VIDEO_SCOPE},
            )
        return cls(
            db=db,
            workspace_id=int(workspace_id),
            account=account,
            app_key=str(app.client_id),
            app_secret=_decrypt_app_secret(app),
            access_token=access_token,
            http_client=http_client,
        )

    async def __aenter__(self) -> "TikTokShopCreatorAPIClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
        self._http_client = None

    async def _client(self, *, upload: bool = False) -> httpx.AsyncClient:
        if self._http_client is None:
            total = 180.0 if upload else float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 25.0))
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(total, connect=15.0),
                http2=True,
            )
        return self._http_client

    async def _force_refresh(self) -> None:
        account = await refresh_account_token(
            self.db,
            workspace_id=self.workspace_id,
            account_id=int(self.account.id),
            force=True,
        )
        account, _app, access_token, _ = _account_credentials(
            self.db,
            workspace_id=self.workspace_id,
            account_id=int(account.id),
        )
        if account.user_type != 1 or CREATOR_VIDEO_SCOPE not in set(account.granted_scopes_json or []):
            raise APIError(
                "CREATOR_REAUTHORIZATION_REQUIRED",
                "The refreshed token no longer grants creator.video.write.",
                409,
            )
        self.account = account
        self.access_token = access_token

    @staticmethod
    def _delay(attempt: int, retry_after: str | None) -> float:
        try:
            requested = float(retry_after or 0)
        except ValueError:
            requested = 0.0
        if requested > 0:
            return min(requested, 30.0)
        return min(0.75 * (2 ** (attempt - 1)) + random.uniform(0.05, 0.2), 8.0)

    def _params(self, path: str, *, body_text: str = "", multipart: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"app_key": self.app_key, "timestamp": int(time.time())}
        params["sign"] = sign_api_request(
            path=path,
            params=params,
            app_secret=self.app_secret,
            body=body_text,
            multipart=multipart,
        )
        return params

    @staticmethod
    def _api_error(*, response: httpx.Response, payload: Any) -> APIError:
        code = _provider_code(payload)
        safe_code = code if code is not None else response.status_code
        request_id = _request_id(payload)
        transient = response.status_code == 429 or response.status_code >= 500 or code in _TRANSIENT_CODES
        if code in _FREQUENCY_CODES or response.status_code == 429:
            status_code = 429
        elif code in _NON_RETRYABLE_CODES:
            status_code = 422
        else:
            status_code = 502
        return APIError(
            "TIKTOK_SHOP_CREATOR_API_ERROR",
            _message(payload),
            status_code,
            data={
                "http_status": response.status_code,
                "provider_code": safe_code,
                "request_id": request_id,
                "retryable": transient,
            },
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        max_attempts: int = 4,
    ) -> CreatorAPIResult:
        method = str(method).upper()
        body_text = _compact_json(body)
        max_attempts = max(1, min(int(max_attempts), 5))
        refreshed = False
        for attempt in range(1, max_attempts + 1):
            params = self._params(path, body_text=body_text)
            params.update({str(k): v for k, v in dict(query or {}).items() if v is not None})
            # The signature must include endpoint-specific query parameters.
            params.pop("sign", None)
            params["sign"] = sign_api_request(
                path=path,
                params=params,
                app_secret=self.app_secret,
                body=body_text,
            )
            try:
                response = await (await self._client()).request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    content=body_text if body is not None else None,
                    headers={
                        "accept": "application/json",
                        "content-type": "application/json",
                        "x-tts-access-token": self.access_token,
                    },
                )
            except httpx.RequestError as exc:
                if attempt >= max_attempts:
                    raise APIError(
                        "TIKTOK_SHOP_CREATOR_UNAVAILABLE",
                        "TikTok Shop Creator API is temporarily unavailable.",
                        502,
                        data={"transport_error": type(exc).__name__, "retryable": True},
                    ) from exc
                await asyncio.sleep(self._delay(attempt, None))
                continue
            try:
                payload: Any = response.json()
            except ValueError:
                payload = None
            code = _provider_code(payload)
            if (response.status_code == 401 or code in _TOKEN_CODES) and not refreshed:
                refreshed = True
                await self._force_refresh()
                continue
            transient = response.status_code == 429 or response.status_code >= 500 or code in _TRANSIENT_CODES
            if transient and attempt < max_attempts:
                await asyncio.sleep(self._delay(attempt, response.headers.get("retry-after")))
                continue
            if response.status_code >= 400 or code != 0:
                error = self._api_error(response=response, payload=payload)
                logger.warning(
                    "TikTok Shop Creator API failed method=%s path=%s provider_code=%s request_id=%s",
                    method,
                    path,
                    (error.data or {}).get("provider_code") if isinstance(error.data, dict) else None,
                    (error.data or {}).get("request_id") if isinstance(error.data, dict) else None,
                )
                raise error
            if not isinstance(payload, dict):
                raise APIError("TIKTOK_SHOP_CREATOR_INVALID_RESPONSE", "TikTok Shop returned invalid JSON.", 502)
            return CreatorAPIResult(data=payload.get("data"), request_id=_request_id(payload))
        raise APIError("TIKTOK_SHOP_CREATOR_UNAVAILABLE", "TikTok Shop Creator API is unavailable.", 502)

    async def upload_video_file(
        self,
        *,
        file_name: str,
        file_obj: BinaryIO,
        media_type: str | None,
    ) -> CreatorAPIResult:
        path = "/affiliate_creator/202505/videos/video_files"
        params = self._params(path, multipart=True)
        try:
            file_obj.seek(0)
            response = await (await self._client(upload=True)).post(
                f"{self.base_url}{path}",
                params=params,
                headers={"accept": "application/json", "x-tts-access-token": self.access_token},
                files={"data": (Path(file_name).name, file_obj, media_type or "application/octet-stream")},
            )
        except httpx.RequestError as exc:
            raise APIError(
                "TIKTOK_SHOP_UPLOAD_UNCERTAIN",
                "The video upload result is uncertain. The system will not retry automatically.",
                502,
                data={"transport_error": type(exc).__name__, "retryable": False},
            ) from exc
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400 or _provider_code(payload) != 0:
            raise self._api_error(response=response, payload=payload)
        if not isinstance(payload, dict):
            raise APIError("TIKTOK_SHOP_CREATOR_INVALID_RESPONSE", "TikTok Shop returned invalid JSON.", 502)
        return CreatorAPIResult(data=payload.get("data"), request_id=_request_id(payload))

    async def profile(self) -> CreatorAPIResult:
        return await self.request("GET", "/affiliate_creator/202508/profiles")

    async def shop_products(self, **query: Any) -> CreatorAPIResult:
        return await self.request("GET", "/affiliate_creator/202509/shop_products", query=query)

    async def showcase_products(self, **query: Any) -> CreatorAPIResult:
        return await self.request("GET", "/affiliate_creator/202405/showcases/products", query=query)

    async def add_showcase_products(self, product_ids: list[str]) -> CreatorAPIResult:
        return await self.request(
            "POST",
            "/affiliate_creator/202405/showcases/products/add",
            body={"add_type": "PRODUCT_ID", "product_ids": product_ids},
            max_attempts=1,
        )

    async def create_precheck(self, body: Mapping[str, Any]) -> CreatorAPIResult:
        return await self.request(
            "POST",
            "/affiliate_creator/202511/videos/precheck_task",
            body=body,
            max_attempts=1,
        )

    async def precheck_status(self, task_id: str) -> CreatorAPIResult:
        return await self.request(
            "GET",
            f"/affiliate_creator/202511/videos/precheck_tasks/{task_id}",
        )

    async def publish_video(self, body: Mapping[str, Any]) -> CreatorAPIResult:
        return await self.request(
            "POST",
            "/affiliate_creator/202603/videos",
            body=body,
            max_attempts=1,
        )

    async def video_status(self, video_id: str) -> CreatorAPIResult:
        return await self.request(
            "GET",
            f"/affiliate_creator/202509/videos/{video_id}/status",
        )


__all__ = ["CREATOR_VIDEO_SCOPE", "CreatorAPIResult", "TikTokShopCreatorAPIClient"]

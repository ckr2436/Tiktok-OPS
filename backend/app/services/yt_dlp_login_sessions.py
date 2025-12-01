from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Optional
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.data.db import SessionLocal
from app.services import video_site_cookies, video_site_login_sessions

logger = logging.getLogger("gmv.ytdlp.login")

SUPPORTED_SITES = {"tiktok", "douyin", "youtube"}

LOGIN_URLS: Dict[str, str] = {
    "tiktok": "https://www.tiktok.com/login",
    "douyin": "https://www.douyin.com/passport/login/qr",
    "youtube": "https://accounts.google.com/ServiceLogin?service=youtube",
}

SESSION_COOKIE_NAMES: Dict[str, set[str]] = {
    "tiktok": {"sessionid", "sessionid_ss", "sid_tt"},
    "douyin": {"sessionid", "sessionid_ss", "passport_csrf_token"},
    "youtube": {"SAPISID", "SSID", "SID", "__Secure-1PSID", "__Secure-3PSID"},
}

DOMAIN_FILTERS: Dict[str, str] = {
    "tiktok": "tiktok.com",
    "douyin": "douyin.com",
    "youtube": "youtube.com",
}

LOGIN_TIMEOUT = timedelta(minutes=3)
LOGIN_SESSION_TTL = video_site_login_sessions.DEFAULT_LOGIN_SESSION_TTL
POLL_INTERVAL = 2.0


class LoginFlowError(Exception):
    """Expected exception for QR/login flow failures."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class LoginSessionSetupError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class LoginSessionState:
    login_session_id: str
    site: str
    label: str
    status: str
    qrcode_image_base64: Optional[str] = None
    account: Optional[Dict[str, Any]] = None
    error_msg: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime | None = None
    _browser: Optional[Browser] = field(default=None, repr=False)
    _context: Optional[BrowserContext] = field(default=None, repr=False)
    _page: Optional[Page] = field(default=None, repr=False)
    _task: Optional[asyncio.Task] = field(default=None, repr=False)
    _playwright: Optional[Any] = field(default=None, repr=False)

    def to_dict(self, include_qr: bool = False) -> Dict[str, Any]:
        data = {
            "login_session_id": self.login_session_id,
            "site": self.site,
            "status": self.status,
            "account": self.account,
            "error_msg": self.error_msg,
        }
        if include_qr:
            data["qrcode_image_base64"] = self.qrcode_image_base64
        return data


class YtDlpLoginSessionManager:
    def __init__(self) -> None:
        self._sessions: Dict[str, LoginSessionState] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, site: str, label: str) -> LoginSessionState:
        site = site.lower()
        if site not in SUPPORTED_SITES:
            raise ValueError(f"Unsupported site: {site}")

        logger.info("starting login session for site=%s label=%s", site, label)
        login_session_id = uuid4().hex
        expires_at = datetime.utcnow() + LOGIN_SESSION_TTL

        browser: Browser | None = None
        context: BrowserContext | None = None
        page: Page | None = None
        playwright = None
        qr_image: str | None = None

        try:
            browser, context, page, qr_image, playwright = await self._prepare_page(site)
        except LoginFlowError as exc:
            logger.warning("login flow failed for site=%s label=%s: %s", site, label, exc)
            raise LoginSessionSetupError(str(exc), status_code=exc.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.exception("unexpected error preparing login page for %s: %s", site, exc)
            raise LoginSessionSetupError(f"login setup failed: {exc}", status_code=502)

        session = LoginSessionState(
            login_session_id=login_session_id,
            site=site,
            label=label,
            status="qrcode_ready",
            qrcode_image_base64=qr_image,
            expires_at=expires_at,
            _browser=browser,
            _context=context,
            _page=page,
            _playwright=playwright,
        )

        with SessionLocal() as db:
            video_site_login_sessions.create_login_session(
                db,
                login_session_id=login_session_id,
                site=site,
                label=label,
                status=session.status,
                qrcode_image_base64=qr_image,
                expires_at=expires_at,
            )
            db.commit()

        async with self._lock:
            self._sessions[login_session_id] = session

        session._task = asyncio.create_task(self._monitor_session(session))
        return session

    def get_session(self, login_session_id: str) -> Optional[LoginSessionState]:
        return self._sessions.get(login_session_id)

    def _persist_session_state(
        self,
        session: LoginSessionState,
        *,
        status: str | None = None,
        account: dict | None = None,
        error_msg: str | None = None,
    ) -> None:
        if status:
            session.status = status
        if account is not None:
            session.account = account
        if error_msg is not None:
            session.error_msg = error_msg

        session.expires_at = datetime.utcnow() + LOGIN_SESSION_TTL
        with SessionLocal() as db:
            video_site_login_sessions.update_login_session(
                db,
                session.login_session_id,
                status=session.status,
                account=session.account,
                error_msg=session.error_msg,
                expires_at=session.expires_at,
            )
            db.commit()

    async def _prepare_page(self, site: str) -> tuple[Browser, BrowserContext, Page, str, Any]:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        try:
            if site == "tiktok":
                qr_image = await self._prepare_tiktok_login(page)
            else:
                try:
                    await page.goto(
                        LOGIN_URLS[site], wait_until="networkidle", timeout=20000
                    )
                except PlaywrightTimeoutError as exc:  # noqa: PERF203
                    raise LoginFlowError(
                        "Login page navigation timed out, please check server network/region",
                        status_code=502,
                    ) from exc
                await self._enter_qr_mode(site, page)
                await page.wait_for_timeout(1200)
                screenshot = await page.screenshot(full_page=True)
                qr_image = f"data:image/png;base64,{base64.b64encode(screenshot).decode()}"
            return browser, context, page, qr_image, playwright
        except Exception:
            await self._close_resources(browser, context, playwright)
            raise

    async def _prepare_tiktok_login(self, page: Page) -> str:
        try:
            response = await page.goto(
                LOGIN_URLS["tiktok"], wait_until="networkidle", timeout=20000
            )
        except PlaywrightTimeoutError as exc:  # noqa: PERF203
            raise LoginFlowError(
                "TikTok QR login timed out, please check server network / region access.",
                status_code=502,
            ) from exc
        status = response.status if response else None
        if not status or status >= 400:
            raise LoginFlowError(
                f"TikTok login page returned HTTP {status}", status_code=502
            )

        await self._tiktok_qr_flow(page)

        qr_locator = await self._wait_for_tiktok_qr(page)
        if not qr_locator:
            raise LoginFlowError(
                "TikTok QR code did not appear in time (maybe region/network/captcha restricted)",
                status_code=502,
            )

        png_bytes = await qr_locator.screenshot(type="png")
        return f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"

    async def _enter_qr_mode(self, site: str, page: Page) -> None:
        handlers: Dict[str, Callable[[Page], Awaitable[None]]] = {
            "douyin": self._douyin_qr_flow,
            "youtube": self._youtube_qr_flow,
        }
        handler = handlers.get(site)
        if handler:
            try:
                await handler(page)
            except Exception as exc:  # noqa: BLE001
                logger.warning("qr flow setup failed for %s: %s", site, exc)

    async def _tiktok_qr_flow(self, page: Page) -> None:
        candidates = [
            page.get_by_role("button", name=re.compile("QR code", re.IGNORECASE)),
            page.get_by_text(re.compile("Use QR code", re.IGNORECASE)),
            page.get_by_text(re.compile("使用二维码登录")),
            page.get_by_text(re.compile("扫码登录")),
        ]

        for locator in candidates:
            try:
                await locator.click(timeout=3000)
                return
            except Exception:
                continue

        raise LoginFlowError(
            "TikTok login page did not expose any QR-code login button", status_code=502
        )

    async def _wait_for_tiktok_qr(self, page: Page):
        locators = [
            page.locator("canvas[data-e2e='qr-code']"),
            page.locator("img[alt*='QR']"),
            page.locator("div[data-e2e='qr-code'] canvas"),
        ]
        for locator in locators:
            try:
                await locator.wait_for(timeout=15000)
                return locator
            except Error:
                continue
        return None

    async def _close_resources(
        self, browser: Browser | None, context: BrowserContext | None, playwright: Any
    ) -> None:
        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        try:
            if playwright:
                await playwright.stop()
        except Exception:
            pass

    async def _douyin_qr_flow(self, page: Page) -> None:
        selectors = ["text=二维码登录", "text=扫码登录", "text=QR"]
        for selector in selectors:
            try:
                element = await page.wait_for_selector(selector, timeout=2000)
                if element:
                    await element.click()
                    return
            except Error:
                continue

    async def _youtube_qr_flow(self, page: Page) -> None:
        try:
            await page.wait_for_timeout(1000)
            await page.wait_for_selector("input[type=email]", timeout=3000)
        except Error:
            return
        try:
            await page.click("text=Use your phone to sign in", timeout=1000)
        except Error:
            pass

    async def _monitor_session(self, session: LoginSessionState) -> None:
        deadline = datetime.utcnow() + LOGIN_TIMEOUT
        try:
            while datetime.utcnow() < deadline:
                await asyncio.sleep(POLL_INTERVAL)
                if not session._context:
                    continue
                cookies = await session._context.cookies()
                if self._is_logged_in(session.site, cookies):
                    await self._handle_success(session, cookies)
                    return
            self._persist_session_state(session, status="expired")
        except Exception as exc:  # noqa: BLE001
            logger.exception("login session failed: %s", exc)
            self._persist_session_state(session, status="failed", error_msg=str(exc))
        finally:
            await self._cleanup_browser(session)
            async with self._lock:
                self._sessions.pop(session.login_session_id, None)

    def _is_logged_in(self, site: str, cookies: list[dict[str, Any]]) -> bool:
        target_names = SESSION_COOKIE_NAMES.get(site, set())
        for cookie in cookies:
            if cookie.get("name") in target_names:
                return True
        return False

    async def _handle_success(self, session: LoginSessionState, cookies: list[dict[str, Any]]) -> None:
        filtered = [
            cookie
            for cookie in cookies
            if DOMAIN_FILTERS.get(session.site, "") in (cookie.get("domain") or "")
        ]
        try:
            with SessionLocal() as db:
                record = video_site_cookies.upsert_video_site_cookies(
                    db,
                    site=session.site,
                    label=session.label,
                    cookies_json=json.dumps(filtered, ensure_ascii=False),
                    is_active=True,
                    last_login_at=datetime.utcnow(),
                )
                db.commit()
                session.account = {
                    "id": record.id,
                    "site": record.site,
                    "label": record.label,
                    "last_login_at": record.last_login_at,
                    "is_active": bool(record.is_active),
                }
            self._persist_session_state(session, status="success", account=session.account)
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed to persist cookies: %s", exc)
            self._persist_session_state(
                session, status="failed", error_msg=f"persist cookies failed: {exc}"
            )

    async def _cleanup_browser(self, session: LoginSessionState) -> None:
        try:
            if session._context:
                await session._context.close()
        except Exception:
            pass
        try:
            if session._browser:
                await session._browser.close()
        except Exception:
            pass
        try:
            if session._playwright:
                await session._playwright.stop()
        except Exception:
            pass


manager = YtDlpLoginSessionManager()

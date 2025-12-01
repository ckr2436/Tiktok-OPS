from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Optional
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Error, Page, async_playwright

from app.data.db import SessionLocal
from app.services import video_site_cookies

logger = logging.getLogger("gmv.ytdlp.login")

SUPPORTED_SITES = {"tiktok", "douyin", "youtube"}

LOGIN_URLS: Dict[str, str] = {
    "tiktok": "https://www.tiktok.com/login/qr",
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
POLL_INTERVAL = 2.0


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
        browser, context, page, qr_image, playwright = await self._prepare_page(site)

        session = LoginSessionState(
            login_session_id=login_session_id,
            site=site,
            label=label,
            status="qrcode_ready",
            qrcode_image_base64=qr_image,
            _browser=browser,
            _context=context,
            _page=page,
            _playwright=playwright,
        )

        async with self._lock:
            self._sessions[login_session_id] = session

        session._task = asyncio.create_task(self._monitor_session(session))
        return session

    def get_session(self, login_session_id: str) -> Optional[LoginSessionState]:
        return self._sessions.get(login_session_id)

    async def _prepare_page(self, site: str) -> tuple[Browser, BrowserContext, Page, str, Any]:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(LOGIN_URLS[site], wait_until="networkidle")
        await self._enter_qr_mode(site, page)
        await page.wait_for_timeout(1200)
        screenshot = await page.screenshot(full_page=True)
        qr_image = f"data:image/png;base64,{base64.b64encode(screenshot).decode()}"
        return browser, context, page, qr_image, playwright

    async def _enter_qr_mode(self, site: str, page: Page) -> None:
        handlers: Dict[str, Callable[[Page], Awaitable[None]]] = {
            "tiktok": self._tiktok_qr_flow,
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
        try:
            await page.wait_for_selector("text=Log in", timeout=5000)
        except Error:
            return
        selectors = ["text=Use QR code", "text=Log in with QR", "text=QR code"]
        for selector in selectors:
            try:
                element = await page.wait_for_selector(selector, timeout=2000)
                if element:
                    await element.click()
                    return
            except Error:
                continue

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
            session.status = "expired"
        except Exception as exc:  # noqa: BLE001
            session.status = "failed"
            session.error_msg = str(exc)
            logger.exception("login session failed: %s", exc)
        finally:
            await self._cleanup_browser(session)

    def _is_logged_in(self, site: str, cookies: list[dict[str, Any]]) -> bool:
        target_names = SESSION_COOKIE_NAMES.get(site, set())
        for cookie in cookies:
            if cookie.get("name") in target_names:
                return True
        return False

    async def _handle_success(self, session: LoginSessionState, cookies: list[dict[str, Any]]) -> None:
        session.status = "success"
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed to persist cookies: %s", exc)
            session.status = "failed"
            session.error_msg = f"persist cookies failed: {exc}"

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

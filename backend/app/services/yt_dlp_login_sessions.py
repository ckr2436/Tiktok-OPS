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
REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
SESSION_COOKIE_NAMES: Dict[str, set[str]] = {
    "tiktok": {
        "sessionid",
        "sessionid_ss",
        "sid_tt",
        "sid_guard",
        "ssid_ucp",
        "sid_ucp_v1",
        "uid_tt",
        "uid_tt_ss",
        "multi_sids",
        "passport_auth_status",
        "passport_auth_status_ss",
    },
    "douyin": {"sessionid", "sessionid_ss", "passport_csrf_token"},
    "youtube": {"SAPISID", "SSID", "SID", "__Secure-1PSID", "__Secure-3PSID"},
}
DOMAIN_FILTERS: Dict[str, str] = {
    "tiktok": "tiktok.com",
    "douyin": "douyin.com",
    "youtube": "youtube.com",
}
COOKIE_URLS: Dict[str, list[str]] = {
    "tiktok": ["https://www.tiktok.com/", "https://m.tiktok.com/", "https://tiktok.com/"],
    "douyin": ["https://www.douyin.com/", "https://douyin.com/"],
    "youtube": ["https://www.youtube.com/", "https://youtube.com/", "https://accounts.google.com/", "https://google.com/"],
}
LOGIN_TIMEOUT = timedelta(minutes=3)
LOGIN_SESSION_TTL = video_site_login_sessions.DEFAULT_LOGIN_SESSION_TTL
POLL_INTERVAL = 2.0
LOGIN_WAITING_STATUS = "waiting_scan"
NAVIGATION_TIMEOUT_MS = 25_000
QR_WAIT_TIMEOUT_MS = 18_000
MAX_DEBUG_LOGS = 80
SNAPSHOT_INTERVAL_SECONDS = 8


def _utc_ts() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _earliest_expiry(cookies: list[dict[str, Any]]) -> datetime | None:
    expiries: list[float] = []
    for cookie in cookies:
        if not cookie.get("expires"):
            continue
        try:
            expires = float(cookie["expires"])
            if expires > 0:
                expiries.append(expires)
        except (TypeError, ValueError):
            continue
    if not expiries:
        return None
    return datetime.utcfromtimestamp(min(expiries))


def _dedupe_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for cookie in cookies:
        key = (str(cookie.get("name") or ""), str(cookie.get("domain") or ""), str(cookie.get("path") or ""))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        result.append(cookie)
    return result


def _safe_cookie_names(cookies: list[dict[str, Any]]) -> list[str]:
    names = sorted({str(cookie.get("name") or "") for cookie in cookies if cookie.get("name")})
    return names[:80]


def _clip_text(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:limit]


class LoginFlowError(Exception):
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
    debug_logs: list[dict[str, Any]] = field(default_factory=list)
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
            "debug_logs": self.debug_logs,
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
        login_session_id = uuid4().hex
        expires_at = datetime.utcnow() + LOGIN_SESSION_TTL
        browser = context = page = playwright = None
        qr_image: str | None = None
        try:
            browser, context, page, qr_image, playwright = await self._prepare_page(site)
        except LoginFlowError as exc:
            logger.warning("login flow failed for site=%s label=%s: %s", site, label, exc)
            raise LoginSessionSetupError(str(exc), status_code=exc.status_code)
        except Exception as exc:
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
        self._debug(session, "qr_ready", "二维码已生成，开始等待扫码。", persist=False)
        with SessionLocal() as db:
            video_site_login_sessions.create_login_session(
                db,
                login_session_id=login_session_id,
                site=site,
                label=label,
                status=session.status,
                qrcode_image_base64=qr_image,
                expires_at=expires_at,
                debug_logs=session.debug_logs,
            )
            db.commit()
        async with self._lock:
            self._sessions[login_session_id] = session
        session._task = asyncio.create_task(self._monitor_session(session))
        return session

    def get_session(self, login_session_id: str) -> Optional[LoginSessionState]:
        return self._sessions.get(login_session_id)

    def _debug(self, session: LoginSessionState, event: str, message: str, *, data: dict | None = None, persist: bool = True) -> None:
        entry = {"time": _utc_ts(), "event": event, "message": message}
        if data:
            entry["data"] = data
        session.debug_logs.append(entry)
        session.debug_logs = session.debug_logs[-MAX_DEBUG_LOGS:]
        logger.info("yt-dlp login debug session=%s event=%s message=%s data=%s", session.login_session_id, event, message, data)
        if persist:
            self._persist_session_state(session)

    def _persist_session_state(self, session: LoginSessionState, *, status: str | None = None, account: dict | None = None, error_msg: str | None = None) -> None:
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
                debug_logs=session.debug_logs,
            )
            db.commit()

    async def _prepare_page(self, site: str) -> tuple[Browser, BrowserContext, Page, str, Any]:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--lang=en-US,en;q=0.9",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1365, "height": 768},
            user_agent=REALISTIC_USER_AGENT,
            locale="en-US",
            timezone_id="America/Los_Angeles",
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script(
            """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
window.chrome = window.chrome || { runtime: {} };
"""
        )
        page = await context.new_page()
        page.set_default_timeout(10_000)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        try:
            if site == "tiktok":
                qr_image = await self._prepare_tiktok_login(page)
            else:
                try:
                    await page.goto(LOGIN_URLS[site], wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                except PlaywrightTimeoutError as exc:
                    raise LoginFlowError("Login page navigation timed out, please check server network/region", status_code=502) from exc
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
            response = await page.goto(LOGIN_URLS["tiktok"], wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        except PlaywrightTimeoutError as exc:
            raise LoginFlowError("TikTok QR login timed out, please check server network / region access.", status_code=502) from exc
        status = response.status if response else None
        if not status or status >= 400:
            raise LoginFlowError(f"TikTok login page returned HTTP {status}", status_code=502)
        await self._tiktok_qr_flow(page)
        qr_locator = await self._wait_for_tiktok_qr(page)
        if not qr_locator:
            raise LoginFlowError("TikTok QR code did not appear in time (maybe region/network/captcha restricted)", status_code=502)
        png_bytes = await qr_locator.screenshot(type="png")
        return f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"

    async def _enter_qr_mode(self, site: str, page: Page) -> None:
        handlers: Dict[str, Callable[[Page], Awaitable[None]]] = {"douyin": self._douyin_qr_flow, "youtube": self._youtube_qr_flow}
        handler = handlers.get(site)
        if handler:
            try:
                await handler(page)
            except Exception as exc:
                logger.warning("qr flow setup failed for %s: %s", site, exc)

    async def _tiktok_qr_flow(self, page: Page) -> None:
        if await self._has_tiktok_qr_element(page):
            return
        locator_candidates = [
            page.get_by_role("button", name=re.compile("(QR code|Log in with QR|Use QR)", re.IGNORECASE)),
            page.get_by_text(re.compile("(QR code|Log in with QR|Use QR)", re.IGNORECASE)),
            page.get_by_text(re.compile("(二维码|扫码)")),
            page.locator("button:has-text('QR')"),
            page.locator("div[role='button']:has-text('QR')"),
            page.locator("[data-e2e*='qr']"),
        ]
        for locator in locator_candidates:
            try:
                await locator.first.click(timeout=3000)
                await page.wait_for_timeout(800)
                if await self._has_tiktok_qr_element(page):
                    return
            except Exception:
                continue
        raise LoginFlowError("TikTok login page did not expose any QR-code login button", status_code=502)

    async def _has_tiktok_qr_element(self, page: Page) -> bool:
        for selector in ["canvas[data-e2e='qr-code']", "[data-e2e='qr-code'] canvas", "[data-e2e*='qr'] canvas", "img[alt*='QR']", "img[src^='data:image']", "canvas"]:
            try:
                if await page.locator(selector).first.is_visible(timeout=600):
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_tiktok_qr(self, page: Page):
        for locator in [page.locator("canvas[data-e2e='qr-code']"), page.locator("[data-e2e='qr-code'] canvas"), page.locator("[data-e2e*='qr'] canvas"), page.locator("img[alt*='QR']"), page.locator("img[src^='data:image']"), page.locator("canvas")]:
            try:
                first = locator.first
                await first.wait_for(timeout=QR_WAIT_TIMEOUT_MS)
                if await first.is_visible(timeout=1000):
                    return first
            except Exception:
                continue
        return None

    async def _close_resources(self, browser: Browser | None, context: BrowserContext | None, playwright: Any) -> None:
        for resource in (context, browser, playwright):
            try:
                if resource:
                    if resource is playwright:
                        await resource.stop()
                    else:
                        await resource.close()
            except Exception:
                pass

    async def _douyin_qr_flow(self, page: Page) -> None:
        for selector in ["text=二维码登录", "text=扫码登录", "text=QR"]:
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
        self._persist_session_state(session, status=LOGIN_WAITING_STATUS)
        self._debug(session, "poll_start", "开始轮询登录状态。", data={"timeout_seconds": int(LOGIN_TIMEOUT.total_seconds())})
        try:
            last_phase = None
            last_snapshot_at: datetime | None = None
            while datetime.utcnow() < deadline:
                await asyncio.sleep(POLL_INTERVAL)
                now = datetime.utcnow()
                if not session._context:
                    self._debug(session, "missing_context", "浏览器上下文不存在，跳过本轮。")
                    continue
                cookies = await self._collect_site_cookies(session)
                logged_in = self._is_logged_in(session.site, cookies)
                phase = session.status
                if session.site == "tiktok":
                    phase = await self._get_tiktok_login_phase(session, cookies)
                    if phase != last_phase:
                        self._debug(session, "phase_change", f"登录阶段变更为 {phase}", data={"url": self._safe_page_url(session), "cookie_count": len(cookies), "cookie_names": _safe_cookie_names(cookies), "body_text": await self._safe_body_text(session)})
                        last_phase = phase
                        last_snapshot_at = now
                    elif phase in {"waiting_scan", "waiting_confirm"} and (
                        last_snapshot_at is None or (now - last_snapshot_at).total_seconds() >= SNAPSHOT_INTERVAL_SECONDS
                    ):
                        self._debug(session, "phase_snapshot", f"仍处于 {phase}，继续等待。", data={"url": self._safe_page_url(session), "cookie_count": len(cookies), "cookie_names": _safe_cookie_names(cookies), "body_text": await self._safe_body_text(session)})
                        last_snapshot_at = now
                    if phase == "waiting_confirm" and session.status != "waiting_confirm":
                        self._persist_session_state(session, status="waiting_confirm")
                    if phase == "success":
                        logged_in = True
                else:
                    self._debug(session, "poll", "轮询登录状态。", data={"cookie_count": len(cookies), "cookie_names": _safe_cookie_names(cookies)})

                if logged_in:
                    self._debug(session, "login_detected", "检测到登录成功信号，准备重新读取 cookies。", data={"cookie_count": len(cookies), "cookie_names": _safe_cookie_names(cookies), "url": self._safe_page_url(session)})
                    await asyncio.sleep(1.0)
                    cookies = await self._collect_site_cookies(session)
                    await self._handle_success(session, cookies)
                    return
            self._debug(session, "expired", "等待登录超时。", data={"url": self._safe_page_url(session), "body_text": await self._safe_body_text(session)})
            self._persist_session_state(session, status="expired", error_msg=f"Timed out waiting for {session.site} login")
        except Exception as exc:
            logger.exception("login session failed: %s", exc)
            self._debug(session, "exception", f"轮询异常：{exc}")
            self._persist_session_state(session, status="failed", error_msg=str(exc))
        finally:
            await self._cleanup_browser(session)
            async with self._lock:
                self._sessions.pop(session.login_session_id, None)

    def _safe_page_url(self, session: LoginSessionState) -> str | None:
        try:
            return session._page.url if session._page else None
        except Exception:
            return None

    async def _safe_body_text(self, session: LoginSessionState) -> str:
        try:
            if not session._page:
                return ""
            text = await asyncio.wait_for(session._page.locator("body").inner_text(timeout=1200), timeout=1.5)
            return _clip_text(text)
        except Exception:
            return ""

    async def _collect_site_cookies(self, session: LoginSessionState) -> list[dict[str, Any]]:
        if not session._context:
            return []
        cookies: list[dict[str, Any]] = []
        try:
            cookies.extend(await session._context.cookies())
        except Exception as exc:
            self._debug(session, "cookies_error", f"读取 context cookies 失败：{exc}")
        urls = COOKIE_URLS.get(session.site) or []
        if urls:
            try:
                cookies.extend(await session._context.cookies(urls))
            except Exception as exc:
                self._debug(session, "cookies_url_error", f"按 URL 读取 cookies 失败：{exc}", data={"urls": urls})
        domain_part = DOMAIN_FILTERS.get(session.site, "")
        filtered = [cookie for cookie in cookies if domain_part in (cookie.get("domain") or "")]
        return _dedupe_cookies(filtered)

    async def _get_tiktok_login_phase(self, session: LoginSessionState, cookies: list[dict[str, Any]]) -> str:
        page = session._page
        if not page:
            return session.status
        if self._is_logged_in("tiktok", cookies):
            return "success"
        try:
            url = page.url or ""
            if "tiktok.com" in url and "/login" not in url and "/signup" not in url:
                return "success"
        except Exception:
            pass
        if await self._tiktok_page_logged_in_signal(page):
            return "success"
        if await self._tiktok_waiting_confirm_signal(page):
            return "waiting_confirm"
        return "waiting_scan"

    async def _tiktok_page_logged_in_signal(self, page: Page) -> bool:
        for selector in ["[data-e2e='nav-profile']", "[data-e2e='profile-icon']", "[data-e2e='nav-user-profile']", "a[href^='/@'] img", "a[data-e2e='nav-profile']"]:
            try:
                await page.wait_for_selector(selector, timeout=700)
                return True
            except Exception:
                continue
        return False

    async def _tiktok_waiting_confirm_signal(self, page: Page) -> bool:
        try:
            body_text = await asyncio.wait_for(page.locator("body").inner_text(timeout=1200), timeout=1.5)
        except Exception:
            body_text = ""
        if not body_text:
            return False
        lower = body_text.lower()
        exact_waiting_phrases = [
            "you scanned the qr code",
            "scanned successfully",
            "scan successful",
            "request sent",
            "open the tiktok app to confirm",
            "confirm on your mobile device",
            "confirm on your phone",
            "approve login",
            "check your phone",
            "已扫码",
            "扫描成功",
            "请在手机上确认",
            "请在 app 中确认",
            "请在 tiktok app 中确认",
        ]
        return any(phrase in lower for phrase in exact_waiting_phrases)

    def _is_logged_in(self, site: str, cookies: list[dict[str, Any]]) -> bool:
        target_names = SESSION_COOKIE_NAMES.get(site, set())
        target_names_lower = {name.lower() for name in target_names}
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            name_lower = name.lower()
            if name in target_names or name_lower in target_names_lower:
                return True
            if site == "tiktok" and (name_lower.startswith("sessionid") or name_lower.startswith("sid_") or name_lower.startswith("uid_tt") or name_lower in {"multi_sids", "passport_auth_status", "passport_auth_status_ss"}):
                return True
        return False

    async def _handle_success(self, session: LoginSessionState, cookies: list[dict[str, Any]]) -> None:
        filtered = _dedupe_cookies([cookie for cookie in cookies if DOMAIN_FILTERS.get(session.site, "") in (cookie.get("domain") or "")])
        if not filtered:
            self._debug(session, "no_cookies", "检测到登录成功信号，但没有抓到站点 cookies。")
            self._persist_session_state(session, status="failed", error_msg="Login confirmed but no site cookies were captured. Please regenerate QR code and try again.")
            return
        expires_at = _earliest_expiry(filtered)
        try:
            with SessionLocal() as db:
                record = video_site_cookies.upsert_video_site_cookies(
                    db,
                    site=session.site,
                    label=session.label,
                    cookies_json=json.dumps(filtered, ensure_ascii=False),
                    is_active=True,
                    last_login_at=datetime.utcnow(),
                    expires_at=expires_at,
                )
                db.commit()
                session.account = {"id": record.id, "site": record.site, "label": record.label, "last_login_at": record.last_login_at, "is_active": bool(record.is_active)}
            self._debug(session, "saved", "Cookies 已保存。", data={"cookie_count": len(filtered), "cookie_names": _safe_cookie_names(filtered)})
            self._persist_session_state(session, status="success", account=session.account)
        except Exception as exc:
            logger.exception("failed to persist cookies: %s", exc)
            self._debug(session, "save_failed", f"保存 cookies 失败：{exc}")
            self._persist_session_state(session, status="failed", error_msg=f"persist cookies failed: {exc}")

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

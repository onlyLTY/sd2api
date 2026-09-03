from __future__ import annotations

import asyncio
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from playwright.async_api import (
    BrowserContext,
    Frame,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .config import Settings
from .models import UpstreamTask
from .tiktok import I2V_MODELS, R2V_MODELS, T2V_MODELS, TikTokUpstreamError
from .temp_mail import TempMailClient, TempMailError
from .uploads import StagedMedia


STUDIO_URL = (
    "https://ads.tiktok.com/creative/creativestudio/image-to-video"
    "?subApp=CreativeStudio/MiniApp/TextToVideo"
)
IMAGE_STUDIO_URL = "https://ads.tiktok.com/creative/creativestudio/image-to-video"
ACCOUNT_LIST_PATH = "/CreativeOne/Client/ClientGetAccountList"
ACCOUNT_INFO_PATH = "/CreativeOne/Client/ClientGetAccountInfo"
KEEPALIVE_ACCOUNT_INFO_PATHS = {
    "/passport/web/account/info",
    ACCOUNT_INFO_PATH.lower(),
}
CREDIT_ACCOUNT_PATH = "/CreativeOne/SymphonyPlatform/QueryCreditAccount"
USER_LEVEL_PATH = "/CreativeOne/SymphonyPlatform/QueryUserLevelDetail"
MINIAPP_PERMISSION_PATH = (
    "/creative_bff_i18n/api/cue/get_miniapp_permission_with_allowlist"
)
ACCOUNT_CONTEXT_COOKIE = "s_aio_client_id"
SEEDANCE2_ALLOWLIST_TOOL = "cue_mini_i2v_seedance_2"
SEEDANCE2_USER_TIERS = {"T00", "T0", "T1"}
CHROMIUM_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _clear_stale_chromium_profile_locks(profile: Path) -> bool:
    lock = profile / "SingletonLock"
    if not lock.is_symlink():
        return False
    try:
        owner, pid_text = os.readlink(lock).rsplit("-", 1)
        pid = int(pid_text)
    except (OSError, ValueError):
        return False
    if pid <= 1:
        return False

    if owner == socket.gethostname():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
        else:
            return False

    removed = False
    for name in CHROMIUM_SINGLETON_FILES:
        singleton = profile / name
        if singleton.is_symlink():
            singleton.unlink(missing_ok=True)
            removed = True
    return removed


@dataclass(slots=True)
class BrowserJob:
    id: str
    prompt: str
    model: str
    duration: int
    mode: Literal["text", "image", "reference"] = "text"
    media: tuple[StagedMedia, ...] = ()
    advertiser_id: str | None = None


class BrowserTikTokClient:
    """Chromium login bootstrap and legacy UI-generation fallback.

    After a successful login the pool exports a narrowly scoped, encrypted
    protocol session. The browser profile remains the recoverable source of
    truth for re-login, while normal generation can run without Chromium.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        account_id: str = "default",
        profile_path: str | None = None,
    ) -> None:
        self.settings = settings
        self.account_id = account_id
        self.profile_path = profile_path or settings.sd2api_browser_profile
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._queue: asyncio.Queue[BrowserJob] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._tasks: dict[str, UpstreamTask] = {}
        self._start_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._subaccount_lock = asyncio.Lock()
        self._running_job = False
        self._login_state = "not_logged_in"
        self._login_error: str | None = None
        self._last_login_at: int | None = None
        self._active_advertiser_id: str | None = None

    async def start(self, *, target_url: str | None = STUDIO_URL) -> dict[str, Any]:
        async with self._start_lock:
            if self._context is not None and (
                self._page is None or self._page.is_closed()
            ):
                open_pages = [page for page in self._context.pages if not page.is_closed()]
                if open_pages:
                    self._page = open_pages[0]
                else:
                    try:
                        self._page = await self._context.new_page()
                    except Exception:
                        # The user may have closed the entire Chromium window.
                        # Relaunch the same persistent profile below.
                        self._context = None
                        self._page = None

            if self._context is None:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                profile_path = Path(self.profile_path).resolve()
                _clear_stale_chromium_profile_locks(profile_path)
                profile = str(profile_path)
                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile,
                    channel=self.settings.sd2api_browser_channel or None,
                    headless=self.settings.sd2api_browser_headless,
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                self._context = context
                context.on("close", lambda *_: self._mark_context_closed(context))
                self._page = context.pages[0] if context.pages else await context.new_page()
            self._require_page().set_default_timeout(30_000)
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._worker_loop(), name="sd2api-browser-worker")
            page = self._require_page()
            if target_url is not None and not page.url.startswith(
                "https://ads.tiktok.com/"
            ):
                await page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
        return await self.status()

    async def stop(self) -> None:
        async with self._stop_lock:
            if self._worker:
                self._worker.cancel()
                try:
                    await self._worker
                except asyncio.CancelledError:
                    pass
                self._worker = None
            context, self._context = self._context, None
            self._page = None
            if context:
                try:
                    await asyncio.wait_for(context.close(), timeout=15)
                except (TimeoutError, Exception):
                    pass
            playwright, self._playwright = self._playwright, None
            if playwright:
                try:
                    await asyncio.wait_for(playwright.stop(), timeout=15)
                except (TimeoutError, Exception):
                    pass

    async def status(self) -> dict[str, Any]:
        if self._page is None or self._page.is_closed():
            if self._login_state not in {"login_failed", "not_configured"}:
                self._set_login_state("browser_closed")
            return {
                "account_id": self.account_id,
                "running": False,
                "logged_in": False,
                "url": None,
                "queued": self._queue.qsize(),
                "busy": self._running_job,
                "credits": None,
                "login_state": self._login_state,
                "login_error": self._login_error,
                "last_login_at": self._last_login_at,
                "active_advertiser_id": self._active_advertiser_id,
            }
        try:
            logged_in = await self._is_logged_in(self._page)
        except Exception:
            if self._browser_unavailable():
                self._set_login_state("browser_closed")
                return {
                    "account_id": self.account_id,
                    "running": False,
                    "logged_in": False,
                    "url": None,
                    "queued": self._queue.qsize(),
                    "busy": self._running_job,
                    "credits": None,
                    "login_state": self._login_state,
                    "login_error": self._login_error,
                    "last_login_at": self._last_login_at,
                    "active_advertiser_id": self._active_advertiser_id,
                }
            raise
        if logged_in and self._login_state != "logging_in":
            self._set_login_state("logged_in")
        credits = await self._visible_credits(self._page) if logged_in else None
        return {
            "running": True,
            "logged_in": logged_in,
            "url": self._page.url,
            "queued": self._queue.qsize(),
            "busy": self._running_job,
            "account_id": self.account_id,
            "credits": credits,
            "login_state": self._login_state,
            "login_error": self._login_error,
            "last_login_at": self._last_login_at,
            "active_advertiser_id": self._active_advertiser_id,
        }

    @property
    def load(self) -> int:
        return self._queue.qsize() + int(self._running_job)

    async def create_text_video(
        self,
        *,
        prompt: str,
        model: str,
        duration: int,
        advertiser_id: str | None = None,
    ) -> str:
        return await self._create_video(
            prompt=prompt,
            model=model,
            duration=duration,
            mode="text",
            media=[],
            advertiser_id=advertiser_id,
        )

    async def create_image_video(
        self,
        *,
        prompt: str,
        model: str,
        duration: int,
        image_path: str,
        advertiser_id: str | None = None,
    ) -> str:
        return await self._create_video(
            prompt=prompt,
            model=model,
            duration=duration,
            mode="image",
            media=[StagedMedia(kind="image", path=image_path)],
            advertiser_id=advertiser_id,
        )

    async def create_reference_video(
        self,
        *,
        prompt: str,
        model: str,
        duration: int,
        media: list[StagedMedia],
        advertiser_id: str | None = None,
    ) -> str:
        return await self._create_video(
            prompt=prompt,
            model=model,
            duration=duration,
            mode="reference",
            media=media,
            advertiser_id=advertiser_id,
        )

    async def _create_video(
        self,
        *,
        prompt: str,
        model: str,
        duration: int,
        mode: Literal["text", "image", "reference"],
        media: list[StagedMedia],
        advertiser_id: str | None,
    ) -> str:
        models = {
            "text": T2V_MODELS,
            "image": I2V_MODELS,
            "reference": R2V_MODELS,
        }[mode]
        if model.lower() not in models:
            raise TikTokUpstreamError(
                f"Unsupported model {model!r} for {mode}-to-video",
                status_code=400,
                code="invalid_model",
            )
        if not 4 <= duration <= 15:
            raise TikTokUpstreamError(
                "Browser mode supports durations from 4 through 15 seconds",
                status_code=400,
                code="invalid_duration",
            )
        resolved_media: list[StagedMedia] = []
        for item in media:
            path = Path(item.path).resolve()
            if not path.is_file():
                raise TikTokUpstreamError(
                    f"The staged {item.kind} reference no longer exists",
                    status_code=422,
                    code=f"{item.kind}_missing",
                )
            resolved_media.append(StagedMedia(kind=item.kind, path=str(path)))
        if mode == "image" and (
            len(resolved_media) != 1 or resolved_media[0].kind != "image"
        ):
            raise TikTokUpstreamError(
                "Image-to-video requires exactly one image",
                status_code=422,
                code="invalid_image_references",
            )
        if mode == "reference":
            images = sum(item.kind == "image" for item in resolved_media)
            videos = sum(item.kind == "video" for item in resolved_media)
            audios = sum(item.kind == "audio" for item in resolved_media)
            if not (images or videos):
                raise TikTokUpstreamError(
                    "Reference-to-video requires at least one image or video",
                    status_code=422,
                    code="visual_reference_required",
                )
            if images > 9 or videos > 3 or audios > 3:
                raise TikTokUpstreamError(
                    "Reference limits are 9 images, 3 videos, and 3 audio clips",
                    status_code=422,
                    code="too_many_references",
                )
        await self.start()
        status = await self.status()
        if not status["logged_in"]:
            raise TikTokUpstreamError(
                "The persistent Chromium session is not logged in to TikTok Ads yet",
                status_code=401,
                code="browser_login_required",
            )
        task_id = "browser_" + uuid.uuid4().hex
        await self.submit(
            task_id=task_id,
            prompt=prompt,
            model=model,
            duration=duration,
            mode=mode,
            media=resolved_media,
            advertiser_id=advertiser_id,
        )
        return task_id

    async def submit(
        self,
        *,
        task_id: str,
        prompt: str,
        model: str,
        duration: int,
        mode: Literal["text", "image", "reference"] = "text",
        media: list[StagedMedia] | None = None,
        advertiser_id: str | None = None,
    ) -> None:
        self._tasks[task_id] = UpstreamTask(id=task_id, status="queued", progress=0)
        await self._queue.put(
            BrowserJob(
                task_id,
                prompt,
                model,
                duration,
                mode,
                tuple(media or []),
                advertiser_id,
            )
        )

    async def focus(self) -> None:
        await self.start()
        await self._require_page().bring_to_front()

    async def renew_protocol_session(self) -> dict[str, Any]:
        """Renew the authenticated Ads cookies in the persistent page context."""
        await self.start(target_url=None)
        page = self._require_page()
        context = self._context
        if context is None:
            raise TikTokUpstreamError(
                "The Chromium context closed before session keepalive",
                status_code=503,
                code="browser_closed",
            )

        core_names = {
            "sessionid_ads",
            "sessionid_ss_ads",
            "sid_tt_ads",
            "uid_tt_ads",
            "uid_tt_ss_ads",
            "sid_ucp_v1_ads",
            "ssid_ucp_v1_ads",
            "tt_session_tlb_tag_ads",
        }
        before = {
            str(cookie.get("name")): (
                str(cookie.get("value") or ""),
                float(cookie.get("expires") or -1),
            )
            for cookie in await context.cookies([self.settings.tiktok_base_url])
            if str(cookie.get("name")) in core_names
        }

        def is_account_info(response: Any) -> bool:
            path = urlparse(response.url).path.rstrip("/").lower()
            return path in KEEPALIVE_ACCOUNT_INFO_PATHS

        try:
            async with page.expect_response(is_account_info, timeout=90_000) as response_info:
                await page.goto(
                    IMAGE_STUDIO_URL,
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )
            response = await response_info.value
        except PlaywrightTimeoutError as exc:
            raise TikTokUpstreamError(
                "TikTok did not complete account-info during session keepalive",
                status_code=504,
                code="session_keepalive_timeout",
            ) from exc
        if response.status != 200:
            raise TikTokUpstreamError(
                f"TikTok account-info returned HTTP {response.status}",
                status_code=502,
                code="session_keepalive_failed",
            )
        await page.wait_for_timeout(1500)
        if not await self._is_logged_in(page):
            raise TikTokUpstreamError(
                "The persistent Chromium profile is no longer logged in",
                status_code=401,
                code="browser_login_required",
            )

        after = {
            str(cookie.get("name")): (
                str(cookie.get("value") or ""),
                float(cookie.get("expires") or -1),
            )
            for cookie in await context.cookies([self.settings.tiktok_base_url])
            if str(cookie.get("name")) in core_names
        }
        return {
            "url": IMAGE_STUDIO_URL,
            "account_info_status": response.status,
            "core_cookie_count": len(after),
            "rotated_cookie_names": sorted(
                name for name, state in after.items() if before.get(name) != state
            ),
        }

    async def export_protocol_session(
        self, *, bootstrap_identity: bool = True
    ) -> dict[str, Any]:
        """Capture the TikTok web session needed by the protocol client.

        The caller must encrypt the returned value before persistence. Only
        cookies applicable to the configured TikTok origin and the public web
        device identifier are included; passwords and browser profile files
        are never read.
        """
        context = self._context
        page = self._require_page()
        if context is None or not (
            await self._is_logged_in(page)
            or await self._has_auth_session_cookie(context)
        ):
            raise TikTokUpstreamError(
                "TikTok Ads must be logged in before exporting the protocol session",
                status_code=401,
                code="browser_login_required",
            )
        observed: dict[str, str] = {
            "device_id": "",
            "fp_id": "",
            "sec_ch_ua": "",
            "sec_ch_ua_mobile": "",
            "sec_ch_ua_platform": "",
        }

        def observe_request(request: Any) -> None:
            try:
                query = parse_qs(urlparse(request.url).query)
                for name in ("device_id", "did", "web_id", "webid"):
                    candidate = str((query.get(name) or [""])[0]).strip()
                    if len(candidate) >= 8:
                        observed["device_id"] = candidate
                        break
                fp_id = str(request.headers.get("x-fp-id") or "").strip()
                if fp_id:
                    observed["fp_id"] = fp_id
                    observed["sec_ch_ua"] = str(
                        request.headers.get("sec-ch-ua") or ""
                    )
                    observed["sec_ch_ua_mobile"] = str(
                        request.headers.get("sec-ch-ua-mobile") or ""
                    )
                    observed["sec_ch_ua_platform"] = str(
                        request.headers.get("sec-ch-ua-platform") or ""
                    )
            except Exception:
                # Network observation is a best-effort supplement to browser
                # storage; it must never make a successful login fail.
                pass

        page.on("request", observe_request)

        async def read_browser_state() -> dict[str, Any]:
            value = await page.evaluate(
                """
                () => {
                  const wanted = new Set([
                    "webid", "web_id", "deviceid", "device_id", "did",
                    "monitorwebid", "monitor_web_id", "monitordeviceid",
                    "monitor_device_id"
                  ].map((key) => key.replace(/[^a-z0-9]/gi, "").toLowerCase()));
                  const valid = (value) => {
                    const text = String(value == null ? "" : value).trim();
                    return text.length >= 8 && text.length <= 128 ? text : "";
                  };
                  const find = (value, depth = 0) => {
                    if (!value || depth > 5 || typeof value !== "object") return "";
                    for (const [key, child] of Object.entries(value)) {
                      const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
                      if (wanted.has(normalized) && typeof child !== "object") {
                        const candidate = valid(child);
                        if (candidate) return candidate;
                      }
                    }
                    for (const child of Object.values(value)) {
                      const candidate = find(child, depth + 1);
                      if (candidate) return candidate;
                    }
                    return "";
                  };
                  const scanStorage = (storage) => {
                    for (let index = 0; index < storage.length; index += 1) {
                      const key = storage.key(index) || "";
                      const raw = storage.getItem(key) || "";
                      const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
                      if (wanted.has(normalized)) {
                        const candidate = valid(raw);
                        if (candidate) return candidate;
                      }
                      try {
                        const candidate = find(JSON.parse(raw));
                        if (candidate) return candidate;
                      } catch (_) {}
                    }
                    return "";
                  };
                  const scanUrl = (raw) => {
                    try {
                      const url = new URL(raw, location.href);
                      for (const key of ["device_id", "did", "web_id", "webid"]) {
                        const candidate = valid(url.searchParams.get(key));
                        if (candidate) return candidate;
                      }
                    } catch (_) {}
                    return "";
                  };
                  let deviceId = scanStorage(localStorage) || scanStorage(sessionStorage);
                  if (!deviceId) {
                    for (const entry of performance.getEntriesByType("resource")) {
                      deviceId = scanUrl(entry.name);
                      if (deviceId) break;
                    }
                  }
                  if (!deviceId) deviceId = scanUrl(location.href);
                  if (!deviceId) {
                    const cookies = Object.fromEntries(document.cookie.split(";").map((item) => {
                      const position = item.indexOf("=");
                      return position < 0
                        ? [item.trim(), ""]
                        : [item.slice(0, position).trim(), item.slice(position + 1)];
                    }));
                    deviceId = find(cookies);
                  }
                  return {
                    device_id: deviceId,
                    user_agent: navigator.userAgent || "Mozilla/5.0"
                  };
                }
                """
            )
            return value if isinstance(value, dict) else {}

        try:
            browser_state = await read_browser_state()
            if bootstrap_identity and (
                not browser_state.get("device_id") or not observed["fp_id"]
            ):
                # A fresh Studio navigation triggers the bootstrap requests
                # which contain the public web device ID and fingerprint ID.
                await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=90_000)
                for _ in range(30):
                    await page.wait_for_timeout(500)
                    browser_state = await read_browser_state()
                    if (
                        (browser_state.get("device_id") or observed["device_id"])
                        and observed["fp_id"]
                    ):
                        break
        finally:
            page.remove_listener("request", observe_request)
        if not isinstance(browser_state, dict):
            browser_state = {}
        cookies = await context.cookies([self.settings.tiktok_base_url])
        device_id = str(browser_state.get("device_id") or observed["device_id"] or "")
        if not device_id:
            wanted_cookie_names = {
                "monitor_device_id", "monitor_web_id", "web_id", "webid",
                "device_id", "did",
            }
            device_id = next(
                (
                    str(item.get("value") or "")
                    for item in cookies
                    if str(item.get("name") or "").lower() in wanted_cookie_names
                    and item.get("value")
                ),
                "",
            )
        return {
            "version": 3,
            "captured_at": int(time.time()),
            "cookies": [dict(item) for item in cookies],
            "device_id": device_id,
            "user_agent": str(
                browser_state.get("user_agent") or self.settings.tiktok_user_agent
            ),
            "fp_id": observed["fp_id"],
            "sec_ch_ua": observed["sec_ch_ua"],
            "sec_ch_ua_mobile": observed["sec_ch_ua_mobile"],
            "sec_ch_ua_platform": observed["sec_ch_ua_platform"],
        }

    async def scan_subaccounts(self, *, check_access: bool = True) -> list[dict[str, Any]]:
        """Discover accounts and capabilities through TikTok's JSON control APIs."""
        async with self._subaccount_lock:
            await self.start()
            page = self._require_page()
            if not await self._is_logged_in(page):
                raise TikTokUpstreamError(
                    "The Chromium session must be logged in before scanning subaccounts",
                    status_code=401,
                    code="browser_login_required",
                )
            discovered = await self._discover_subaccounts(page)
            original = self._active_advertiser_id
            if not check_access:
                return discovered

            results: list[dict[str, Any]] = []
            try:
                for item in discovered:
                    checked = dict(item)
                    try:
                        advertiser_id = str(item["advertiser_id"])
                        await self._set_account_context(page, advertiser_id)
                        info, credit, permissions, user_level = await asyncio.gather(
                            self._api_json("GET", ACCOUNT_INFO_PATH),
                            self._api_json("POST", CREDIT_ACCOUNT_PATH, data={}),
                            self._api_json("GET", MINIAPP_PERMISSION_PATH),
                            self._api_json("POST", USER_LEVEL_PATH, data={}),
                        )
                        returned_id = str(
                            (info.get("account") or {}).get("aioClientID") or ""
                        )
                        if returned_id != advertiser_id:
                            raise RuntimeError(
                                f"TikTok returned account {returned_id!r} after selecting "
                                f"{advertiser_id!r}"
                            )
                        checked["credits"] = self._parse_credits(credit)
                        checked["seedance_access"] = self._seedance2_access(
                            permissions, user_level
                        )
                        checked["last_error"] = None
                    except Exception as exc:
                        checked["seedance_access"] = False
                        checked["credits"] = None
                        checked["last_error"] = f"{exc.__class__.__name__}: {exc}"
                    checked["active"] = checked["advertiser_id"] == original
                    checked["last_checked_at"] = int(time.time())
                    results.append(checked)
            finally:
                if original:
                    await self._set_account_context(page, original)
                self._active_advertiser_id = original

            for item in results:
                item["active"] = item["advertiser_id"] == self._active_advertiser_id
            return results

    async def select_subaccount(self, advertiser_id: str) -> None:
        async with self._subaccount_lock:
            page = self._require_page()
            await self._switch_subaccount(page, advertiser_id)

    async def _open_subaccount_menu(self, page: Page) -> None:
        await self._ensure_terms_accepted(page)
        labels = page.locator("p").filter(has_text=re.compile(r"^ID:\s*\d{10,}$"))
        sections = page.get_by_text(
            re.compile(r"^(Client account|Partner account)$", re.I)
        )

        async def menu_visible() -> bool:
            for locator in (labels, sections):
                for index in range(await locator.count()):
                    if await locator.nth(index).is_visible():
                        return True
            return False

        if await menu_visible():
            return
        triggers = page.locator('nav p[class*="xl:block"]')
        for index in range(await triggers.count()):
            trigger = triggers.nth(index)
            if await trigger.is_visible():
                for ancestor in ("xpath=..", "xpath=../..", "xpath=../../.."):
                    try:
                        await trigger.locator(ancestor).click(timeout=5_000)
                    except PlaywrightTimeoutError:
                        await self._ensure_terms_accepted(page)
                        continue
                    await page.wait_for_timeout(500)
                    if await menu_visible():
                        return
        raise RuntimeError("Could not open the TikTok subaccount menu")

    async def _discover_subaccounts(self, page: Page) -> list[dict[str, Any]]:
        account_list, account_info = await asyncio.gather(
            self._api_json("GET", ACCOUNT_LIST_PATH),
            self._api_json("GET", ACCOUNT_INFO_PATH),
        )
        accounts = account_list.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            raise RuntimeError("TikTok did not return any Client or Partner accounts")
        active_id = str(
            ((account_info.get("account") or {}).get("aioClientID")) or ""
        )
        result: list[dict[str, Any]] = []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            advertiser_id = str(account.get("aioClientID") or "")
            if not advertiser_id:
                continue
            account_type = account.get("accountType")
            result.append(
                {
                    "advertiser_id": advertiser_id,
                    "name": str(
                        account.get("profileName")
                        or account.get("companyName")
                        or advertiser_id
                    ),
                    "account_type": (
                        "client"
                        if account_type == 1
                        else "partner"
                        if account_type == 3
                        else "unknown"
                    ),
                    "active": advertiser_id == active_id,
                }
            )
        if not result:
            raise RuntimeError("TikTok returned an empty account list")
        self._active_advertiser_id = active_id or None
        return result

    async def _switch_subaccount(self, page: Page, advertiser_id: str) -> None:
        entries = await self._discover_subaccounts(page)
        if not any(item["advertiser_id"] == advertiser_id for item in entries):
            raise RuntimeError(f"TikTok subaccount {advertiser_id} is not available")
        await self._set_account_context(page, advertiser_id)
        account_info = await self._api_json("GET", ACCOUNT_INFO_PATH)
        returned_id = str((account_info.get("account") or {}).get("aioClientID") or "")
        if returned_id != advertiser_id:
            raise RuntimeError(
                f"TikTok did not activate subaccount {advertiser_id}; got {returned_id!r}"
            )
        await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1_000)
        await self._ensure_terms_accepted(page)
        self._active_advertiser_id = advertiser_id

    async def _set_account_context(self, page: Page, advertiser_id: str) -> None:
        if not re.fullmatch(r"\d{10,}", advertiser_id):
            raise ValueError(f"Invalid TikTok account ID {advertiser_id!r}")
        await page.evaluate(
            """
            ({name, value}) => {
              const expires = new Date();
              expires.setDate(expires.getDate() + 30);
              document.cookie = `${name}=${value}; domain=tiktok.com; ` +
                `expires=${expires.toUTCString()}; path=/`;
            }
            """,
            {"name": ACCOUNT_CONTEXT_COOKIE, "value": advertiser_id},
        )

    async def _api_json(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = self._context
        if context is None:
            raise RuntimeError("Persistent browser is not running")
        url = f"{self.settings.tiktok_base_url.rstrip('/')}/{path.lstrip('/')}"
        if method == "GET":
            response = await context.request.get(url)
        elif method == "POST":
            response = await context.request.post(url, data=data or {})
        else:
            raise ValueError(f"Unsupported browser API method {method!r}")
        if not response.ok:
            raise TikTokUpstreamError(
                f"TikTok {path} returned HTTP {response.status}",
                status_code=502,
                code="tiktok_control_api_error",
            )
        payload = await response.json()
        if not isinstance(payload, dict):
            raise TikTokUpstreamError(
                f"TikTok {path} returned a non-object response",
                status_code=502,
                code="tiktok_control_api_error",
            )
        base_response = payload.get("BaseResp")
        if isinstance(base_response, dict) and int(base_response.get("StatusCode") or 0):
            raise TikTokUpstreamError(
                str(base_response.get("StatusMessage") or f"TikTok {path} failed"),
                status_code=502,
                code="tiktok_control_api_error",
            )
        if int(payload.get("code") or 0):
            raise TikTokUpstreamError(
                str(payload.get("message") or f"TikTok {path} failed"),
                status_code=502,
                code="tiktok_control_api_error",
            )
        return payload

    @staticmethod
    def _parse_credits(payload: dict[str, Any]) -> int | None:
        parsed: list[int] = []
        for value in (payload.get("credits"), payload.get("bonus")):
            if isinstance(value, bool):
                continue
            try:
                if value is not None:
                    parsed.append(int(value))
            except (TypeError, ValueError):
                continue
        return sum(parsed) if parsed else None

    @staticmethod
    def _seedance2_access(
        permissions: dict[str, Any], user_level: dict[str, Any]
    ) -> bool:
        allowlist = (permissions.get("data") or {}).get("allowlist") or []
        allowlisted = any(
            isinstance(item, dict)
            and item.get("tool") == SEEDANCE2_ALLOWLIST_TOOL
            and "entry" in (item.get("auth") or [])
            for item in allowlist
        )
        tier = str((user_level.get("user_level_info") or {}).get("user_segment_tier") or "")
        return allowlisted or tier in SEEDANCE2_USER_TIERS

    async def _has_seedance_access(self, page: Page) -> bool:
        current = page.get_by_text(re.compile(r"^Dreamina Seedance", re.I))
        for index in range(await current.count()):
            if await current.nth(index).is_visible():
                return True
        selectors = page.get_by_text(re.compile(r"^Video 1\.5", re.I))
        visible = [
            selectors.nth(index)
            for index in range(await selectors.count())
            if await selectors.nth(index).is_visible()
        ]
        if not visible:
            return False
        await visible[-1].click()
        await page.wait_for_timeout(300)
        options = page.get_by_text("Dreamina Seedance 2.0", exact=True)
        for index in range(await options.count()):
            option = options.nth(index)
            if not await option.is_visible():
                continue
            container = option.locator("xpath=../..")
            disabled = (await container.get_attribute("aria-disabled")) == "true"
            classes = await container.get_attribute("class") or ""
            await page.keyboard.press("Escape")
            return not disabled and "cursor-not-allowed" not in classes
        await page.keyboard.press("Escape")
        return False

    async def login(
        self,
        *,
        username: str,
        password: str,
        email_address: str,
        mail_client: TempMailClient,
    ) -> dict[str, Any]:
        """Log in while allowing a human to solve CAPTCHA or enter an OTP manually."""
        async with self._login_lock:
            started_at = time.time()
            deadline = time.monotonic() + self.settings.sd2api_login_timeout
            browser_restarts = 0
            while time.monotonic() < deadline:
                mail_task: asyncio.Task[str] | None = None
                try:
                    self._set_login_state(
                        "recovering_browser" if browser_restarts else "opening_login"
                    )
                    await self.start()
                    page = self._require_page()
                    if await self._has_browser_login_candidate(page):
                        self._last_login_at = int(time.time())
                        self._set_login_state("logged_in")
                        return await self.status()

                    # A restored TikTok SPA can render its authenticated shell
                    # a moment after domcontentloaded. Avoid mistaking that
                    # transition for a logged-out page.
                    await page.wait_for_timeout(2_000)
                    if await self._has_browser_login_candidate(page):
                        self._last_login_at = int(time.time())
                        self._set_login_state("logged_in")
                        return await self.status()

                    if not page.url.startswith("https://ads.tiktok.com/"):
                        await page.goto(
                            STUDIO_URL, wait_until="domcontentloaded", timeout=90_000
                        )
                        await page.wait_for_timeout(2_000)

                    existing_code_inputs = await self._visible_code_inputs(page)
                    if existing_code_inputs:
                        # A service/browser restart can restore an unfinished
                        # email challenge. Ask TikTok for a fresh code and resume
                        # there instead of starting the credential form again.
                        for frame in self._search_frames(page):
                            resend = frame.get_by_role(
                                "button",
                                name=re.compile(r"^(Send again|Resend|重新发送)$", re.I),
                            )
                            clicked = False
                            for index in range(await resend.count()):
                                button = resend.nth(index)
                                if await button.is_visible() and await button.is_enabled():
                                    await button.click()
                                    started_at = time.time()
                                    await page.wait_for_timeout(750)
                                    clicked = True
                                    break
                            if clicked:
                                break
                    else:
                        self._set_login_state("entering_credentials")
                        await self._open_password_login(page)
                        try:
                            username_input = await self._wait_for_visible(
                                page,
                                (
                                    'input[type="email"]',
                                    'input[type="text"]',
                                    'input[name*="email" i]',
                                    'input[name*="username" i]',
                                    'input[placeholder*="email" i]',
                                    'input[aria-label*="email" i]',
                                    'input[autocomplete="username"]',
                                ),
                                timeout=30,
                            )
                        except RuntimeError:
                            if await self._has_browser_login_candidate(page):
                                self._last_login_at = int(time.time())
                                self._set_login_state("logged_in")
                                return await self.status()
                            raise
                        await username_input.fill(username)

                        password_input = await self._first_visible(
                            page,
                            ('input[type="password"]', 'input[autocomplete="current-password"]'),
                        )
                        if password_input is None:
                            await self._click_login_action(
                                page, re.compile(r"^(Continue|Next|继续|下一步)$", re.I)
                            )
                            password_input = await self._wait_for_visible(
                                page,
                                ('input[type="password"]', 'input[autocomplete="current-password"]'),
                                timeout=30,
                            )
                        await password_input.fill(password)
                        await self._click_login_action(
                            page,
                            re.compile(r"^(Log in|Sign in|Continue|登录|登入|继续)$", re.I),
                        )

                    code_submitted = False
                    automatic_mail_failed: str | None = None
                    while time.monotonic() < deadline:
                        # Do not block on cf_temp_mail: the user can still type
                        # and submit the code manually while polling continues.
                        await asyncio.sleep(1.0)
                        if page.is_closed() or self._context is None:
                            raise RuntimeError("Chromium was closed during login")
                        if await self._has_browser_login_candidate(page):
                            self._last_login_at = int(time.time())
                            self._set_login_state("logged_in")
                            return await self.status()
                        invalid = await self._login_error_text(page)
                        if invalid:
                            raise RuntimeError(invalid)
                        if await self._captcha_visible(page):
                            self._set_login_state("captcha_required")
                            continue

                        code_inputs = await self._visible_code_inputs(page)
                        if code_inputs and not code_submitted:
                            if (
                                mail_client.configured
                                and mail_task is None
                                and automatic_mail_failed is None
                            ):
                                mail_task = asyncio.create_task(
                                    mail_client.wait_for_code(
                                        to_address=email_address,
                                        since=started_at,
                                    ),
                                    name=f"sd2api-mail-code-{self.account_id}",
                                )
                            if mail_task is not None and mail_task.done():
                                try:
                                    code = mail_task.result()
                                except TempMailError as exc:
                                    automatic_mail_failed = str(exc)
                                    mail_task = None
                                else:
                                    self._set_login_state("submitting_email_code")
                                    await self._fill_code_inputs(code_inputs, code)
                                    try:
                                        await self._click_login_action(
                                            page,
                                            re.compile(
                                                r"^(Verify|Confirm|Continue|Submit|验证|确认|继续|提交)$",
                                                re.I,
                                            ),
                                        )
                                    except RuntimeError:
                                        # Some OTP forms submit immediately when
                                        # their final digit is filled.
                                        await page.wait_for_timeout(2_000)
                                    code_submitted = True
                                    mail_task = None
                                    continue

                            if automatic_mail_failed:
                                self._set_login_state(
                                    "waiting_email_code_manual", automatic_mail_failed
                                )
                            elif mail_client.configured:
                                self._set_login_state("waiting_email_code")
                            else:
                                self._set_login_state(
                                    "waiting_email_code_manual",
                                    "cf_temp_mail is not configured; enter the email code manually",
                                )
                            continue

                        # A disappearing code form may mean that the user
                        # submitted the OTP manually. Keep checking the page.
                        self._set_login_state("waiting_for_login")

                    raise RuntimeError(
                        f"TikTok login did not finish within {self.settings.sd2api_login_timeout} seconds"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if (
                        self._browser_unavailable() or self._transient_page_error(exc)
                    ) and browser_restarts < 3:
                        browser_restarts += 1
                        self._set_login_state("recovering_browser", str(exc))
                        await asyncio.sleep(0.5)
                        continue
                    self._set_login_state("login_failed", str(exc))
                    raise
                finally:
                    if mail_task is not None and not mail_task.done():
                        mail_task.cancel()
                        await asyncio.gather(mail_task, return_exceptions=True)

            message = (
                f"TikTok login did not finish within "
                f"{self.settings.sd2api_login_timeout} seconds"
            )
            self._set_login_state("login_failed", message)
            raise RuntimeError(message)

    async def diagnostics(
        self,
        *,
        open_generation_menu: bool = False,
        open_subaccount_menu: bool = False,
        click_subaccount_id: str | None = None,
    ) -> dict[str, Any]:
        """Return visible page labels without reading browser authentication storage."""
        await self.start()
        page = self._require_page()
        if open_generation_menu:
            selectors = page.get_by_role(
                "button",
                name=re.compile(r"^(Text|Image|Reference) to video$", re.I),
            )
            if await selectors.count():
                await selectors.last.click()
                await page.wait_for_timeout(500)
        if open_subaccount_menu:
            await self._open_subaccount_menu(page)
        if click_subaccount_id:
            await self._open_subaccount_menu(page)
            labels = page.get_by_text(f"ID: {click_subaccount_id}", exact=True)
            visible = [
                labels.nth(index)
                for index in range(await labels.count())
                if await labels.nth(index).is_visible()
            ]
            if len(visible) != 1:
                raise RuntimeError(
                    f"Could not uniquely locate TikTok subaccount {click_subaccount_id}"
                )
            try:
                await visible[0].locator("xpath=../../..").click(timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(5_000)
        buttons = page.locator("button:visible")
        roles = page.locator('[role="option"]:visible, [role="menuitem"]:visible')
        upload_candidates = page.locator(
            'input, [class*="upload" i], [class*="drop" i], [aria-label*="upload" i]'
        )
        frames = []
        visible_inputs: list[dict[str, Any]] = []
        use_email_elements: list[dict[str, Any]] = []
        topbar_elements: list[dict[str, Any]] = []
        subaccount_elements: list[dict[str, Any]] = []
        for frame in page.frames:
            frame_inputs = frame.locator('input[type="file"]')
            frames.append(
                {
                    "url": frame.url,
                    "file_inputs": [
                        {
                            "accept": await frame_inputs.nth(index).get_attribute("accept"),
                            "class": await frame_inputs.nth(index).get_attribute("class"),
                            "aria_label": await frame_inputs.nth(index).get_attribute("aria-label"),
                            "data_e2e": await frame_inputs.nth(index).get_attribute("data-e2e"),
                        }
                        for index in range(min(await frame_inputs.count(), 20))
                    ],
                }
            )
            all_inputs = frame.locator("input")
            for index in range(min(await all_inputs.count(), 50)):
                item = all_inputs.nth(index)
                if not await item.is_visible():
                    continue
                visible_inputs.append(
                    {
                        "type": await item.get_attribute("type"),
                        "name": await item.get_attribute("name"),
                        "id": await item.get_attribute("id"),
                        "placeholder": await item.get_attribute("placeholder"),
                        "autocomplete": await item.get_attribute("autocomplete"),
                        "inputmode": await item.get_attribute("inputmode"),
                        "maxlength": await item.get_attribute("maxlength"),
                        "class": await item.get_attribute("class"),
                    }
                )
            email_switches = frame.get_by_text("Use email", exact=True)
            for index in range(min(await email_switches.count(), 20)):
                item = email_switches.nth(index)
                if not await item.is_visible():
                    continue
                parent = item.locator("xpath=..")
                use_email_elements.append(
                    {
                        "tag": await item.evaluate("element => element.tagName"),
                        "role": await item.get_attribute("role"),
                        "class": await item.get_attribute("class"),
                        "parent_tag": await parent.evaluate("element => element.tagName"),
                        "parent_role": await parent.get_attribute("role"),
                        "parent_class": await parent.get_attribute("class"),
                    }
                )
        topbar = page.locator("nav:visible *, header:visible *")
        for index in range(min(await topbar.count(), 300)):
            item = topbar.nth(index)
            if not await item.is_visible():
                continue
            text = " ".join(((await item.text_content()) or "").split())
            if not text or len(text) > 80:
                continue
            parent = item.locator("xpath=..")
            topbar_elements.append(
                {
                    "text": text,
                    "tag": await item.evaluate("element => element.tagName"),
                    "role": await item.get_attribute("role"),
                    "class": await item.get_attribute("class"),
                    "parent_tag": await parent.evaluate("element => element.tagName"),
                    "parent_class": await parent.get_attribute("class"),
                }
            )
        account_labels = page.locator("p").filter(
            has_text=re.compile(r"^ID:\s*\d{10,}$")
        )
        for index in range(min(await account_labels.count(), 30)):
            label = account_labels.nth(index)
            if not await label.is_visible():
                continue
            ancestors: list[dict[str, Any]] = []
            node = label
            for _ in range(6):
                ancestors.append(
                    {
                        "tag": await node.evaluate("element => element.tagName"),
                        "class": await node.get_attribute("class"),
                        "role": await node.get_attribute("role"),
                        "text": " ".join(
                            (((await node.text_content()) or "")[:160]).split()
                        ),
                    }
                )
                node = node.locator("xpath=..")
            subaccount_elements.append(
                {
                    "id_text": " ".join(
                        (((await label.text_content()) or "")[:80]).split()
                    ),
                    "ancestors": ancestors,
                }
            )
        return {
            "url": page.url,
            "title": await page.title(),
            "buttons": [
                (await buttons.nth(index).inner_text()).strip()
                for index in range(min(await buttons.count(), 100))
            ],
            "options": [
                (await roles.nth(index).inner_text()).strip()
                for index in range(min(await roles.count(), 100))
            ],
            "button_details": [
                {
                    "text": (await buttons.nth(index).inner_text()).strip(),
                    "aria_label": await buttons.nth(index).get_attribute("aria-label"),
                    "title": await buttons.nth(index).get_attribute("title"),
                    "class": await buttons.nth(index).get_attribute("class"),
                }
                for index in range(min(await buttons.count(), 100))
            ],
            "upload_candidates": [
                {
                    "type": await upload_candidates.nth(index).get_attribute("type"),
                    "accept": await upload_candidates.nth(index).get_attribute("accept"),
                    "class": await upload_candidates.nth(index).get_attribute("class"),
                    "aria_label": await upload_candidates.nth(index).get_attribute("aria-label"),
                    "data_e2e": await upload_candidates.nth(index).get_attribute("data-e2e"),
                }
                for index in range(min(await upload_candidates.count(), 50))
            ],
            "visible_inputs": visible_inputs,
            "use_email_elements": use_email_elements,
            "topbar_elements": topbar_elements,
            "subaccount_elements": subaccount_elements,
            "frames": frames,
            "visible_text": (await page.locator("body").inner_text())[:20_000],
        }

    async def check_task(self, task_id: str) -> UpstreamTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise TikTokUpstreamError(
                f"Browser task {task_id!r} is not present in this running browser session",
                status_code=404,
                code="task_not_found",
            )
        return task.model_copy(deep=True)

    async def fetch_video(self, video_url: str) -> tuple[bytes, str]:
        """Download a generated asset through the authenticated browser context."""
        await self.start()
        context = self._context
        page = self._require_page()
        if context is None:
            raise RuntimeError("Persistent browser is not running")
        response = await context.request.get(
            video_url,
            headers={
                "referer": page.url,
                "accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
            },
            timeout=180_000,
        )
        if not response.ok:
            raise TikTokUpstreamError(
                f"TikTok CDN returned HTTP {response.status} inside the browser session"
            )
        content_type = response.headers.get("content-type", "video/mp4").split(";", 1)[0]
        return await response.body(), content_type

    async def _worker_loop(self) -> None:
        while True:
            job = await self._queue.get()
            self._running_job = True
            try:
                await self._run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._tasks[job.id] = UpstreamTask(
                    id=job.id,
                    status="failed",
                    progress=100,
                    error_code="browser_automation_error",
                    error_message=f"{exc.__class__.__name__}: {exc}",
                )
            finally:
                self._running_job = False
                self._queue.task_done()
                upload_root = Path(self.settings.sd2api_upload_dir).resolve()
                for item in job.media:
                    path = Path(item.path).resolve()
                    if path.is_relative_to(upload_root):
                        path.unlink(missing_ok=True)

    async def _run_job(self, job: BrowserJob) -> None:
        page = self._require_page()
        self._tasks[job.id] = UpstreamTask(id=job.id, status="running", progress=1)
        target_url = IMAGE_STUDIO_URL if job.mode == "image" else STUDIO_URL
        await page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(3_000)
        if not await self._is_logged_in(page):
            raise RuntimeError("TikTok session is no longer logged in")
        await self._ensure_terms_accepted(page)
        if job.advertiser_id:
            await self.select_subaccount(job.advertiser_id)
            if page.url.split("?", 1)[0] != target_url.split("?", 1)[0]:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(2_000)

        # Close transient overlays without inspecting authentication storage.
        await page.keyboard.press("Escape")
        await self._select_generation_mode(page, mode=job.mode)
        if job.mode == "image":
            await self._upload_input_image(page, job.media[0].path)
        elif job.mode == "reference":
            await self._upload_reference_media(page, list(job.media))

        await self._select_model(page, job.model)
        await self._select_duration(page, job.duration)
        editor = await self._find_prompt_editor(page)
        await editor.fill(job.prompt)

        previous_sources = await self._output_video_sources(page)
        composer = page.get_by_role("group", name="File drop zone")
        if not await composer.count():
            raise RuntimeError("Could not find the TikTok video composer")
        submit = composer.last.locator("button").last
        await submit.wait_for(state="visible")
        submit_deadline = time.monotonic() + 60
        while await submit.is_disabled() and time.monotonic() < submit_deadline:
            await page.wait_for_timeout(1_000)
        if await submit.is_disabled():
            raise RuntimeError("TikTok generate button is disabled")
        await submit.click()

        started = time.monotonic()
        deadline = started + self.settings.sd2api_browser_max_wait
        while time.monotonic() < deadline:
            await page.wait_for_timeout(5_000)
            sources = await self._output_video_sources(page)
            new_sources = [source for source in sources if source not in previous_sources]
            if not new_sources and len(sources) == 1 and sources[0] not in previous_sources:
                new_sources = sources
            if new_sources:
                source = new_sources[0]
                poster = await self._poster_for_source(page, source)
                self._tasks[job.id] = UpstreamTask(
                    id=job.id,
                    status="succeeded",
                    progress=100,
                    video_url=source,
                    poster_url=poster,
                    raw={
                        "backend": "browser",
                        "account_id": self.account_id,
                        "advertiser_id": job.advertiser_id,
                        "credits": await self._visible_credits(page),
                        "elapsed_seconds": int(time.monotonic() - started),
                    },
                )
                return

            failure = page.get_by_text(re.compile(r"failed|try again|couldn't generate", re.I))
            if await failure.count() and await failure.first.is_visible():
                message = (await failure.first.inner_text()).strip()
                self._tasks[job.id] = UpstreamTask(
                    id=job.id,
                    status="failed",
                    progress=100,
                    error_code="tiktok_generation_failed",
                    error_message=message,
                )
                return

            elapsed = time.monotonic() - started
            progress = min(98, max(2, int(elapsed / self.settings.sd2api_browser_max_wait * 100)))
            percentages = page.get_by_text(re.compile(r"^\d{1,3}%$"))
            for index in range(await percentages.count()):
                item = percentages.nth(index)
                if await item.is_visible():
                    match = re.match(r"^(\d+)%$", (await item.inner_text()).strip())
                    if match:
                        progress = min(99, int(match.group(1)))
                        break
            self._tasks[job.id] = UpstreamTask(id=job.id, status="running", progress=progress)

        self._tasks[job.id] = UpstreamTask(
            id=job.id,
            status="failed",
            progress=100,
            error_code="browser_timeout",
            error_message=f"TikTok did not finish within {self.settings.sd2api_browser_max_wait} seconds",
        )

    async def _select_model(self, page: Page, model: str) -> None:
        normalized = model.lower()
        if "mini" in normalized:
            target = "Dreamina Seedance 2.0 Mini"
        elif "fast" in normalized:
            target = "Dreamina Seedance 2.0 Fast"
        elif "2.5" in normalized or "2-5" in normalized:
            target = "Dreamina Seedance 2.5"
        else:
            target = "Dreamina Seedance 2.0"
        selectors = page.get_by_role(
            "button", name=re.compile(r"Dreamina Seedance|Video 1\.5", re.I)
        )
        visible = [
            selectors.nth(index)
            for index in range(await selectors.count())
            if await selectors.nth(index).is_visible()
        ]
        if not visible:
            raise RuntimeError("Could not find the TikTok model selector")
        current = visible[-1]
        current_text = (await current.inner_text()).strip()
        if target not in current_text:
            try:
                await current.click(timeout=5_000)
            except PlaywrightTimeoutError:
                # The generated-assets overview can briefly overlap the
                # composer after switching subaccounts. The control itself is
                # already visible and enabled, so a DOM-level click is safe.
                await page.keyboard.press("Escape")
                await current.click(force=True)
            options = page.get_by_text(target, exact=True)
            candidates = [
                options.nth(index)
                for index in range(await options.count())
                if await options.nth(index).is_visible()
            ]
            if not candidates:
                raise RuntimeError(f"TikTok does not expose model {target!r}")
            option = candidates[-1]
            container = option.locator("xpath=../..")
            disabled = (await container.get_attribute("aria-disabled")) == "true"
            classes = await container.get_attribute("class") or ""
            if disabled or "cursor-not-allowed" in classes:
                await page.keyboard.press("Escape")
                raise RuntimeError(
                    f"The active TikTok subaccount does not have access to {target}"
                )
            await option.click()

    @staticmethod
    async def _select_generation_mode(
        page: Page,
        *,
        mode: Literal["text", "image", "reference"],
    ) -> None:
        target = {
            "text": "Text to video",
            "image": "Image to video",
            "reference": "Reference to video",
        }[mode]
        if mode == "image" and await page.locator('input[type="file"][accept*="image" i]').count():
            return
        selectors = page.get_by_role(
            "button",
            name=re.compile(r"^(Text|Image|Reference) to video$", re.I),
        )
        if not await selectors.count():
            raise RuntimeError("Could not find the TikTok generation type selector")
        selector = selectors.last
        current = (await selector.inner_text()).strip()
        if current.lower() != target.lower():
            await selector.click()
            await page.wait_for_timeout(750)
            pattern = {
                "text": re.compile(r"Text\s*(?:to|-to-)\s*video", re.I),
                "image": re.compile(r"Image\s*(?:to|-to-)\s*video", re.I),
                "reference": re.compile(r"Reference\s*(?:to|-to-)\s*video", re.I),
            }[mode]
            option = page.get_by_text(pattern)
            visible_options = []
            for index in range(await option.count()):
                candidate = option.nth(index)
                if await candidate.is_visible():
                    visible_options.append(candidate)
            if not visible_options:
                raise RuntimeError(f"TikTok does not expose the {target!r} option")
            selected = visible_options[0]
            selected_length = len((await selected.inner_text()).strip())
            for candidate in visible_options[1:]:
                length = len((await candidate.inner_text()).strip())
                if length < selected_length:
                    selected = candidate
                    selected_length = length
            await selected.click()
            await page.wait_for_timeout(1_000)

    @staticmethod
    async def _upload_reference_media(page: Page, media: list[StagedMedia]) -> None:
        trigger = page.locator('button[aria-label="Upload"]:visible')
        await trigger.first.wait_for(state="visible", timeout=10_000)
        await trigger.first.click()
        await page.wait_for_timeout(500)

        upload_items = page.get_by_text(re.compile(r"^\s*Upload media\s*$", re.I))
        chosen = None
        for _attempt in range(20):
            for index in range(await upload_items.count()):
                candidate = upload_items.nth(index)
                if await candidate.is_visible():
                    chosen = candidate
                    break
            if chosen is not None:
                break
            await page.wait_for_timeout(500)
        if chosen is None:
            raise RuntimeError("Could not find a visible Upload media control")
        async with page.expect_file_chooser(timeout=15_000) as chooser_info:
            await chosen.click()
        chooser = await chooser_info.value
        if len(media) > 1 and not chooser.is_multiple():
            raise RuntimeError("TikTok reference upload control does not accept multiple files")
        await chooser.set_files([item.path for item in media])
        await page.wait_for_timeout(3_000)

    @staticmethod
    async def _upload_input_image(page: Page, image_path: str) -> None:
        inputs = page.locator('input[type="file"]')
        chosen = None
        for index in range(await inputs.count()):
            candidate = inputs.nth(index)
            accept = (await candidate.get_attribute("accept") or "").lower()
            if "image" in accept or any(ext in accept for ext in (".png", ".jpg", ".jpeg", ".webp")):
                chosen = candidate
                break
        if chosen is None and await inputs.count():
            chosen = inputs.last
        if chosen is not None:
            await chosen.set_input_files(image_path)
        else:
            trigger = page.get_by_role("button", name="Upload first frame", exact=True)
            if not await trigger.count():
                raise RuntimeError("Could not find the TikTok image upload control")
            await trigger.last.click()
            await page.wait_for_timeout(500)
            upload_option = page.get_by_text("Upload image", exact=True)
            if not await upload_option.count():
                raise RuntimeError("Could not find Upload image in the TikTok asset menu")
            async with page.expect_file_chooser(timeout=15_000) as chooser_info:
                await upload_option.last.click()
            chooser = await chooser_info.value
            await chooser.set_files(image_path)
        await page.wait_for_timeout(2_000)

        crop_actions = page.get_by_role(
            "button",
            name=re.compile(r"^(Confirm|Done|Apply|Use|Save)$", re.I),
        )
        for index in range(await crop_actions.count() - 1, -1, -1):
            action = crop_actions.nth(index)
            if await action.is_visible() and await action.is_enabled():
                await action.click()
                await page.wait_for_timeout(2_000)
                break

    async def _select_duration(self, page: Page, duration: int) -> None:
        duration_buttons = page.get_by_role("button", name=re.compile(r"^\d+s$"))
        if not await duration_buttons.count():
            raise RuntimeError("Could not find the TikTok duration selector")
        button = duration_buttons.last
        if (await button.inner_text()).strip() != f"{duration}s":
            await button.click()
            option = page.get_by_text(f"{duration}s", exact=True)
            await option.last.click()

    @staticmethod
    async def _find_prompt_editor(page: Page):
        composer = page.get_by_role("group", name="File drop zone")
        editors = composer.last.locator('[contenteditable="true"]')
        for index in range(await editors.count()):
            editor = editors.nth(index)
            if await editor.is_visible():
                return editor
        textareas = composer.last.locator("textarea")
        for index in range(await textareas.count()):
            editor = textareas.nth(index)
            if await editor.is_visible():
                return editor
        raise RuntimeError("Could not find the TikTok prompt editor")

    @staticmethod
    async def _output_video_sources(page: Page) -> set[str]:
        result: set[str] = set()
        videos = page.locator("video")
        for index in range(await videos.count()):
            source = await videos.nth(index).get_attribute("src")
            if source and source.startswith("http") and "tiktokcdn" in source:
                result.add(source)
        return result

    @staticmethod
    async def _poster_for_source(page: Page, source: str) -> str | None:
        videos = page.locator("video")
        for index in range(await videos.count()):
            video = videos.nth(index)
            if await video.get_attribute("src") == source:
                return await video.get_attribute("poster")
        return None

    def _set_login_state(self, state: str, error: str | None = None) -> None:
        self._login_state = state
        self._login_error = error

    @staticmethod
    def _search_frames(page: Page) -> list[Frame]:
        return list(page.frames)

    @classmethod
    async def _first_visible(cls, page: Page, selectors: tuple[str, ...]):
        for frame in cls._search_frames(page):
            for selector in selectors:
                locator = frame.locator(selector)
                for index in range(await locator.count()):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        return candidate
        return None

    @classmethod
    async def _wait_for_visible(
        cls,
        page: Page,
        selectors: tuple[str, ...],
        *,
        timeout: int,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            candidate = await cls._first_visible(page, selectors)
            if candidate is not None:
                return candidate
            await page.wait_for_timeout(500)
        raise RuntimeError(f"Could not find a visible login field matching {selectors!r}")

    @classmethod
    async def _open_password_login(cls, page: Page) -> None:
        credential_selectors = (
            'input[type="email"]',
            'input[name*="email" i]',
            'input[name*="username" i]',
            'input[placeholder*="email" i]',
            'input[aria-label*="email" i]',
            'input[autocomplete="username"]:not([name="mobile"])',
        )
        patterns = (
            re.compile(r"Use (?:phone|email|username)", re.I),
            re.compile(r"Log in with (?:email|username)", re.I),
            re.compile(r"Email / Username", re.I),
            re.compile(r"邮箱|用户名|账号密码"),
        )

        # The public landing page can finish rendering several seconds after
        # Playwright's domcontentloaded event. Wait for its login action rather
        # than checking only once at task startup.
        on_login_page = any(
            token in page.url.lower() for token in ("/login", "/signin")
        )
        if not on_login_page:
            landing_deadline = time.monotonic() + 30
            while time.monotonic() < landing_deadline:
                if await cls._first_visible(page, credential_selectors) is not None:
                    return
                opened = False
                for frame in cls._search_frames(page):
                    actions = frame.get_by_role(
                        "button",
                        name=re.compile(r"^(Log in|Sign in|登录|登入)$", re.I),
                    )
                    for index in range(await actions.count()):
                        action = actions.nth(index)
                        if await action.is_visible() and await action.is_enabled():
                            await action.click()
                            await page.wait_for_timeout(750)
                            opened = True
                            break
                    if opened:
                        break
                if opened:
                    break
                await page.wait_for_timeout(500)

        method_deadline = time.monotonic() + 15
        while time.monotonic() < method_deadline:
            if await cls._first_visible(page, credential_selectors) is not None:
                return

            switched_to_email = False
            for frame in cls._search_frames(page):
                switches = frame.locator("div.login-form__change_item").filter(
                    has_text=re.compile(r"^Use email$", re.I)
                )
                for index in range(await switches.count()):
                    item = switches.nth(index)
                    if await item.is_visible():
                        await item.click()
                        await page.wait_for_timeout(750)
                        switched_to_email = True
                        break
                if switched_to_email:
                    break
            if switched_to_email:
                continue

            method_selected = False
            for frame in cls._search_frames(page):
                for pattern in patterns:
                    candidate = frame.get_by_text(pattern)
                    for index in range(await candidate.count()):
                        item = candidate.nth(index)
                        if await item.is_visible():
                            await item.click()
                            await page.wait_for_timeout(750)
                            method_selected = True
                            break
                    if method_selected:
                        break
                if method_selected:
                    break
            await page.wait_for_timeout(500)
        raise RuntimeError("Could not switch the TikTok login form to email")

    @classmethod
    async def _click_login_action(cls, page: Page, pattern: re.Pattern[str]) -> None:
        for frame in cls._search_frames(page):
            buttons = frame.get_by_role("button", name=pattern)
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                if await button.is_visible() and await button.is_enabled():
                    await button.click()
                    return
        submit = await cls._first_visible(page, ('button[type="submit"]', 'input[type="submit"]'))
        if submit is None:
            raise RuntimeError(f"Could not find a login action matching {pattern.pattern!r}")
        await submit.click()

    @classmethod
    async def _captcha_visible(cls, page: Page) -> bool:
        for frame in cls._search_frames(page):
            if "captcha" in frame.url.lower():
                return True
            candidates = frame.locator(
                '[class*="captcha" i]:visible, [id*="captcha" i]:visible, '
                'iframe[src*="captcha" i]:visible'
            )
            if await candidates.count():
                return True
            try:
                text = (await frame.locator("body").inner_text()).lower()
            except Exception:
                continue
            if any(
                token in text
                for token in (
                    "drag the slider",
                    "select 2 objects",
                    "complete the verification",
                    "security verification",
                    "安全验证",
                    "请完成验证",
                    "拖动滑块",
                    "图形验证码",
                )
            ):
                return True
        return False

    @classmethod
    async def _visible_code_inputs(cls, page: Page) -> list[Any]:
        selectors = (
            'input[autocomplete="one-time-code"]',
            'input[name*="code" i]',
            'input[id*="code" i]',
            'input[aria-label*="code" i]',
            'input[placeholder*="code" i]',
            'input[placeholder*="verification" i]',
            'input[maxlength="6"]',
            'input[placeholder*="验证码"]',
            'input[inputmode="numeric"]',
        )
        result: list[Any] = []
        for frame in cls._search_frames(page):
            for selector in selectors:
                inputs = frame.locator(selector)
                for index in range(await inputs.count()):
                    item = inputs.nth(index)
                    if await item.is_visible() and item not in result:
                        result.append(item)
                if result:
                    return result

            # Some TikTok builds render the OTP as unlabelled text inputs.
            # Only broaden the selector when the page explicitly says that it
            # is waiting for a verification code, so the username field cannot
            # be mistaken for an OTP field.
            try:
                body_text = (await frame.locator("body").inner_text()).lower()
            except Exception:
                body_text = ""
            if any(
                token in body_text
                for token in (
                    "verification code",
                    "enter the code",
                    "code has been sent",
                    "邮箱验证码",
                    "输入验证码",
                )
            ):
                generic = frame.locator(
                    'input:visible:not([type="password"]):not([type="email"]):not([type="hidden"])'
                )
                for index in range(await generic.count()):
                    item = generic.nth(index)
                    if await item.is_visible():
                        result.append(item)
                if result:
                    return result
        return result

    @staticmethod
    async def _fill_code_inputs(inputs: list[Any], code: str) -> None:
        if len(inputs) == 1:
            await inputs[0].fill(code)
            return
        if len(inputs) < len(code):
            raise RuntimeError("TikTok verification form has fewer fields than the received code")
        fields = list(zip(inputs, code, strict=False))
        for index, (field, digit) in enumerate(fields):
            if index == len(fields) - 1:
                # TikTok's six-box OTP form submits from the final key event.
                # locator.fill() updates the value but does not fire that
                # keydown handler, so type the final character as a real key.
                await field.fill("")
                await field.click()
                await field.press(digit)
            else:
                await field.fill(digit)

    @classmethod
    async def _login_error_text(cls, page: Page) -> str | None:
        patterns = (
            "incorrect password",
            "invalid password",
            "account doesn't exist",
            "too many attempts",
            "密码错误",
            "账号不存在",
            "尝试次数过多",
        )
        for frame in cls._search_frames(page):
            try:
                text = (await frame.locator("body").inner_text()).lower()
            except Exception:
                continue
            for pattern in patterns:
                if pattern in text:
                    return f"TikTok login rejected the account: {pattern}"
        return None

    @staticmethod
    async def _is_logged_in(page: Page) -> bool:
        if any(token in page.url.lower() for token in ("login", "signin")):
            return False

        # The public landing page uses the same "Symphony Creative Studio"
        # branding as the authenticated app.  Treating that text as proof of a
        # session made a fresh profile look logged in while a visible "Log in"
        # button was still on screen.
        login_actions = page.get_by_role(
            "button", name=re.compile(r"^(Log in|Sign in|登录|登入)$", re.I)
        )
        for index in range(await login_actions.count()):
            if await login_actions.nth(index).is_visible():
                return False

        logout = page.get_by_text(
            re.compile(r"^(Log out|Sign out|退出登录|登出)$", re.I), exact=True
        )
        for index in range(await logout.count()):
            if await logout.nth(index).is_visible():
                return True

        # A successful login returns to the generation route. Do not trust the
        # route alone: a fresh logged-out page briefly keeps this URL while its
        # redirect is loading. Require a control that only exists in the actual
        # generation workspace as well.
        studio_url = page.url.lower()
        authenticated_routes = (
            "/creative/creativestudio/image-to-video",
            "/creative/creativestudio/create",
            "/creative/creativestudio/chat",
        )
        if not any(route in studio_url for route in authenticated_routes):
            return False
        account_identity = page.locator('nav p[class*="xl:block"]')
        for index in range(await account_identity.count()):
            if await account_identity.nth(index).is_visible():
                return True
        generation_controls = page.get_by_text(
            re.compile(
                r"^(Text|Image|Reference) to video$|^Dreamina Seedance 2\.0$",
                re.I,
            ),
            exact=False,
        )
        for index in range(await generation_controls.count()):
            if await generation_controls.nth(index).is_visible():
                return True
        return False

    async def _has_auth_session_cookie(
        self, context: BrowserContext | None = None
    ) -> bool:
        browser_context = context or self._context
        if browser_context is None:
            return False
        cookies = await browser_context.cookies([self.settings.tiktok_base_url])
        return any(
            str(cookie.get("name") or "") == "sessionid_ads"
            and bool(str(cookie.get("value") or "").strip())
            for cookie in cookies
        )

    async def _has_browser_login_candidate(self, page: Page) -> bool:
        if await self._is_logged_in(page):
            return True
        if any(token in page.url.lower() for token in ("login", "signin")):
            return False
        return await self._has_auth_session_cookie()

    @staticmethod
    async def _visible_credits(page: Page) -> int | None:
        candidates = page.get_by_role("button", name=re.compile(r"^\d{1,9}$"))
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if await candidate.is_visible():
                value = (await candidate.inner_text()).strip()
                if value.isdigit():
                    return int(value)
        # TikTok's newer studio shell renders the credit balance as plain text
        # inside the top navigation rather than as the button's accessible name.
        navigation = page.locator("nav:visible, header:visible")
        for index in range(await navigation.count()):
            text = await navigation.nth(index).inner_text()
            for line in text.splitlines():
                value = line.strip().replace(",", "")
                if re.fullmatch(r"\d{1,9}", value):
                    return int(value)
        return None

    async def _ensure_terms_accepted(self, page: Page) -> None:
        disclaimers = page.locator('[class*="ai-disclaimer"]:visible')
        terms_text = page.get_by_text(re.compile(r"Creative GenA[Il] Terms", re.I))
        accept_text = page.get_by_role(
            "button", name=re.compile(r"^Accept$", re.I)
        )
        visible_anchor = None
        for locator in (disclaimers, terms_text):
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    visible_anchor = candidate
                    break
            if visible_anchor is not None:
                break
        if visible_anchor is None:
            return
        visible_accept = None
        for index in range(await accept_text.count()):
            candidate = accept_text.nth(index)
            if await candidate.is_visible():
                visible_accept = candidate
                break
        if visible_accept is None:
            return
        await visible_anchor.evaluate(
            """
            element => {
              let node = element;
              while (node) {
                if (node.scrollHeight > node.clientHeight + 2) {
                  node.scrollTop = node.scrollHeight;
                  node.dispatchEvent(new Event('scroll', {bubbles: true}));
                  return;
                }
                node = node.parentElement;
              }
            }
            """
        )
        await page.wait_for_timeout(500)
        if not await visible_accept.is_enabled():
            raise TikTokUpstreamError(
                "TikTok Creative GenAI Terms Accept button stayed disabled after scrolling",
                status_code=409,
                code="terms_acceptance_failed",
            )
        await visible_accept.click()
        try:
            await visible_accept.wait_for(state="hidden", timeout=10_000)
        except PlaywrightTimeoutError as exc:
            raise TikTokUpstreamError(
                "TikTok Creative GenAI Terms remained visible after automatic acceptance",
                status_code=409,
                code="terms_acceptance_failed",
            ) from exc

    def _mark_context_closed(self, context: BrowserContext) -> None:
        if self._context is context:
            self._context = None
            self._page = None
            if self._login_state not in {"login_failed", "not_configured"}:
                self._set_login_state("browser_closed")

    def _browser_unavailable(self) -> bool:
        return self._context is None or self._page is None or self._page.is_closed()

    @staticmethod
    def _transient_page_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "frame was detached",
                "execution context was destroyed",
                "target page, context or browser has been closed",
            )
        )

    def _require_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            raise RuntimeError("Persistent browser is not running")
        return self._page

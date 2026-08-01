from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from .config import Settings
from .models import UpstreamTask
from .tiktok import T2V_MODELS, TikTokUpstreamError
from .uploads import StagedMedia


STUDIO_URL = (
    "https://ads.tiktok.com/creative/creativestudio/image-to-video"
    "?subApp=CreativeStudio/MiniApp/TextToVideo"
)
IMAGE_STUDIO_URL = "https://ads.tiktok.com/creative/creativestudio/image-to-video"


@dataclass(slots=True)
class BrowserJob:
    id: str
    prompt: str
    model: str
    duration: int
    mode: Literal["text", "image", "reference"] = "text"
    media: tuple[StagedMedia, ...] = ()


class BrowserTikTokClient:
    """TikTok client that drives a dedicated persistent Edge profile.

    The profile owns its authentication state. This class intentionally never
    reads cookies, localStorage, or the browser profile files.
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
        self._running_job = False

    async def start(self) -> dict[str, Any]:
        async with self._start_lock:
            if self._context is None:
                self._playwright = await async_playwright().start()
                profile = str(Path(self.profile_path).resolve())
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile,
                    channel=self.settings.sd2api_browser_channel or None,
                    headless=self.settings.sd2api_browser_headless,
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
                self._page.set_default_timeout(30_000)
                self._worker = asyncio.create_task(self._worker_loop(), name="sd2api-browser-worker")
            page = self._require_page()
            if not page.url.startswith("https://ads.tiktok.com/"):
                await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=90_000)
        return await self.status()

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        if self._context:
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def status(self) -> dict[str, Any]:
        if self._page is None or self._page.is_closed():
            return {
                "account_id": self.account_id,
                "running": False,
                "logged_in": False,
                "url": None,
                "queued": self._queue.qsize(),
                "busy": self._running_job,
                "credits": None,
            }
        logged_in = await self._is_logged_in(self._page)
        credits = await self._visible_credits(self._page) if logged_in else None
        return {
            "running": True,
            "logged_in": logged_in,
            "url": self._page.url,
            "queued": self._queue.qsize(),
            "busy": self._running_job,
            "account_id": self.account_id,
            "credits": credits,
        }

    @property
    def load(self) -> int:
        return self._queue.qsize() + int(self._running_job)

    async def create_text_video(self, *, prompt: str, model: str, duration: int) -> str:
        return await self._create_video(
            prompt=prompt,
            model=model,
            duration=duration,
            mode="text",
            media=[],
        )

    async def create_image_video(
        self, *, prompt: str, model: str, duration: int, image_path: str
    ) -> str:
        return await self._create_video(
            prompt=prompt,
            model=model,
            duration=duration,
            mode="image",
            media=[StagedMedia(kind="image", path=image_path)],
        )

    async def create_reference_video(
        self,
        *,
        prompt: str,
        model: str,
        duration: int,
        media: list[StagedMedia],
    ) -> str:
        return await self._create_video(
            prompt=prompt,
            model=model,
            duration=duration,
            mode="reference",
            media=media,
        )

    async def _create_video(
        self,
        *,
        prompt: str,
        model: str,
        duration: int,
        mode: Literal["text", "image", "reference"],
        media: list[StagedMedia],
    ) -> str:
        if model.lower() not in T2V_MODELS:
            raise TikTokUpstreamError(
                f"Unsupported model {model!r}. Browser mode currently supports Seedance aliases only",
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
                "The persistent Edge window is not logged in to TikTok Ads yet",
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
    ) -> None:
        self._tasks[task_id] = UpstreamTask(id=task_id, status="queued", progress=0)
        await self._queue.put(
            BrowserJob(task_id, prompt, model, duration, mode, tuple(media or []))
        )

    async def focus(self) -> None:
        await self.start()
        await self._require_page().bring_to_front()

    async def diagnostics(self, *, open_generation_menu: bool = False) -> dict[str, Any]:
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
        buttons = page.locator("button:visible")
        roles = page.locator('[role="option"]:visible, [role="menuitem"]:visible')
        upload_candidates = page.locator(
            'input, [class*="upload" i], [class*="drop" i], [aria-label*="upload" i]'
        )
        frames = []
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
        target = "Dreamina Seedance 2.5" if "2.5" in model else "Dreamina Seedance 2.0"
        current = page.get_by_role("button", name=re.compile(r"Dreamina Seedance"))
        if not await current.count():
            raise RuntimeError("Could not find the TikTok model selector")
        current_text = (await current.last.inner_text()).strip()
        if target not in current_text:
            await current.last.click()
            option = page.get_by_text(target, exact=True)
            await option.last.click()

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

    @staticmethod
    async def _is_logged_in(page: Page) -> bool:
        if any(token in page.url.lower() for token in ("login", "signin")):
            return False
        logout = page.get_by_text("Log out", exact=True)
        if await logout.count():
            return True
        studio = page.get_by_text("Symphony Creative Studio", exact=False)
        return bool(await studio.count())

    @staticmethod
    async def _visible_credits(page: Page) -> int | None:
        candidates = page.get_by_role("button", name=re.compile(r"^\d{1,9}$"))
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if await candidate.is_visible():
                value = (await candidate.inner_text()).strip()
                if value.isdigit():
                    return int(value)
        return None

    def _require_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            raise RuntimeError("Persistent browser is not running")
        return self._page

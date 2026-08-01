from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .browser_client import BrowserTikTokClient
from .config import Settings
from .models import UpstreamTask
from .store import TaskStore
from .tiktok import TikTokUpstreamError
from .uploads import StagedMedia


class BrowserPoolClient:
    """Schedules jobs across isolated, persistent browser account profiles."""

    def __init__(self, settings: Settings, store: TaskStore) -> None:
        self.settings = settings
        self.store = store
        self._workers: dict[str, BrowserTikTokClient] = {}
        self._task_accounts: dict[str, str] = {}
        self._scheduler_lock = asyncio.Lock()
        self._last_selected: dict[str, int] = {}
        self._selection_counter = 0

    def _profile_path(self, account_id: str) -> str:
        root = Path(self.settings.sd2api_browser_profile).resolve()
        return str(root / account_id)

    def _worker(self, account_id: str) -> BrowserTikTokClient:
        account = self.store.get_account(account_id)
        if account is None:
            raise TikTokUpstreamError(
                f"Account {account_id!r} does not exist",
                status_code=404,
                code="account_not_found",
            )
        worker = self._workers.get(account_id)
        if worker is None:
            worker = BrowserTikTokClient(
                self.settings,
                account_id=account_id,
                profile_path=self._profile_path(account_id),
            )
            self._workers[account_id] = worker
        return worker

    async def start(self) -> dict[str, Any]:
        accounts = [account for account in self.store.list_accounts() if account["enabled"]]

        semaphore = asyncio.Semaphore(self.settings.sd2api_pool_start_concurrency)

        async def start_one(account_id: str) -> dict[str, Any]:
            async with semaphore:
                return await self.start_account(account_id)

        results = await asyncio.gather(
            *(start_one(account["id"]) for account in accounts),
            return_exceptions=True,
        )
        errors = {
            account["id"]: f"{result.__class__.__name__}: {result}"
            for account, result in zip(accounts, results, strict=True)
            if isinstance(result, Exception)
        }
        return {"mode": "browser_pool", "accounts": await self.list_accounts(), "errors": errors}

    async def stop(self) -> None:
        workers = list(self._workers.values())
        await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)
        self._workers.clear()

    async def status(self) -> dict[str, Any]:
        accounts = await self.list_accounts()
        return {
            "mode": "browser_pool",
            "accounts": accounts,
            "total": len(accounts),
            "logged_in": sum(bool(account.get("logged_in")) for account in accounts),
            "running_jobs": sum(bool(account.get("busy")) for account in accounts),
            "queued_jobs": sum(int(account.get("queued", 0)) for account in accounts),
            "max_parallel": sum(
                bool(account.get("enabled")) and bool(account.get("logged_in"))
                for account in accounts
            ),
        }

    async def list_accounts(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for account in self.store.list_accounts():
            worker = self._workers.get(account["id"])
            runtime = await worker.status() if worker else {
                "running": False,
                "logged_in": False,
                "queued": 0,
                "busy": False,
                "url": None,
                "credits": None,
            }
            result.append({**account, **runtime})
        return result

    async def add_account(self, *, account_id: str, name: str, start: bool) -> dict[str, Any]:
        try:
            self.store.create_account(account_id=account_id, name=name)
        except Exception as exc:
            if self.store.get_account(account_id) is not None:
                raise TikTokUpstreamError(
                    f"Account {account_id!r} already exists",
                    status_code=409,
                    code="account_exists",
                ) from exc
            raise
        if start:
            await self.start_account(account_id)
        return await self.account_status(account_id)

    async def update_account(
        self, account_id: str, *, name: str | None = None, enabled: bool | None = None
    ) -> dict[str, Any]:
        if enabled is False:
            await self.stop_account(account_id)
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = name
        if enabled is not None:
            changes["enabled"] = enabled
        if changes:
            try:
                self.store.update_account(account_id, **changes)
            except KeyError as exc:
                raise TikTokUpstreamError(
                    f"Account {account_id!r} does not exist",
                    status_code=404,
                    code="account_not_found",
                ) from exc
        return await self.account_status(account_id)

    async def delete_account(self, account_id: str) -> None:
        await self.stop_account(account_id)
        if not self.store.delete_account(account_id):
            raise TikTokUpstreamError(
                f"Account {account_id!r} does not exist",
                status_code=404,
                code="account_not_found",
            )

    async def start_account(self, account_id: str) -> dict[str, Any]:
        account = self.store.get_account(account_id)
        if account is None:
            raise TikTokUpstreamError(
                f"Account {account_id!r} does not exist",
                status_code=404,
                code="account_not_found",
            )
        if not account["enabled"]:
            raise TikTokUpstreamError(
                f"Account {account_id!r} is disabled",
                status_code=409,
                code="account_disabled",
            )
        await self._worker(account_id).start()
        return await self.account_status(account_id)

    async def stop_account(self, account_id: str, *, force: bool = False) -> None:
        worker = self._workers.pop(account_id, None)
        if worker:
            if worker.load > 0 and not force:
                self._workers[account_id] = worker
                raise TikTokUpstreamError(
                    f"Account {account_id!r} still has running or queued tasks",
                    status_code=409,
                    code="account_busy",
                )
            await worker.stop()

    async def focus_account(self, account_id: str) -> dict[str, Any]:
        worker = self._worker(account_id)
        await worker.focus()
        return await self.account_status(account_id)

    async def diagnostics(
        self, *, account_id: str | None = None, open_generation_menu: bool = False
    ) -> dict[str, Any]:
        if not account_id:
            running = [key for key, worker in self._workers.items() if worker.load == 0]
            if not running:
                raise TikTokUpstreamError(
                    "No idle browser account is available for diagnostics",
                    status_code=409,
                    code="diagnostic_account_unavailable",
                )
            account_id = sorted(running)[0]
        return await self._worker(account_id).diagnostics(
            open_generation_menu=open_generation_menu
        )

    async def account_status(self, account_id: str) -> dict[str, Any]:
        account = self.store.get_account(account_id)
        if account is None:
            raise TikTokUpstreamError(
                f"Account {account_id!r} does not exist",
                status_code=404,
                code="account_not_found",
            )
        worker = self._workers.get(account_id)
        runtime = await worker.status() if worker else {
            "running": False,
            "logged_in": False,
            "queued": 0,
                "busy": False,
                "url": None,
                "credits": None,
        }
        return {**account, **runtime}

    async def create_text_video(self, *, prompt: str, model: str, duration: int) -> str:
        return await self._schedule(
            prompt=prompt,
            model=model,
            duration=duration,
            mode="text",
            media=[],
        )

    async def create_image_video(
        self, *, prompt: str, model: str, duration: int, image_path: str
    ) -> str:
        return await self._schedule(
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
        return await self._schedule(
            prompt=prompt,
            model=model,
            duration=duration,
            mode="reference",
            media=media,
        )

    async def _schedule(
        self,
        *,
        prompt: str,
        model: str,
        duration: int,
        mode: str,
        media: list[StagedMedia],
    ) -> str:
        async with self._scheduler_lock:
            statuses = await self.list_accounts()
            eligible = [
                status
                for status in statuses
                if status["enabled"] and status["running"] and status["logged_in"]
                and status.get("credits") != 0
            ]
            if not eligible:
                raise TikTokUpstreamError(
                    "No enabled, logged-in browser accounts are available",
                    status_code=503,
                    code="account_pool_unavailable",
                )
            total_pending = sum(self._worker(item["id"]).load for item in eligible)
            if total_pending >= self.settings.sd2api_pool_max_pending:
                raise TikTokUpstreamError(
                    "The browser account pool queue is full",
                    status_code=429,
                    code="pool_queue_full",
                )
            selected = min(
                eligible,
                key=lambda item: (
                    self._worker(item["id"]).load,
                    -int(item["credits"]) if isinstance(item.get("credits"), int) else 1,
                    self._last_selected.get(item["id"], -1),
                    item["id"],
                ),
            )
            account_id = selected["id"]
            worker = self._worker(account_id)
            if mode == "image":
                task_id = await worker.create_image_video(
                    prompt=prompt,
                    model=model,
                    duration=duration,
                    image_path=media[0].path,
                )
            elif mode == "reference":
                task_id = await worker.create_reference_video(
                    prompt=prompt,
                    model=model,
                    duration=duration,
                    media=media,
                )
            else:
                task_id = await worker.create_text_video(
                    prompt=prompt,
                    model=model,
                    duration=duration,
                )
            self._selection_counter += 1
            self._last_selected[account_id] = self._selection_counter
            self._task_accounts[task_id] = account_id
            return task_id

    def account_for_task(self, task_id: str) -> str | None:
        return self._task_accounts.get(task_id)

    async def check_task(self, task_id: str) -> UpstreamTask:
        account_id = self._task_accounts.get(task_id)
        if account_id is None:
            raise TikTokUpstreamError(
                f"Pool task {task_id!r} is not active in this process",
                status_code=404,
                code="task_not_active",
            )
        return await self._worker(account_id).check_task(task_id)

    async def fetch_video(self, video_url: str, account_id: str | None) -> tuple[bytes, str]:
        if not account_id:
            raise TikTokUpstreamError(
                "The completed task has no assigned account",
                status_code=409,
                code="task_account_missing",
            )
        return await self._worker(account_id).fetch_video(video_url)

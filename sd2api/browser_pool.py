from __future__ import annotations

import asyncio
from pathlib import Path
import time
from typing import Any

from .browser_client import BrowserTikTokClient
from .config import Settings
from .models import UpstreamTask
from .security import CredentialError, CredentialVault
from .store import TaskStore
from .temp_mail import TempMailClient
from .tiktok import TikTokUpstreamError
from .uploads import StagedMedia


class BrowserPoolClient:
    """Schedules jobs across isolated, persistent browser account profiles."""

    def __init__(self, settings: Settings, store: TaskStore) -> None:
        self.settings = settings
        self.store = store
        self._workers: dict[str, BrowserTikTokClient] = {}
        self._task_accounts: dict[str, str] = {}
        self._task_advertisers: dict[str, str] = {}
        self._scheduler_lock = asyncio.Lock()
        self._last_selected: dict[str, int] = {}
        self._selection_counter = 0
        self._login_tasks: dict[str, asyncio.Task[None]] = {}
        self._monitor_task: asyncio.Task[None] | None = None

    def _vault(self) -> CredentialVault:
        return CredentialVault(self.settings.credential_master_key)

    def _mail_client(self) -> TempMailClient:
        return TempMailClient(
            base_url=self.settings.sd2api_temp_mail_base_url,
            api_key=self.settings.sd2api_temp_mail_api_key,
            poll_seconds=self.settings.sd2api_temp_mail_poll_seconds,
            timeout_seconds=self.settings.sd2api_temp_mail_timeout,
        )

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
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(
                self._login_monitor_loop(), name="sd2api-account-login-monitor"
            )
        return {"mode": "browser_pool", "accounts": await self.list_accounts(), "errors": errors}

    async def stop(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        login_tasks = list(self._login_tasks.values())
        for task in login_tasks:
            task.cancel()
        await asyncio.gather(*login_tasks, return_exceptions=True)
        self._login_tasks.clear()
        workers = list(self._workers.values())
        await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)
        self._workers.clear()

    async def status(self) -> dict[str, Any]:
        accounts = await self.list_accounts()
        subaccounts = [item for account in accounts for item in account["subaccounts"]]
        return {
            "mode": "browser_pool",
            "accounts": accounts,
            "total": len(accounts),
            "logged_in": sum(bool(account.get("logged_in")) for account in accounts),
            "running_jobs": sum(bool(account.get("busy")) for account in accounts),
            "queued_jobs": sum(int(account.get("queued", 0)) for account in accounts),
            "max_parallel": sum(
                bool(account.get("enabled"))
                and bool(account.get("logged_in"))
                and any(self._subaccount_eligible(item) for item in account["subaccounts"])
                for account in accounts
            ),
            "subaccounts": len(subaccounts),
            "enabled_subaccounts": sum(bool(item["enabled"]) for item in subaccounts),
            "eligible_subaccounts": sum(
                self._subaccount_eligible(item) for item in subaccounts
            ),
            "logging_in": sum(
                account.get("login_state")
                in {"opening_login", "entering_credentials", "waiting_for_login", "waiting_email_code", "submitting_email_code"}
                for account in accounts
            ),
            "captcha_required": sum(
                account.get("login_state") == "captcha_required" for account in accounts
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
                "login_state": account.get("login_state", "not_started"),
                "login_error": account.get("last_error"),
                "last_login_at": account.get("last_login_at"),
            }
            result.append(
                {
                    **account,
                    **runtime,
                    "subaccounts": self.store.list_subaccounts(account["id"]),
                }
            )
        return result

    @staticmethod
    def _subaccount_eligible(item: dict[str, Any]) -> bool:
        return bool(item.get("enabled")) and item.get("seedance_access") is True and (
            item.get("credits") is None or int(item["credits"]) > 0
        )

    async def add_account(
        self,
        *,
        account_id: str,
        name: str,
        start: bool,
        username: str | None = None,
        password: str | None = None,
        email_address: str | None = None,
        auto_login: bool = True,
    ) -> dict[str, Any]:
        password_ciphertext = None
        if username and password:
            password_ciphertext = self._vault().encrypt(password)
        try:
            self.store.create_account(
                account_id=account_id,
                name=name,
                username=username,
                password_ciphertext=password_ciphertext,
                email_address=email_address or username,
                auto_login=auto_login,
            )
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
        self,
        account_id: str,
        *,
        name: str | None = None,
        enabled: bool | None = None,
        username: str | None = None,
        password: str | None = None,
        email_address: str | None = None,
        auto_login: bool | None = None,
    ) -> dict[str, Any]:
        if enabled is False:
            await self.stop_account(account_id)
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = name
        if enabled is not None:
            changes["enabled"] = enabled
        if username is not None:
            changes["username"] = username
        if password is not None:
            changes["password_ciphertext"] = self._vault().encrypt(password)
            changes["login_state"] = "pending"
            changes["last_error"] = None
        if email_address is not None:
            changes["email_address"] = email_address
        if auto_login is not None:
            changes["auto_login"] = auto_login
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
        status = await self.account_status(account_id)
        if (
            self.settings.sd2api_auto_login
            and account.get("auto_login")
            and account.get("credentials_configured")
            and not status["logged_in"]
        ):
            self._schedule_login(account_id)
        elif status["logged_in"]:
            try:
                await self.refresh_subaccounts(account_id, check_access=True)
            except Exception as exc:
                self.store.update_account(
                    account_id,
                    last_error=f"Subaccount scan failed: {exc.__class__.__name__}: {exc}",
                )
        return await self.account_status(account_id)

    async def stop_account(self, account_id: str, *, force: bool = False) -> None:
        login_task = self._login_tasks.get(account_id)
        if login_task and not login_task.done():
            if not force:
                raise TikTokUpstreamError(
                    f"Account {account_id!r} is currently logging in",
                    status_code=409,
                    code="account_login_busy",
                )
            login_task.cancel()
            await asyncio.gather(login_task, return_exceptions=True)
            self._login_tasks.pop(account_id, None)
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

    async def login_account(self, account_id: str, *, wait: bool = False) -> dict[str, Any]:
        task = self._schedule_login(account_id)
        if wait:
            await task
        return await self.account_status(account_id)

    def _schedule_login(self, account_id: str) -> asyncio.Task[None]:
        current = self._login_tasks.get(account_id)
        if current and not current.done():
            return current
        task = asyncio.create_task(
            self._run_login(account_id), name=f"sd2api-login-{account_id}"
        )
        self._login_tasks[account_id] = task
        return task

    async def _run_login(self, account_id: str) -> None:
        try:
            credentials = self.store.account_credentials(account_id)
            if credentials is None:
                raise CredentialError("This account has no stored username and password")
            password = self._vault().decrypt(credentials["password_ciphertext"])
            self.store.update_account(
                account_id,
                login_state="logging_in",
                last_login_attempt=int(time.time()),
                last_error=None,
            )
            worker = self._worker(account_id)
            result = await worker.login(
                username=credentials["username"],
                password=password,
                email_address=credentials["email_address"],
                mail_client=self._mail_client(),
            )
            self.store.update_account(
                account_id,
                login_state=str(result.get("login_state") or "logged_in"),
                last_login_at=int(time.time()),
                last_error=None,
            )
            try:
                discovered = await worker.scan_subaccounts(check_access=True)
                self.store.upsert_subaccounts(account_id, discovered)
            except Exception as scan_exc:
                self.store.update_account(
                    account_id,
                    last_error=(
                        f"Subaccount scan failed: {scan_exc.__class__.__name__}: {scan_exc}"
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                self.store.update_account(
                    account_id,
                    login_state="login_failed",
                    last_error=f"{exc.__class__.__name__}: {exc}",
                )
            except KeyError:
                pass
        finally:
            current = self._login_tasks.get(account_id)
            if current is asyncio.current_task():
                self._login_tasks.pop(account_id, None)

    async def _login_monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.sd2api_relogin_interval)
            if not self.settings.sd2api_auto_login:
                continue
            for account in self.store.list_accounts():
                if not (
                    account["enabled"]
                    and account.get("auto_login")
                    and account.get("credentials_configured")
                ):
                    continue
                worker = self._workers.get(account["id"])
                if worker is None or worker.load > 0:
                    continue
                status = await worker.status()
                if not status["logged_in"]:
                    self._schedule_login(account["id"])

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

    async def refresh_subaccounts(
        self, account_id: str, *, check_access: bool = True
    ) -> dict[str, Any]:
        account = self.store.get_account(account_id)
        if account is None:
            raise TikTokUpstreamError(
                f"Account {account_id!r} does not exist",
                status_code=404,
                code="account_not_found",
            )
        worker = self._worker(account_id)
        if worker.load > 0:
            raise TikTokUpstreamError(
                f"Account {account_id!r} is busy and cannot switch subaccounts",
                status_code=409,
                code="account_busy",
            )
        await worker.start()
        status = await worker.status()
        if not status["logged_in"]:
            raise TikTokUpstreamError(
                f"Account {account_id!r} must be logged in before scanning subaccounts",
                status_code=401,
                code="browser_login_required",
            )
        discovered = await worker.scan_subaccounts(check_access=check_access)
        self.store.upsert_subaccounts(account_id, discovered)
        return await self.account_status(account_id)

    async def set_subaccount_enabled(
        self, account_id: str, advertiser_id: str, *, enabled: bool
    ) -> dict[str, Any]:
        items = self.store.list_subaccounts(account_id)
        target = next(
            (item for item in items if item["advertiser_id"] == advertiser_id), None
        )
        if target is None:
            raise TikTokUpstreamError(
                f"Subaccount {advertiser_id!r} was not discovered for {account_id!r}",
                status_code=404,
                code="subaccount_not_found",
            )
        if enabled and target.get("seedance_access") is not True:
            raise TikTokUpstreamError(
                f"Subaccount {advertiser_id!r} has no verified Seedance 2 access",
                status_code=409,
                code="seedance_access_required",
            )
        updated = self.store.set_subaccount_enabled(account_id, advertiser_id, enabled)
        return updated

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
            "login_state": account.get("login_state", "not_started"),
            "login_error": account.get("last_error"),
            "last_login_at": account.get("last_login_at"),
        }
        return {
            **account,
            **runtime,
            "subaccounts": self.store.list_subaccounts(account_id),
        }

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
            eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for status in statuses:
                if not (status["enabled"] and status["running"] and status["logged_in"]):
                    continue
                eligible.extend(
                    (status, subaccount)
                    for subaccount in status["subaccounts"]
                    if self._subaccount_eligible(subaccount)
                )
            if not eligible:
                raise TikTokUpstreamError(
                    "No selected subaccounts with verified Seedance 2 access and credits are available",
                    status_code=503,
                    code="account_pool_unavailable",
                )
            eligible_workers = {status["id"] for status, _ in eligible}
            total_pending = sum(self._worker(account_id).load for account_id in eligible_workers)
            if total_pending >= self.settings.sd2api_pool_max_pending:
                raise TikTokUpstreamError(
                    "The browser account pool queue is full",
                    status_code=429,
                    code="pool_queue_full",
                )
            selected_account, selected_subaccount = min(
                eligible,
                key=lambda pair: (
                    self._worker(pair[0]["id"]).load,
                    -int(pair[1]["credits"])
                    if isinstance(pair[1].get("credits"), int)
                    else 1,
                    self._last_selected.get(
                        f'{pair[0]["id"]}:{pair[1]["advertiser_id"]}', -1
                    ),
                    pair[0]["id"],
                    pair[1]["advertiser_id"],
                ),
            )
            account_id = selected_account["id"]
            advertiser_id = selected_subaccount["advertiser_id"]
            worker = self._worker(account_id)
            if mode == "image":
                task_id = await worker.create_image_video(
                    prompt=prompt,
                    model=model,
                    duration=duration,
                    image_path=media[0].path,
                    advertiser_id=advertiser_id,
                )
            elif mode == "reference":
                task_id = await worker.create_reference_video(
                    prompt=prompt,
                    model=model,
                    duration=duration,
                    media=media,
                    advertiser_id=advertiser_id,
                )
            else:
                task_id = await worker.create_text_video(
                    prompt=prompt,
                    model=model,
                    duration=duration,
                    advertiser_id=advertiser_id,
                )
            self._selection_counter += 1
            self._last_selected[f"{account_id}:{advertiser_id}"] = self._selection_counter
            self._task_accounts[task_id] = account_id
            self._task_advertisers[task_id] = advertiser_id
            return task_id

    def account_for_task(self, task_id: str) -> str | None:
        return self._task_accounts.get(task_id)

    def advertiser_for_task(self, task_id: str) -> str | None:
        return self._task_advertisers.get(task_id)

    async def check_task(self, task_id: str) -> UpstreamTask:
        account_id = self._task_accounts.get(task_id)
        if account_id is None:
            raise TikTokUpstreamError(
                f"Pool task {task_id!r} is not active in this process",
                status_code=404,
                code="task_not_active",
            )
        task = await self._worker(account_id).check_task(task_id)
        advertiser_id = self._task_advertisers.get(task_id)
        credits = task.raw.get("credits") if task.raw else None
        if advertiser_id and isinstance(credits, int):
            try:
                self.store.update_subaccount(
                    account_id,
                    advertiser_id,
                    credits=credits,
                    last_checked_at=int(time.time()),
                )
            except KeyError:
                pass
        return task

    async def fetch_video(self, video_url: str, account_id: str | None) -> tuple[bytes, str]:
        if not account_id:
            raise TikTokUpstreamError(
                "The completed task has no assigned account",
                status_code=409,
                code="task_account_missing",
            )
        return await self._worker(account_id).fetch_video(video_url)

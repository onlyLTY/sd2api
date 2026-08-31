from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from typing import Any
import uuid

from .browser_client import BrowserTikTokClient
from .config import Settings
from .models import UpstreamTask
from .protocol import ProtocolSession, ProtocolTikTokClient
from .security import CredentialError, CredentialVault
from .store import TaskStore
from .temp_mail import TempMailClient
from .tiktok import (
    TikTokUpstreamError,
    is_tiktok_authentication_error,
    tiktok_authentication_error,
)
from .uploads import StagedMedia


class _KeepalivePending(Exception):
    """Internal signal that scheduling should wait for browser maintenance."""


class BrowserPoolClient:
    """Schedules jobs across isolated, persistent browser account profiles."""

    def __init__(self, settings: Settings, store: TaskStore) -> None:
        self.settings = settings
        self.store = store
        self._workers: dict[str, BrowserTikTokClient] = {}
        self._protocol_clients: dict[tuple[str, str | None], ProtocolTikTokClient] = {}
        self._started_accounts: set[str] = set()
        self._task_accounts: dict[str, str] = {}
        self._task_advertisers: dict[str, str] = {}
        self._scheduler_lock = asyncio.Lock()
        self._last_selected: dict[str, int] = {}
        self._selection_counter = 0
        self._login_tasks: dict[str, asyncio.Task[None]] = {}
        self._monitor_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._keepalive_accounts: set[str] = set()
        self._keepalive_condition = asyncio.Condition()
        self._keepalive_revision = 0
        self._keepalive_waiters = 0

    def _vault(self) -> CredentialVault:
        return CredentialVault(self.settings.credential_master_key)

    def _mail_client(self) -> TempMailClient:
        return TempMailClient(
            base_url=self.settings.sd2api_temp_mail_base_url,
            api_key=self.settings.sd2api_temp_mail_api_key,
            poll_seconds=self.settings.sd2api_temp_mail_poll_seconds,
            timeout_seconds=self.settings.sd2api_temp_mail_timeout,
        )

    def _protocol_session(self, account_id: str) -> ProtocolSession | None:
        ciphertext = self.store.account_session(account_id)
        if not ciphertext:
            return None
        try:
            value = json.loads(self._vault().decrypt(ciphertext))
            if not isinstance(value, dict):
                raise ValueError("Session payload is not an object")
            return ProtocolSession.from_dict(value)
        except (CredentialError, ValueError, json.JSONDecodeError) as exc:
            raise TikTokUpstreamError(
                f"Stored protocol session is unreadable: {exc}",
                status_code=500,
                code="protocol_session_invalid",
            ) from exc

    def _protocol_client(
        self, account_id: str, advertiser_id: str | None = None
    ) -> ProtocolTikTokClient:
        key = (account_id, advertiser_id)
        client = self._protocol_clients.get(key)
        if client is not None:
            return client
        session = self._protocol_session(account_id)
        if session is None:
            raise TikTokUpstreamError(
                f"Account {account_id!r} has no captured protocol session",
                status_code=401,
                code="protocol_login_required",
            )
        client = ProtocolTikTokClient(
            self.settings,
            session,
            account_id=account_id,
            advertiser_id=advertiser_id,
        )
        self._protocol_clients[key] = client
        return client

    async def _close_protocol_clients(self, account_id: str | None = None) -> None:
        selected = [
            (key, client)
            for key, client in self._protocol_clients.items()
            if account_id is None or key[0] == account_id
        ]
        for key, _ in selected:
            self._protocol_clients.pop(key, None)
        await asyncio.gather(*(client.close() for _, client in selected), return_exceptions=True)

    def _profile_path(self, account_id: str) -> str:
        root = Path(self.settings.sd2api_browser_profile).resolve()
        account = self.store.get_account(account_id)
        username = str((account or {}).get("username") or "").strip().lower()
        if not username:
            return str(root / account_id)
        profile_key = hashlib.sha256(username.encode("utf-8")).hexdigest()[:24]
        stable = root / f"user_{profile_key}"
        legacy = root / account_id
        # Migrate the currently retained account-id profile once. Future
        # delete/re-add operations for the same email resolve to `stable`.
        if legacy.exists() and not stable.exists():
            legacy.rename(stable)
        return str(stable)

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

    async def _capture_protocol_session(
        self,
        account_id: str,
        worker: BrowserTikTokClient,
        *,
        bootstrap_identity: bool = True,
        identity_fallback: ProtocolSession | None = None,
        validate: bool = False,
    ) -> ProtocolSession:
        exported = await worker.export_protocol_session(
            bootstrap_identity=bootstrap_identity
        )
        if identity_fallback is not None:
            for name in (
                "device_id",
                "user_agent",
                "fp_id",
                "sec_ch_ua",
                "sec_ch_ua_mobile",
                "sec_ch_ua_platform",
            ):
                if not exported.get(name):
                    exported[name] = getattr(identity_fallback, name)
        session = ProtocolSession.from_dict(exported)
        if validate:
            validator = ProtocolTikTokClient(
                self.settings, session, account_id=account_id
            )
            try:
                await validator.validate()
            finally:
                await validator.close()
        ciphertext = self._vault().encrypt(
            json.dumps(session.to_dict(), ensure_ascii=False, separators=(",", ":"))
        )
        await self._close_protocol_clients(account_id)
        captured_at = int(time.time())
        self.store.update_account(
            account_id,
            session_ciphertext=ciphertext,
            session_updated_at=captured_at,
            login_state="logged_in",
            last_error=None,
            keepalive_state="idle",
            keepalive_next_at=(
                captured_at + self.settings.sd2api_session_keepalive_interval
            ),
            keepalive_error=None,
        )
        return session

    async def start(self) -> dict[str, Any]:
        self._ensure_background_tasks()
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

    def _ensure_background_tasks(self) -> None:
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(
                self._login_monitor_loop(), name="sd2api-account-login-monitor"
            )
        if self._keepalive_task is None:
            self._recover_interrupted_keepalives()
            self._keepalive_task = asyncio.create_task(
                self._session_keepalive_loop(), name="sd2api-session-keepalive"
            )

    async def stop(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            await asyncio.gather(self._keepalive_task, return_exceptions=True)
            self._keepalive_task = None
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
        await self._close_protocol_clients()
        self._started_accounts.clear()
        self._keepalive_accounts.clear()

    async def status(self) -> dict[str, Any]:
        accounts = await self.list_accounts()
        subaccounts = [item for account in accounts for item in account["subaccounts"]]
        schedulable_subaccounts = [
            item
            for account in accounts
            if (
                account.get("enabled")
                and account.get("logged_in")
                and not account.get("keepalive_active")
            )
            for item in account["subaccounts"]
            if self._subaccount_eligible(item)
        ]
        return {
            "mode": "browser_pool",
            "accounts": accounts,
            "total": len(accounts),
            "logged_in": sum(bool(account.get("logged_in")) for account in accounts),
            "running_jobs": sum(int(account.get("queued", 0)) for account in accounts),
            "queued_jobs": sum(int(account.get("queued", 0)) for account in accounts),
            "max_parallel": (
                max(
                    0,
                    self.settings.sd2api_pool_max_pending
                    - self._pool_load(accounts),
                )
                if schedulable_subaccounts
                else 0
            ),
            "subaccounts": len(subaccounts),
            "enabled_subaccounts": sum(bool(item["enabled"]) for item in subaccounts),
            "eligible_subaccounts": sum(
                self._subaccount_eligible(item) for item in subaccounts
            ),
            "quota_blocked_subaccounts": sum(
                bool(item.get("quota_blocked")) for item in subaccounts
            ),
            "rate_limited_subaccounts": sum(
                bool(item.get("rate_limited")) for item in subaccounts
            ),
            "logging_in": sum(
                account.get("login_state")
                in {"opening_login", "entering_credentials", "waiting_for_login", "waiting_email_code", "submitting_email_code"}
                for account in accounts
            ),
            "captcha_required": sum(
                account.get("login_state") == "captcha_required" for account in accounts
            ),
            "keepalive_waiting_requests": self._keepalive_waiters,
        }

    async def list_accounts(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for account in self.store.list_accounts():
            worker = self._workers.get(account["id"])
            try:
                if worker:
                    runtime = await worker.status()
                elif (
                    account["id"] in self._started_accounts
                    and account.get("session_available")
                ):
                    protocol_load = self._account_load(account["id"])
                    runtime = {
                        "running": True,
                        "browser_running": False,
                        "backend": "protocol",
                        "logged_in": True,
                        "queued": protocol_load,
                        "busy": protocol_load > 0,
                        "url": None,
                        "credits": None,
                        "login_state": "logged_in",
                        "login_error": account.get("last_error"),
                        "last_login_at": account.get("last_login_at"),
                        "active_advertiser_id": None,
                    }
                else:
                    runtime = self._stopped_runtime(account)
            except Exception as exc:
                runtime = self._stopped_runtime(
                    account,
                    state="browser_error",
                    error=f"{exc.__class__.__name__}: {exc}",
                )
            runtime.setdefault("browser_running", bool(worker))
            runtime.setdefault("backend", "browser" if worker else "stopped")
            runtime["keepalive_active"] = account["id"] in self._keepalive_accounts
            result.append(
                {
                    **account,
                    **runtime,
                    "subaccounts": self._decorate_subaccounts(
                        account["id"], self.store.list_subaccounts(account["id"])
                    ),
                }
            )
        return result

    @staticmethod
    def _stopped_runtime(
        account: dict[str, Any], *, state: str | None = None, error: str | None = None
    ) -> dict[str, Any]:
        return {
            "running": False,
            "browser_running": False,
            "backend": "stopped",
            "logged_in": False,
            "queued": 0,
            "busy": False,
            "url": None,
            "credits": None,
            "login_state": state or account.get("login_state", "not_started"),
            "login_error": error or account.get("last_error"),
            "last_login_at": account.get("last_login_at"),
            "active_advertiser_id": None,
        }

    @staticmethod
    def _subaccount_eligible(item: dict[str, Any]) -> bool:
        return bool(item.get("enabled")) and item.get("seedance_access") is True and (
            item.get("credits") is None or int(item["credits"]) > 0
        ) and max(
            int(item.get("quota_blocked_until") or 0),
            int(item.get("rate_limited_until") or 0),
        ) <= int(time.time())

    def _subaccount_load(self, account_id: str, advertiser_id: str) -> int:
        stored = len(self.store.active_task_ids(account_id, advertiser_id))
        client = self._protocol_clients.get((account_id, advertiser_id))
        return max(stored, client.load if client is not None else 0)

    def _account_load(self, account_id: str) -> int:
        stored = sum(
            self._subaccount_load(account_id, item["advertiser_id"])
            for item in self.store.list_subaccounts(account_id)
        )
        client_only = sum(
            client.load
            for (owner, advertiser_id), client in self._protocol_clients.items()
            if owner == account_id and advertiser_id is None
        )
        return stored + client_only

    def _pool_load(self, statuses: list[dict[str, Any]]) -> int:
        total = 0
        for status in statuses:
            if status.get("session_available"):
                total += sum(
                    self._subaccount_load(status["id"], item["advertiser_id"])
                    for item in status.get("subaccounts", [])
                )
                continue
            worker = self._workers.get(status["id"])
            total += worker.load if worker is not None else int(status.get("queued") or 0)
        return total

    @staticmethod
    def _today_start() -> int:
        now = datetime.now().astimezone()
        return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def _is_daily_quota_error(self, exc: TikTokUpstreamError) -> bool:
        known_codes = {"10040104"}
        configured_codes = {
            item.strip()
            for item in self.settings.sd2api_pool_daily_quota_codes.split(",")
            if item.strip()
        }
        if exc.code in known_codes or exc.code in configured_codes:
            return True
        text = f"{exc.code} {exc}".lower()
        markers = (
            "daily limit",
            "daily quota",
            "daily generation",
            "generation day limit",
            "generation limit reached",
            "quota exceeded",
            "maximum number of generations",
            "今日",
            "当天",
            "每日",
            "日上限",
            "次数已达",
            "达到上限",
            "已达上限",
            "用量上限",
            "生成额度",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_retryable_submission_error(exc: TikTokUpstreamError) -> bool:
        if exc.code != "50000":
            return False
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "remote or network error",
                "request timeout",
                "connect_timeout",
                "thrift_egress",
            )
        )

    @staticmethod
    def _rate_limit_cooldown(exc: TikTokUpstreamError, settings: Settings) -> int:
        text = f"{exc.code} {exc}".lower()
        if not (
            "10043101" in text
            or "10001113" in text
            or "endpointaccountrpmratelimitexceeded" in text
            or "requests per minute" in text
            or "too many requests" in text
            or ("rpm" in text and ("limit" in text or "exceeded" in text))
            or "user generation 5min limit" in text
        ):
            return 0
        if (
            "10001113" in text
            or "5min limit" in text
            or "user generation 5min limit" in text
        ):
            return settings.sd2api_pool_generation_limit_cooldown
        return settings.sd2api_pool_rate_limit_cooldown

    @staticmethod
    def _is_concurrency_full_error(exc: TikTokUpstreamError) -> bool:
        text = f"{exc.code} {exc}".lower()
        slot_markers = (
            "concurrent",
            "concurrency",
            "parallel",
            "simultaneous",
            "in progress",
            "processing",
            "generating",
            "running",
            "queue",
            "queued",
            "pending",
            "并发",
            "生成中",
            "处理中",
            "进行中",
            "排队",
        )
        full_markers = (
            "5 videos",
            "five videos",
            "5 tasks",
            "five tasks",
            "5条",
            "5 个",
            "五条",
            "full",
            "满",
        )
        return any(marker in text for marker in slot_markers) and any(
            marker in text for marker in full_markers
        )

    async def _mark_account_session_expired(
        self, account_id: str, exc: TikTokUpstreamError
    ) -> None:
        """Remove an invalid login session before trying another account."""
        await self._close_protocol_clients(account_id)
        message = "TikTok session expired; the account must log in again"
        self.store.update_account(
            account_id,
            session_ciphertext=None,
            session_updated_at=None,
            login_state="pending",
            last_error=message,
        )
        self.store.add_event(
            level="warning",
            category="account",
            message=message,
            account_id=account_id,
            details={
                "upstream_code": exc.code,
                "upstream_message": str(exc),
            },
        )
        account = self.store.get_account(account_id)
        if (
            account
            and account["enabled"]
            and self.settings.sd2api_auto_login
            and account.get("auto_login")
            and account.get("credentials_configured")
        ):
            self._schedule_login(account_id)

    def _decorate_subaccounts(
        self, account_id: str, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        now = int(time.time())
        result: list[dict[str, Any]] = []
        for item in items:
            decorated = dict(item)
            advertiser_id = str(item["advertiser_id"])
            active_tasks = self._subaccount_load(account_id, advertiser_id)
            blocked_until = int(item.get("quota_blocked_until") or 0)
            quota_blocked = blocked_until > now
            rate_limited_until = int(item.get("rate_limited_until") or 0)
            decorated.update(
                active_tasks=active_tasks,
                quota_blocked=quota_blocked,
                rate_limited=rate_limited_until > now,
                tasks_today=self.store.task_count_since(
                    account_id, advertiser_id, self._today_start()
                ),
            )
            result.append(decorated)
        return result

    async def add_account(
        self,
        *,
        account_id: str | None,
        name: str | None,
        start: bool,
        username: str,
        password: str,
        auto_login: bool = True,
    ) -> dict[str, Any]:
        normalized_username = username.strip().lower()
        if any(
            str(item.get("username") or "").strip().lower() == normalized_username
            for item in self.store.list_accounts()
        ):
            raise TikTokUpstreamError(
                f"Login email {username!r} is already present in the account pool",
                status_code=409,
                code="account_username_exists",
            )
        if account_id is None:
            while True:
                account_id = "account_" + uuid.uuid4().hex[:12]
                if self.store.get_account(account_id) is None:
                    break
        password_ciphertext = self._vault().encrypt(password)
        try:
            self.store.create_account(
                account_id=account_id,
                name=name or username,
                username=username,
                password_ciphertext=password_ciphertext,
                email_address=username,
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
            # TikTok Ads sends the OTP to the same mailbox used to sign in.
            changes["email_address"] = username
            changes["session_ciphertext"] = None
            changes["session_updated_at"] = None
        if password is not None:
            changes["password_ciphertext"] = self._vault().encrypt(password)
            changes["login_state"] = "pending"
            changes["last_error"] = None
            changes["session_ciphertext"] = None
            changes["session_updated_at"] = None
        if auto_login is not None:
            changes["auto_login"] = auto_login
        if changes:
            if "session_ciphertext" in changes:
                await self._close_protocol_clients(account_id)
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
        self._ensure_background_tasks()
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
        self._started_accounts.add(account_id)
        if account.get("session_available"):
            try:
                session = self._protocol_session(account_id)
                if session is not None and (
                    not session.device_id
                    or not session.fp_id
                    or not session.sec_ch_ua
                ):
                    # One-time migration for sessions captured by older builds.
                    # Reuse the persistent authenticated profile; no password,
                    # CAPTCHA, or email code should be required.
                    worker = self._worker(account_id)
                    await worker.start()
                    browser_status = await worker.status()
                    for _ in range(30):
                        if browser_status["logged_in"]:
                            break
                        await asyncio.sleep(0.5)
                        browser_status = await worker.status()
                    if not browser_status["logged_in"]:
                        raise TikTokUpstreamError(
                            "The saved browser profile must sign in once to capture its web identity",
                            status_code=401,
                            code="protocol_identity_missing",
                        )
                    session = await self._capture_protocol_session(account_id, worker)
                    if not session.fp_id:
                        raise TikTokUpstreamError(
                            "TikTok did not expose a web fingerprint ID during session capture",
                            status_code=409,
                            code="protocol_fp_id_missing",
                        )
                    await worker.stop()
                    self._workers.pop(account_id, None)
                await self._protocol_client(account_id).validate()
                self.store.update_account(
                    account_id,
                    login_state="logged_in",
                    last_error=None,
                )
                await self.refresh_subaccounts(account_id, check_access=True)
                return await self.account_status(account_id)
            except TikTokUpstreamError as exc:
                if not is_tiktok_authentication_error(exc):
                    self.store.update_account(
                        account_id,
                        last_error=f"Protocol validation failed: {exc}",
                    )
                    return await self.account_status(account_id)
                await self._close_protocol_clients(account_id)
                self.store.update_account(
                    account_id,
                    session_ciphertext=None,
                    session_updated_at=None,
                    login_state="pending",
                    last_error="Stored TikTok session expired; re-login required",
                )
        try:
            await self._worker(account_id).start()
        except Exception as exc:
            message = f"Could not start Chromium: {exc.__class__.__name__}: {exc}"
            self.store.update_account(
                account_id, login_state="browser_error", last_error=message
            )
            raise TikTokUpstreamError(
                message, status_code=503, code="browser_start_failed"
            ) from exc
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
                worker = self._worker(account_id)
                await self._capture_protocol_session(account_id, worker, validate=True)
                await worker.stop()
                self._workers.pop(account_id, None)
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
        protocol_load = self._account_load(account_id)
        if protocol_load > 0 and not force:
            if worker:
                self._workers[account_id] = worker
            raise TikTokUpstreamError(
                f"Account {account_id!r} still has active protocol tasks",
                status_code=409,
                code="account_busy",
            )
        if worker:
            if worker.load > 0 and not force:
                self._workers[account_id] = worker
                raise TikTokUpstreamError(
                    f"Account {account_id!r} still has running or queued tasks",
                    status_code=409,
                    code="account_busy",
                )
            await worker.stop()
        await self._close_protocol_clients(account_id)
        self._started_accounts.discard(account_id)

    async def focus_account(self, account_id: str) -> dict[str, Any]:
        worker = self._worker(account_id)
        try:
            await worker.focus()
        except Exception as exc:
            raise TikTokUpstreamError(
                f"Could not open Chromium: {exc.__class__.__name__}: {exc}",
                status_code=503,
                code="browser_focus_failed",
            ) from exc
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
            self._started_accounts.add(account_id)
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
                await self._capture_protocol_session(account_id, worker, validate=True)
                await worker.stop()
                self._workers.pop(account_id, None)
                await self.refresh_subaccounts(account_id, check_access=True)
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
                if account.get("session_available") and account["id"] in self._started_accounts:
                    try:
                        client = self._protocol_client(account["id"])
                        if client.load == 0:
                            await client.validate()
                        continue
                    except TikTokUpstreamError as exc:
                        if not is_tiktok_authentication_error(exc):
                            self.store.update_account(
                                account["id"],
                                last_error=f"Protocol status failed: {exc}",
                            )
                            continue
                        await self._close_protocol_clients(account["id"])
                        self.store.update_account(
                            account["id"],
                            session_ciphertext=None,
                            session_updated_at=None,
                            login_state="pending",
                            last_error="TikTok session expired; re-login required",
                        )
                        self._schedule_login(account["id"])
                        continue
                worker = self._workers.get(account["id"])
                if worker is None or worker.load > 0:
                    continue
                try:
                    status = await worker.status()
                except Exception as exc:
                    self.store.update_account(
                        account["id"],
                        login_state="browser_error",
                        last_error=f"Browser status failed: {exc.__class__.__name__}: {exc}",
                    )
                    self._schedule_login(account["id"])
                    continue
                if not status["logged_in"]:
                    self._schedule_login(account["id"])

    def _recover_interrupted_keepalives(self) -> None:
        for account in self.store.list_accounts():
            if account.get("keepalive_state") != "running":
                continue
            now = int(time.time())
            self.store.update_account(
                account["id"],
                keepalive_state="interrupted",
                keepalive_finished_at=now,
                keepalive_error="Service restarted before keepalive finished",
            )
            self.store.add_event(
                level="warning",
                category="account",
                message="Session keepalive was interrupted by a service restart",
                account_id=account["id"],
            )

    async def _session_keepalive_loop(self) -> None:
        while True:
            interval = self.settings.sd2api_session_keepalive_interval
            await asyncio.sleep(min(300, max(30, interval // 12)))
            await self._run_session_keepalive_once()

    async def _run_session_keepalive_once(self, *, now: int | None = None) -> None:
        """Renew due, idle sessions serially without triggering login."""
        current_time = int(time.time()) if now is None else now
        interval = self.settings.sd2api_session_keepalive_interval
        for account in self.store.list_accounts():
            account_id = str(account["id"])
            updated_at = account.get("session_updated_at")
            default_due_at = (
                int(updated_at) + interval if isinstance(updated_at, int) else None
            )
            due_at = (
                int(account["keepalive_next_at"])
                if isinstance(account.get("keepalive_next_at"), int)
                else default_due_at
            )
            if due_at is not None and account.get("keepalive_next_at") is None:
                self.store.update_account(account_id, keepalive_next_at=due_at)
            if not (
                account.get("enabled")
                and account_id in self._started_accounts
                and account.get("session_available")
                and due_at is not None
                and current_time >= due_at
            ):
                continue
            async with self._scheduler_lock:
                login_task = self._login_tasks.get(account_id)
                if login_task is not None and not login_task.done():
                    continue
                worker = self._workers.get(account_id)
                if worker is not None and worker.load > 0:
                    continue
                if self._account_load(account_id) > 0:
                    continue
                self._keepalive_accounts.add(account_id)
                started_at = int(time.time())
                self.store.update_account(
                    account_id,
                    keepalive_state="running",
                    keepalive_started_at=started_at,
                    keepalive_finished_at=None,
                    keepalive_error=None,
                )
            self.store.add_event(
                level="info",
                category="account",
                message="Session keepalive started; Chromium profile is opening",
                account_id=account_id,
                details={"url": "https://ads.tiktok.com/creative/creativestudio/image-to-video"},
            )
            worker = self._worker(account_id)
            try:
                existing_session = self._protocol_session(account_id)
                result = await worker.renew_protocol_session()
                await self._capture_protocol_session(
                    account_id,
                    worker,
                    bootstrap_identity=False,
                    identity_fallback=existing_session,
                    validate=True,
                )
                finished_at = int(time.time())
                self.store.update_account(
                    account_id,
                    keepalive_state="succeeded",
                    keepalive_finished_at=finished_at,
                    keepalive_next_at=finished_at + interval,
                    keepalive_error=None,
                )
                self.store.add_event(
                    level="success",
                    category="account",
                    message="Session keepalive completed and protocol validation passed",
                    account_id=account_id,
                    details={
                        "url": result.get("url"),
                        "account_info_status": result.get("account_info_status"),
                        "core_cookie_count": result.get("core_cookie_count"),
                        "rotated_cookie_names": result.get("rotated_cookie_names", []),
                    },
                )
            except asyncio.CancelledError:
                finished_at = int(time.time())
                self.store.update_account(
                    account_id,
                    keepalive_state="interrupted",
                    keepalive_finished_at=finished_at,
                    keepalive_error="Service stopped while keepalive was running",
                )
                self.store.add_event(
                    level="warning",
                    category="account",
                    message="Session keepalive interrupted; Chromium is being closed",
                    account_id=account_id,
                )
                raise
            except Exception as exc:
                finished_at = int(time.time())
                message = f"{exc.__class__.__name__}: {exc}"
                self.store.update_account(
                    account_id,
                    keepalive_state="failed",
                    keepalive_finished_at=finished_at,
                    keepalive_next_at=finished_at + min(1800, interval),
                    keepalive_error=message,
                    last_error=f"Session keepalive failed: {message}",
                )
                self.store.add_event(
                    level="error",
                    category="account",
                    message="Session keepalive failed; Chromium is being closed",
                    account_id=account_id,
                    details={"error": message},
                )
            finally:
                try:
                    await worker.stop()
                finally:
                    self._workers.pop(account_id, None)
                    self._keepalive_accounts.discard(account_id)
                    await self._notify_keepalive_finished()

    async def _notify_keepalive_finished(self) -> None:
        async with self._keepalive_condition:
            self._keepalive_revision += 1
            self._keepalive_condition.notify_all()

    async def _wait_for_keepalive(self, revision: int) -> None:
        self._keepalive_waiters += 1
        try:
            async with self._keepalive_condition:
                await self._keepalive_condition.wait_for(
                    lambda: self._keepalive_revision != revision
                )
        finally:
            self._keepalive_waiters -= 1

    async def diagnostics(
        self,
        *,
        account_id: str | None = None,
        open_generation_menu: bool = False,
        open_subaccount_menu: bool = False,
        click_subaccount_id: str | None = None,
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
            open_generation_menu=open_generation_menu,
            open_subaccount_menu=open_subaccount_menu,
            click_subaccount_id=click_subaccount_id,
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
        try:
            root_client = self._protocol_client(account_id)
            discovered = await root_client.discover_subaccounts()
            if check_access:
                async def inspect(item: dict[str, Any]) -> dict[str, Any]:
                    checked = dict(item)
                    try:
                        advertiser_id = str(item["advertiser_id"])
                        capability = await self._protocol_client(
                            account_id, advertiser_id
                        ).account_capabilities()
                        if capability["advertiser_id"] != advertiser_id:
                            raise RuntimeError(
                                "TikTok returned a different subaccount context"
                            )
                        checked.update(
                            credits=capability["credits"],
                            seedance_access=capability["seedance_access"],
                            last_error=None,
                            last_checked_at=int(time.time()),
                        )
                    except TikTokUpstreamError as exc:
                        if is_tiktok_authentication_error(exc):
                            raise
                        checked.update(
                            credits=None,
                            seedance_access=False,
                            last_error=f"{exc.__class__.__name__}: {exc}",
                            last_checked_at=int(time.time()),
                        )
                    except Exception as exc:
                        checked.update(
                            credits=None,
                            seedance_access=False,
                            last_error=f"{exc.__class__.__name__}: {exc}",
                            last_checked_at=int(time.time()),
                        )
                    return checked

                discovered = list(await asyncio.gather(*(inspect(item) for item in discovered)))
        except TikTokUpstreamError as exc:
            if is_tiktok_authentication_error(exc):
                await self._mark_account_session_expired(account_id, exc)
                raise tiktok_authentication_error() from exc
            raise
        except Exception as exc:
            raise TikTokUpstreamError(
                f"Subaccount scan failed: {exc.__class__.__name__}: {exc}",
                status_code=502,
                code="subaccount_scan_failed",
            ) from exc
        self.store.upsert_subaccounts(account_id, discovered)
        self.store.update_account(account_id, last_error=None)
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
                status_code=403,
                code="seedance_access_required",
            )
        if enabled and int(target.get("quota_blocked_until") or 0) > int(time.time()):
            raise TikTokUpstreamError(
                f"Subaccount {advertiser_id!r} reached its daily generation limit",
                status_code=429,
                code="subaccount_daily_quota_exhausted",
            )
        if enabled and int(target.get("rate_limited_until") or 0) > int(time.time()):
            raise TikTokUpstreamError(
                f"Subaccount {advertiser_id!r} is temporarily rate limited",
                status_code=429,
                code="subaccount_rate_limited",
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
        try:
            if worker:
                runtime = await worker.status()
                runtime.setdefault("browser_running", True)
                runtime.setdefault("backend", "browser")
            elif account_id in self._started_accounts and account.get("session_available"):
                protocol_load = self._account_load(account_id)
                runtime = {
                    "running": True,
                    "browser_running": False,
                    "backend": "protocol",
                    "logged_in": True,
                    "queued": protocol_load,
                    "busy": protocol_load > 0,
                    "url": None,
                    "credits": None,
                    "login_state": "logged_in",
                    "login_error": account.get("last_error"),
                    "last_login_at": account.get("last_login_at"),
                    "active_advertiser_id": None,
                }
            else:
                runtime = self._stopped_runtime(account)
        except Exception as exc:
            runtime = self._stopped_runtime(
                account,
                state="browser_error",
                error=f"{exc.__class__.__name__}: {exc}",
            )
        return {
            **account,
            **runtime,
            "keepalive_active": account_id in self._keepalive_accounts,
            "subaccounts": self._decorate_subaccounts(
                account_id, self.store.list_subaccounts(account_id)
            ),
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
        while True:
            revision = self._keepalive_revision
            try:
                return await self._schedule_once(
                    prompt=prompt,
                    model=model,
                    duration=duration,
                    mode=mode,
                    media=media,
                )
            except _KeepalivePending:
                await self._wait_for_keepalive(revision)

    async def _schedule_once(
        self,
        *,
        prompt: str,
        model: str,
        duration: int,
        mode: str,
        media: list[StagedMedia],
    ) -> str:
        async with self._scheduler_lock:
            cleanup_protocol_media = False
            statuses = await self.list_accounts()
            eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
            keepalive_eligible = False
            for status in statuses:
                if (
                    status["enabled"]
                    and status["running"]
                    and status["logged_in"]
                    and status.get("keepalive_active")
                    and any(
                        self._subaccount_eligible(subaccount)
                        for subaccount in status["subaccounts"]
                    )
                ):
                    keepalive_eligible = True
                if not (
                    status["enabled"]
                    and status["running"]
                    and status["logged_in"]
                    and not status.get("keepalive_active")
                ):
                    continue
                eligible.extend(
                    (status, subaccount)
                    for subaccount in status["subaccounts"]
                    if self._subaccount_eligible(subaccount)
                )
            if not eligible:
                if keepalive_eligible:
                    raise _KeepalivePending
                online_subaccounts = [
                    subaccount
                    for status in statuses
                    if status["enabled"] and status["running"] and status["logged_in"]
                    for subaccount in status["subaccounts"]
                ]
                selected_subaccounts = [
                    subaccount
                    for subaccount in online_subaccounts
                    if subaccount.get("enabled")
                    and subaccount.get("seedance_access") is True
                    and (
                        subaccount.get("credits") is None
                        or int(subaccount["credits"]) > 0
                    )
                ]
                if selected_subaccounts and all(
                    int(subaccount.get("quota_blocked_until") or 0)
                    > int(time.time())
                    for subaccount in selected_subaccounts
                ):
                    raise TikTokUpstreamError(
                        "All selected subaccounts have exhausted their daily generation quota",
                        status_code=429,
                        code="subaccount_daily_quota_exhausted",
                    )
                if selected_subaccounts and all(
                    max(
                        int(subaccount.get("quota_blocked_until") or 0),
                        int(subaccount.get("rate_limited_until") or 0),
                    ) > int(time.time())
                    for subaccount in selected_subaccounts
                ):
                    raise TikTokUpstreamError(
                        "All selected subaccounts are temporarily rate limited",
                        status_code=429,
                        code="subaccount_rate_limited",
                    )
                if online_subaccounts and not any(
                    subaccount.get("seedance_access") is True
                    for subaccount in online_subaccounts
                ):
                    raise TikTokUpstreamError(
                        "No online subaccount has verified Seedance 2 access",
                        status_code=403,
                        code="seedance_access_required",
                    )
                raise TikTokUpstreamError(
                    "No selected subaccounts with verified Seedance 2 access and credits are available",
                    status_code=503,
                    code="account_pool_unavailable",
                )

            def pair_load(pair: tuple[dict[str, Any], dict[str, Any]]) -> int:
                status, subaccount = pair
                if status.get("session_available"):
                    stored = len(
                        self.store.active_task_ids(
                            status["id"], subaccount["advertiser_id"]
                        )
                    )
                    return max(
                        stored,
                        self._protocol_client(
                            status["id"], subaccount["advertiser_id"]
                        ).load,
                    )
                return self._worker(status["id"]).load

            total_pending = self._pool_load(statuses)
            if total_pending >= self.settings.sd2api_pool_max_pending:
                raise TikTokUpstreamError(
                    "The browser account pool queue is full",
                    status_code=429,
                    code="pool_queue_full",
                )

            candidates = list(eligible)

            try:
                last_authentication_error: TikTokUpstreamError | None = None
                quota_failures = 0
                transient_limit_failures = 0
                submission_retries = 0
                preserve_media = False
                while candidates:
                    selected_account, selected_subaccount = min(
                        candidates,
                        key=lambda pair: (
                            pair_load(pair),
                            int(pair[1].get("tasks_today") or 0),
                            -int(pair[1]["credits"])
                            if isinstance(pair[1].get("credits"), int)
                            else 1,
                            self._last_selected.get(
                                f'{pair[0]["id"]}:{pair[1]["advertiser_id"]}',
                                -1,
                            ),
                            pair[0]["id"],
                            pair[1]["advertiser_id"],
                        ),
                    )
                    candidates.remove((selected_account, selected_subaccount))
                    account_id = selected_account["id"]
                    advertiser_id = selected_subaccount["advertiser_id"]
                    protocol_mode = bool(selected_account.get("session_available"))
                    cleanup_protocol_media = protocol_mode
                    target: Any = (
                        self._protocol_client(account_id, advertiser_id)
                        if protocol_mode
                        else self._worker(account_id)
                    )
                    try:
                        if mode == "image":
                            kwargs: dict[str, Any] = {
                                "prompt": prompt,
                                "model": model,
                                "duration": duration,
                                "image_path": media[0].path,
                            }
                            if not protocol_mode:
                                kwargs["advertiser_id"] = advertiser_id
                            task_id = await target.create_image_video(**kwargs)
                        elif mode == "reference":
                            kwargs = {
                                "prompt": prompt,
                                "model": model,
                                "duration": duration,
                                "media": media,
                            }
                            if not protocol_mode:
                                kwargs["advertiser_id"] = advertiser_id
                            task_id = await target.create_reference_video(**kwargs)
                        else:
                            kwargs = {
                                "prompt": prompt,
                                "model": model,
                                "duration": duration,
                            }
                            if not protocol_mode:
                                kwargs["advertiser_id"] = advertiser_id
                            task_id = await target.create_text_video(**kwargs)
                    except TikTokUpstreamError as exc:
                        if (
                            self._is_retryable_submission_error(exc)
                            and submission_retries < 2
                        ):
                            submission_retries += 1
                            candidates.append((selected_account, selected_subaccount))
                            self.store.add_event(
                                level="warning",
                                category="video",
                                message=(
                                    "Retrying task submission after transient upstream error"
                                ),
                                account_id=account_id,
                                details={
                                    "advertiser_id": advertiser_id,
                                    "retry": submission_retries,
                                    "max_retries": 2,
                                    "upstream_code": exc.code,
                                    "upstream_message": str(exc),
                                },
                            )
                            await asyncio.sleep(0.5 * submission_retries)
                            continue
                        if is_tiktok_authentication_error(exc):
                            last_authentication_error = exc
                            await self._mark_account_session_expired(account_id, exc)
                            candidates = [
                                pair for pair in candidates if pair[0]["id"] != account_id
                            ]
                            continue
                        if self._is_daily_quota_error(exc):
                            quota_failures += 1
                            blocked_until = (
                                int(time.time())
                                + self.settings.sd2api_pool_quota_cooldown
                            )
                            self.store.update_subaccount(
                                account_id,
                                advertiser_id,
                                quota_blocked_until=blocked_until,
                                quota_reason=str(exc),
                                quota_updated_at=int(time.time()),
                                last_error=str(exc),
                            )
                            event_message = "Subaccount daily generation quota exhausted"
                        else:
                            cooldown = self._rate_limit_cooldown(exc, self.settings)
                            if cooldown:
                                transient_limit_failures += 1
                                blocked_until = int(time.time()) + cooldown
                                self.store.update_subaccount(
                                    account_id,
                                    advertiser_id,
                                    rate_limited_until=blocked_until,
                                    rate_limit_reason=str(exc),
                                    rate_limit_updated_at=int(time.time()),
                                    last_error=str(exc),
                                )
                                event_message = "Subaccount temporarily rate limited"
                            elif self._is_concurrency_full_error(exc):
                                transient_limit_failures += 1
                                blocked_until = None
                                event_message = "Subaccount upstream concurrency is full"
                            else:
                                raise
                        self.store.add_event(
                            level="warning",
                            category="account",
                            message=event_message,
                            account_id=account_id,
                            details={
                                "advertiser_id": advertiser_id,
                                "blocked_until": blocked_until,
                                "upstream_code": exc.code,
                                "upstream_message": str(exc),
                            },
                        )
                        continue
                    self._selection_counter += 1
                    self._last_selected[
                        f"{account_id}:{advertiser_id}"
                    ] = self._selection_counter
                    self._task_accounts[task_id] = account_id
                    self._task_advertisers[task_id] = advertiser_id
                    if selected_subaccount.get("quota_reason"):
                        self.store.update_subaccount(
                            account_id,
                            advertiser_id,
                            quota_blocked_until=None,
                            quota_reason=None,
                            quota_updated_at=None,
                            last_error=None,
                        )
                    if selected_subaccount.get("rate_limit_reason"):
                        self.store.update_subaccount(
                            account_id,
                            advertiser_id,
                            rate_limited_until=None,
                            rate_limit_reason=None,
                            rate_limit_updated_at=None,
                            last_error=None,
                        )
                    return task_id
                if keepalive_eligible:
                    preserve_media = True
                    raise _KeepalivePending
                if (
                    last_authentication_error is not None
                    and quota_failures == 0
                    and transient_limit_failures == 0
                ):
                    raise last_authentication_error
                if last_authentication_error is not None:
                    raise TikTokUpstreamError(
                        "All selected accounts are unavailable because their sessions expired "
                        "or their daily generation quota was exhausted",
                        status_code=503,
                        code="account_pool_unavailable",
                    )
                if transient_limit_failures:
                    raise TikTokUpstreamError(
                        "All selected subaccounts are temporarily limited by TikTok",
                        status_code=429,
                        code="subaccount_rate_limited",
                    )
                raise TikTokUpstreamError(
                    "All selected subaccounts have exhausted their daily generation quota",
                    status_code=429,
                    code="subaccount_daily_quota_exhausted",
                )
            finally:
                if cleanup_protocol_media and not preserve_media:
                    upload_root = Path(self.settings.sd2api_upload_dir).resolve()
                    for item in media:
                        path = Path(item.path).resolve()
                        if path.is_relative_to(upload_root):
                            path.unlink(missing_ok=True)

    def account_for_task(self, task_id: str) -> str | None:
        return self._task_accounts.get(task_id)

    def advertiser_for_task(self, task_id: str) -> str | None:
        return self._task_advertisers.get(task_id)

    async def check_task(self, task_id: str) -> UpstreamTask:
        record = self.store.get(task_id)
        account_id = self._task_accounts.get(task_id) or (
            record.account_id if record else None
        )
        if account_id is None:
            raise TikTokUpstreamError(
                f"Pool task {task_id!r} is not active in this process",
                status_code=404,
                code="task_not_active",
            )
        advertiser_id = self._task_advertisers.get(task_id) or (
            record.advertiser_id if record else None
        )
        account = self.store.get_account(account_id)
        protocol_client: ProtocolTikTokClient | None = None
        if account and account.get("session_available"):
            protocol_client = self._protocol_client(account_id, advertiser_id)
            task = await protocol_client.check_task(task_id)
        else:
            task = await self._worker(account_id).check_task(task_id)
        credits = task.raw.get("credits") if task.raw else None
        if (
            advertiser_id
            and protocol_client is not None
            and task.status in {"succeeded", "failed"}
            and not isinstance(credits, int)
        ):
            try:
                credits = (await protocol_client.account_capabilities())["credits"]
            except Exception:
                # Credit refresh is informational and must not turn a valid
                # task result into an API failure.
                credits = None
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
        record = next(
            (
                item
                for item in self.store.list(limit=100, account_id=account_id)
                if item.video_url == video_url
            ),
            None,
        )
        account = self.store.get_account(account_id)
        if account and account.get("session_available"):
            return await self._protocol_client(
                account_id, record.advertiser_id if record else None
            ).fetch_video(video_url)
        return await self._worker(account_id).fetch_video(video_url)

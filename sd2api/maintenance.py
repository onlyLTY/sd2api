from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .store import ExpiredTask, TaskRecord, TaskStore
from .uploads import StagedMedia, UploadManager


class TaskMaintenance:
    def __init__(
        self,
        *,
        store: TaskStore,
        uploads: UploadManager,
        refresh: Callable[[TaskRecord], Awaitable[TaskRecord]],
        audit: Callable[..., None],
        expiry_seconds: int,
        reconcile_interval: int,
        reconcile_concurrency: int,
    ) -> None:
        self.store = store
        self.uploads = uploads
        self.refresh = refresh
        self.audit = audit
        self.expiry_seconds = expiry_seconds
        self.reconcile_interval = reconcile_interval
        self.reconcile_concurrency = reconcile_concurrency
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.expire_tasks()
        self._task = asyncio.create_task(
            self._run(), name="sd2api-task-maintenance"
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def expire_tasks(self, *, now: int | None = None) -> list[ExpiredTask]:
        timestamp = int(time.time()) if now is None else now
        expired = self.store.expire_active_tasks(
            created_before=timestamp - self.expiry_seconds,
            error_code="task_expired",
            error_message=(
                f"Task did not reach a terminal state within {self.expiry_seconds} seconds"
            ),
        )
        for record in expired:
            try:
                self._cleanup_payload(record.submission_payload)
            except Exception as exc:
                self.audit(
                    "warning",
                    "video",
                    "过期任务暂存素材清理失败",
                    account_id=record.account_id,
                    task_id=record.id,
                    details={"error": f"{exc.__class__.__name__}: {exc}"},
                )
        if expired:
            self.audit(
                "warning",
                "video",
                "已终结超过保留期限的非终态任务",
                details={
                    "expired_tasks": len(expired),
                    "expiry_seconds": self.expiry_seconds,
                },
            )
        return expired

    async def run_once(self, *, now: int | None = None) -> int:
        timestamp = int(time.time()) if now is None else now
        self.expire_tasks(now=timestamp)
        records = self.store.reconcilable_tasks(
            created_after=timestamp - self.expiry_seconds,
            updated_before=timestamp - self.reconcile_interval,
            limit=max(100, self.reconcile_concurrency * 25),
        )
        semaphore = asyncio.Semaphore(self.reconcile_concurrency)

        async def refresh_one(record: TaskRecord) -> None:
            async with semaphore:
                try:
                    await self.refresh(record)
                except Exception as exc:
                    self.audit(
                        "warning",
                        "video",
                        "后台任务状态刷新失败",
                        account_id=record.account_id,
                        task_id=record.id,
                        details={"error": f"{exc.__class__.__name__}: {exc}"},
                    )

        await asyncio.gather(*(refresh_one(record) for record in records))
        return len(records)

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.audit(
                    "error",
                    "system",
                    "任务维护循环失败",
                    details={"error": f"{exc.__class__.__name__}: {exc}"},
                )
            await asyncio.sleep(self.reconcile_interval)

    def _cleanup_payload(self, payload: dict[str, Any] | None) -> None:
        media = [
            item
            for item in (payload or {}).get("media", [])
            if isinstance(item, dict)
            and item.get("source") == "path"
            and item.get("kind") in {"image", "video", "audio"}
            and isinstance(item.get("value"), str)
        ]
        self.uploads.cleanup(
            [
                StagedMedia(kind=item["kind"], path=item["value"])
                for item in media
            ]
        )

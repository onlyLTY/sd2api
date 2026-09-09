from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .browser_pool import BrowserPoolClient
from .store import TaskRecord, TaskStore
from .tiktok import TikTokUpstreamError
from .uploads import StagedMedia, UploadManager


class VideoSubmissionDispatcher:
    def __init__(
        self,
        *,
        store: TaskStore,
        uploads: UploadManager,
        client: Any,
        concurrency: int,
        audit: Callable[..., None],
    ) -> None:
        self.store = store
        self.uploads = uploads
        self.client = client
        self.concurrency = concurrency
        self.audit = audit
        self._wake = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []
        self._active: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        for record in self.store.interrupted_submissions():
            self._cleanup_payload(record.submission_payload)
            self.store.fail_submission(
                record.id,
                error_code="submission_interrupted",
                error_message=(
                    "The service restarted while submitting this task; the result is "
                    "unknown, so it was not retried to avoid duplicate generation"
                ),
            )
        self._workers = [
            asyncio.create_task(self._run_worker(), name=f"video-submission-{index + 1}")
            for index in range(self.concurrency)
        ]
        self.wake()

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    def wake(self) -> None:
        self._wake.set()

    async def cancel(self, task_id: str) -> None:
        active = self._active.get(task_id)
        if active is not None:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        record = self.store.get(task_id)
        if record is not None:
            self._cleanup_payload(record.submission_payload)

    async def _run_worker(self) -> None:
        while True:
            task_id = next(iter(self.store.queued_submission_ids()), None)
            if task_id is None:
                self._wake.clear()
                if self.store.queued_submission_ids():
                    continue
                await self._wake.wait()
                continue
            submission = asyncio.create_task(
                self._submit(task_id), name=f"submit-{task_id}"
            )
            self._active[task_id] = submission
            try:
                await submission
            except asyncio.CancelledError:
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    raise
            finally:
                self._active.pop(task_id, None)

    async def _submit(self, task_id: str) -> None:
        record = self.store.claim_submission(task_id)
        if record is None:
            return
        payload = record.submission_payload or {}
        media: list[StagedMedia] = []
        try:
            for item in payload.get("media", []):
                kind = item["kind"]
                if item["source"] == "url":
                    self.uploads.ensure_staging_capacity(kind=kind)
                    media.append(await self.uploads.save_media_url(item["value"], kind=kind))
                else:
                    path = Path(item["value"])
                    if not path.is_file():
                        raise TikTokUpstreamError(
                            f"Queued {kind} file is no longer available",
                            status_code=422,
                            code="staged_media_missing",
                        )
                    media.append(StagedMedia(kind=kind, path=str(path)))

            mode = payload.get("mode", "text")
            if mode == "reference":
                upstream_task_id = await self.client.create_reference_video(
                    prompt=record.prompt,
                    model=record.model,
                    duration=record.seconds,
                    media=media,
                )
            elif mode == "image":
                upstream_task_id = await self.client.create_image_video(
                    prompt=record.prompt,
                    model=record.model,
                    duration=record.seconds,
                    image_path=media[0].path,
                )
            else:
                upstream_task_id = await self.client.create_text_video(
                    prompt=record.prompt,
                    model=record.model,
                    duration=record.seconds,
                )
            account_id = (
                self.client.account_for_task(upstream_task_id)
                if isinstance(self.client, BrowserPoolClient)
                else None
            )
            advertiser_id = (
                self.client.advertiser_for_task(upstream_task_id)
                if isinstance(self.client, BrowserPoolClient)
                else None
            )
            submitted = self.store.complete_submission(
                record.id,
                upstream_task_id=upstream_task_id,
                account_id=account_id,
                advertiser_id=advertiser_id,
            )
            self.audit(
                "info",
                "video",
                "视频任务已提交到上游",
                account_id=submitted.account_id,
                task_id=submitted.id,
                details={
                    "upstream_task_id": upstream_task_id,
                    "mode": mode,
                    "model": submitted.model,
                    "duration": submitted.seconds,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = (
                str(exc.code)
                if isinstance(exc, TikTokUpstreamError) and exc.code
                else "submission_failed"
            )
            try:
                failed = self.store.fail_submission(
                    record.id,
                    error_code=code,
                    error_message=str(exc),
                )
            except KeyError:
                return
            context = (
                dict(exc.context)
                if isinstance(exc, TikTokUpstreamError)
                else {}
            )
            self.audit(
                "error",
                "video",
                "视频任务提交失败",
                account_id=context.get("account_id") or failed.account_id,
                task_id=failed.id,
                details={
                    "error_code": code,
                    "error_message": str(exc),
                    **context,
                },
            )
        finally:
            self.uploads.cleanup(media)

    def _cleanup_payload(self, payload: dict[str, Any] | None) -> None:
        media = [
            StagedMedia(kind=item["kind"], path=item["value"])
            for item in (payload or {}).get("media", [])
            if item.get("source") == "path"
        ]
        self.uploads.cleanup(media)

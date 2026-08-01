from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import httpx

from .config import Settings
from .models import UpstreamTask


T2V_MODELS: dict[str, str] = {
    "seedance-2.0": "5000005",
    "seedance-2-0": "5000005",
    "dreamina-seedance-2.0": "5000005",
    "dreamina-seedance-2-0": "5000005",
    "sora-2": "5000005",
    "sora2": "5000005",
    "seedance-2.5": "5000007",
    "seedance-2-5": "5000007",
    "dreamina-seedance-2.5": "5000007",
}


class TikTokUpstreamError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502, code: str = "upstream_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _first(mapping: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = mapping.get(name)
        if value is not None and value != "":
            return value
    return None


def _deep_find(value: Any, names: set[str]) -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in names and child is not None:
                return child
        for child in value.values():
            found = _deep_find(child, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _deep_find(child, names)
            if found is not None:
                return found
    return None


class TikTokClient:
    create_path = "/creative_bff_i18n/api/cue/t2v/create_generate_task"
    check_path = "/creative_bff_i18n/api/cue/generate-task/check"
    bind_path = "/creative_bff_i18n/api/cue/lego/bind_videos"
    video_info_paths = (
        "/creative_bff_i18n/api/cue/lego/get_video_info",
        "/creative_bff_i18n/api/cue/video_info",
    )

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport

    @property
    def params(self) -> dict[str, str]:
        device_id = self.settings.tiktok_device_id
        return {
            "aid": "585599",
            "app_name": "creative_aio_client",
            "device_platform": "web",
            "did": device_id,
            "device_id": device_id,
        }

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "cookie": self.settings.tiktok_cookie,
            "origin": self.settings.tiktok_base_url.rstrip("/"),
            "referer": self.settings.tiktok_base_url.rstrip("/") + "/creative/creativestudio/image-to-video",
            "user-agent": self.settings.tiktok_user_agent,
            "x-creative-source": "cue/p2v",
            "agw-js-conv": "str",
        }
        if self.settings.csrf_token:
            headers["x-csrftoken"] = self.settings.csrf_token
        if self.settings.creative_csrf_token:
            headers["x-creative-csrf-token"] = self.settings.creative_csrf_token
        if self.settings.tiktok_fp_id:
            headers["x-fp-id"] = self.settings.tiktok_fp_id
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.settings.validate_tiktok_auth()
        async with httpx.AsyncClient(
            base_url=self.settings.tiktok_base_url,
            headers=self.headers,
            params=self.params,
            timeout=self.settings.sd2api_request_timeout,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            try:
                response = await client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise TikTokUpstreamError(f"TikTok request failed: {exc.__class__.__name__}") from exc
        if response.status_code in {401, 403}:
            raise TikTokUpstreamError(
                "TikTok session is expired or rejected; refresh TIKTOK_COOKIE and CSRF values",
                status_code=401,
                code="tiktok_authentication_error",
            )
        if response.status_code >= 400:
            raise TikTokUpstreamError(
                f"TikTok returned HTTP {response.status_code}",
                status_code=502,
                code="tiktok_http_error",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TikTokUpstreamError("TikTok returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise TikTokUpstreamError("TikTok returned an unexpected response shape")
        code = _first(payload, ("code", "status_code", "statusCode"))
        if code not in (None, 0, "0", 200, "200"):
            message = _first(payload, ("message", "msg", "status_message")) or "TikTok rejected the request"
            raise TikTokUpstreamError(str(message), code=str(code))
        return payload

    async def create_text_video(self, *, prompt: str, model: str, duration: int) -> str:
        internal_model = T2V_MODELS.get(model.lower())
        if not internal_model:
            raise TikTokUpstreamError(
                f"Unsupported model {model!r}. Supported aliases: {', '.join(sorted(T2V_MODELS))}",
                status_code=400,
                code="invalid_model",
            )
        settings = {
            "aiModel": internal_model,
            "duration": duration,
            "prompt": prompt,
            "useEnhancePrompt": False,
            "useReferencePrompt": False,
        }
        payload = await self._request(
            "POST",
            self.create_path,
            json={
                "prompt": prompt,
                "gokuModel": internal_model,
                "model": internal_model,
                "duration": duration,
                "settings": json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
            },
        )
        task_id = _deep_find(payload, {"taskId", "task_id", "TaskId"})
        if task_id is None:
            data = payload.get("data")
            if isinstance(data, (str, int)):
                task_id = data
        if task_id is None:
            raise TikTokUpstreamError("TikTok accepted the request but did not return a task ID")
        return str(task_id)

    async def check_task(self, task_id: str) -> UpstreamTask:
        payload = await self._request("POST", self.check_path, json={"taskId": task_id})
        drafts = _deep_find(payload, {"draft_infos", "draftInfos"})
        if not isinstance(drafts, list) or not drafts:
            return UpstreamTask(id=task_id, status="queued", progress=0, raw=payload)

        draft = next(
            (
                item
                for item in drafts
                if isinstance(item, dict)
                and str(_first(item, ("taskId", "task_id")) or task_id) == task_id
            ),
            drafts[0],
        )
        if not isinstance(draft, dict):
            raise TikTokUpstreamError("TikTok returned an invalid task record")

        draft_status = _first(draft, ("draftTaskStatus", "draft_task_status"))
        render_status = _first(draft, ("renderTaskStatus", "render_task_status"))
        video_id = _first(draft, ("vid", "videoId", "video_id", "watermarkVid"))
        video_url = self._extract_video_url(draft)
        poster_url = self._extract_poster_url(draft)
        failed = draft_status in (2, "2", "FAILED", "failed") or render_status in (
            2,
            "2",
            "FAILED",
            "failed",
        )
        succeeded = (
            draft_status in (0, "0", "SUCCESS", "success")
            and render_status in (0, "0", "SUCCESS", "success")
            and bool(video_id)
        )
        if failed:
            status, progress = "failed", 100
        elif succeeded:
            status, progress = "succeeded", 100
        elif draft_status is None and render_status is None:
            status, progress = "queued", 0
        else:
            status, progress = "running", 50

        error_code = _first(draft, ("generateErrorCode", "renderErrorCode", "errorCode"))
        error_message = _first(
            draft,
            ("generateErrorMessage", "renderErrorMessage", "errorMessage", "message"),
        )
        result = UpstreamTask(
            id=task_id,
            status=status,
            progress=progress,
            video_id=str(video_id) if video_id is not None else None,
            video_url=video_url,
            poster_url=poster_url,
            error_code=str(error_code) if error_code is not None else None,
            error_message=str(error_message) if error_message is not None else None,
            raw=payload,
        )
        if result.status == "succeeded" and result.video_id and not result.video_url:
            enriched = await self._get_video_info(result.video_id)
            result.video_url = self._extract_video_url(enriched)
            result.poster_url = result.poster_url or self._extract_poster_url(enriched)
        return result

    async def _get_video_info(self, video_id: str) -> dict[str, Any]:
        try:
            await self._request("POST", self.bind_path, json={"vids": [video_id]})
        except TikTokUpstreamError:
            pass
        last_error: TikTokUpstreamError | None = None
        for path in self.video_info_paths:
            for params in ({"vid": video_id}, {"vids": video_id}):
                try:
                    return await self._request("GET", path, params=params)
                except TikTokUpstreamError as exc:
                    last_error = exc
        if last_error:
            raise last_error
        return {}

    @staticmethod
    def _extract_video_url(value: Any) -> str | None:
        candidate = _deep_find(
            value,
            {
                "MainUrl",
                "MainHTTPUrl",
                "BackupUrl",
                "BackupHTTPUrl",
                "video_url",
                "videoUrl",
                "previewLink",
                "encode_url",
                "src",
            },
        )
        return str(candidate) if isinstance(candidate, str) and candidate.startswith("http") else None

    @staticmethod
    def _extract_poster_url(value: Any) -> str | None:
        candidate = _deep_find(value, {"PosterUrl", "posterUrl", "coverImage", "cover_image"})
        return str(candidate) if isinstance(candidate, str) and candidate.startswith("http") else None


from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from .config import settings
from .browser_client import BrowserTikTokClient
from .browser_pool import BrowserPoolClient
from .models import (
    AccountCreateRequest,
    AccountLoginRequest,
    AccountUpdateRequest,
    AudioURLContent,
    ImageURLContent,
    OpenAICreateVideoRequest,
    SeedanceCreateRequest,
    SubaccountRefreshRequest,
    SubaccountUpdateRequest,
    VideoURLContent,
)
from .store import TaskRecord, TaskStore
from .tiktok import TikTokClient, TikTokUpstreamError
from .uploads import StagedMedia, UploadManager


logger = logging.getLogger("sd2api")
store = TaskStore(settings.sd2api_database)
uploads = UploadManager(settings)
client: TikTokClient | BrowserTikTokClient | BrowserPoolClient
if settings.sd2api_mode.lower() == "browser_pool":
    client = BrowserPoolClient(settings, store)
elif settings.sd2api_mode.lower() == "browser":
    client = BrowserTikTokClient(settings)
else:
    client = TikTokClient(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if isinstance(client, (BrowserTikTokClient, BrowserPoolClient)) and settings.sd2api_browser_autostart:
        try:
            await client.start()
        except Exception:
            logger.exception("Could not auto-start the persistent browser")
    try:
        yield
    finally:
        if isinstance(client, (BrowserTikTokClient, BrowserPoolClient)):
            await client.stop()


app = FastAPI(
    title="sd2api",
    version="0.1.0",
    description="TikTok Symphony Seedance adapter with Seedance and OpenAI-compatible video APIs",
    lifespan=lifespan,
)


def require_api_key(request: Request) -> None:
    expected = settings.sd2api_api_key
    if not expected:
        return
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid API key")


def require_admin_key(request: Request) -> None:
    expected = settings.sd2api_admin_key or settings.sd2api_api_key
    if not expected:
        return
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid admin API key")


@app.exception_handler(TikTokUpstreamError)
async def handle_upstream_error(request: Request, exc: TikTokUpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": str(exc),
                "type": "upstream_error",
                "param": None,
                "code": exc.code,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    location = first.get("loc", [])
    parameter = ".".join(str(item) for item in location if item not in {"body", "query"}) or None
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "message": first.get("msg", "Invalid request"),
                "type": "invalid_request_error",
                "param": parameter,
                "code": "validation_error",
            }
        },
    )


def not_found(task_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Video task {task_id!r} was not found")


async def refresh(record: TaskRecord, *, force: bool = False) -> TaskRecord:
    if not force and record.status in {"succeeded", "failed"} and record.video_url:
        return record
    upstream = await client.check_task(record.id)
    return store.update(
        record.id,
        status=upstream.status,
        progress=upstream.progress,
        video_id=upstream.video_id,
        video_url=upstream.video_url,
        poster_url=upstream.poster_url,
        error_code=upstream.error_code,
        error_message=upstream.error_message,
        raw=upstream.raw,
    )


def openai_status(status: str) -> str:
    return {
        "queued": "queued",
        "running": "in_progress",
        "succeeded": "completed",
        "failed": "failed",
    }[status]


def openai_video(record: TaskRecord) -> dict[str, Any]:
    error = None
    if record.status == "failed":
        error = {
            "code": record.error_code or "generation_failed",
            "message": record.error_message or "TikTok video generation failed",
        }
    return {
        "id": record.id,
        "object": "video",
        "model": record.model,
        "status": openai_status(record.status),
        "progress": record.progress,
        "created_at": record.created_at,
        "completed_at": record.completed_at,
        "expires_at": None,
        "prompt": record.prompt,
        "seconds": str(record.seconds),
        "size": record.size,
        "error": error,
    }


def seedance_task(record: TaskRecord) -> dict[str, Any]:
    content = None
    if record.video_url:
        content = {"video_url": record.video_url, "poster_url": record.poster_url}
    return {
        "id": record.id,
        "model": record.model,
        "status": record.status,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "content": content,
        "duration": record.seconds,
        "ratio": record.ratio,
        "resolution": record.resolution,
        "error": (
            None
            if record.status != "failed"
            else {
                "code": record.error_code or "generation_failed",
                "message": record.error_message or "TikTok video generation failed",
            }
        ),
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin", include_in_schema=False)
async def admin_dashboard() -> FileResponse:
    return FileResponse(Path(__file__).with_name("static") / "admin.html")


@app.post("/browser/start", dependencies=[Depends(require_admin_key)])
async def start_browser() -> dict[str, Any]:
    if not isinstance(client, (BrowserTikTokClient, BrowserPoolClient)):
        raise HTTPException(status_code=409, detail="SD2API_MODE is not browser")
    return await client.start()


@app.get("/browser/status", dependencies=[Depends(require_admin_key)])
async def browser_status() -> dict[str, Any]:
    if not isinstance(client, (BrowserTikTokClient, BrowserPoolClient)):
        return {"running": False, "logged_in": False, "mode": settings.sd2api_mode}
    result = await client.status()
    result.setdefault("mode", "browser")
    return result


@app.post("/browser/stop", dependencies=[Depends(require_admin_key)])
async def stop_browser() -> dict[str, bool]:
    if isinstance(client, (BrowserTikTokClient, BrowserPoolClient)):
        await client.stop()
    return {"stopped": True}


@app.get("/browser/diagnostics", dependencies=[Depends(require_admin_key)])
async def browser_diagnostics(
    account_id: str | None = None,
    open_generation_menu: bool = False,
    open_subaccount_menu: bool = False,
    click_subaccount_id: str | None = None,
) -> dict[str, Any]:
    if isinstance(client, BrowserPoolClient):
        return await client.diagnostics(
            account_id=account_id,
            open_generation_menu=open_generation_menu,
            open_subaccount_menu=open_subaccount_menu,
            click_subaccount_id=click_subaccount_id,
        )
    if isinstance(client, BrowserTikTokClient):
        return await client.diagnostics(
            open_generation_menu=open_generation_menu,
            open_subaccount_menu=open_subaccount_menu,
            click_subaccount_id=click_subaccount_id,
        )
    raise HTTPException(status_code=409, detail="SD2API_MODE is not browser")


def require_pool() -> BrowserPoolClient:
    if not isinstance(client, BrowserPoolClient):
        raise HTTPException(status_code=409, detail="SD2API_MODE is not browser_pool")
    return client


@app.get("/admin/accounts", dependencies=[Depends(require_admin_key)])
async def list_pool_accounts() -> dict[str, Any]:
    pool = require_pool()
    return {"data": await pool.list_accounts()}


@app.post("/admin/accounts", dependencies=[Depends(require_admin_key)])
async def add_pool_account(body: AccountCreateRequest) -> dict[str, Any]:
    return await require_pool().add_account(
        account_id=body.id,
        name=body.name,
        start=body.start,
        username=body.username,
        password=body.password.get_secret_value(),
        auto_login=body.auto_login,
    )


@app.get("/admin/accounts/{account_id}", dependencies=[Depends(require_admin_key)])
async def get_pool_account(account_id: str) -> dict[str, Any]:
    return await require_pool().account_status(account_id)


@app.patch("/admin/accounts/{account_id}", dependencies=[Depends(require_admin_key)])
async def update_pool_account(account_id: str, body: AccountUpdateRequest) -> dict[str, Any]:
    return await require_pool().update_account(
        account_id,
        name=body.name,
        enabled=body.enabled,
        username=body.username,
        password=body.password.get_secret_value() if body.password else None,
        auto_login=body.auto_login,
    )


@app.delete("/admin/accounts/{account_id}", dependencies=[Depends(require_admin_key)])
async def delete_pool_account(account_id: str) -> dict[str, Any]:
    await require_pool().delete_account(account_id)
    return {
        "id": account_id,
        "deleted": True,
        "profile_retained": True,
    }


@app.post("/admin/accounts/{account_id}/start", dependencies=[Depends(require_admin_key)])
async def start_pool_account(account_id: str) -> dict[str, Any]:
    return await require_pool().start_account(account_id)


@app.post("/admin/accounts/{account_id}/stop", dependencies=[Depends(require_admin_key)])
async def stop_pool_account(account_id: str) -> dict[str, bool]:
    await require_pool().stop_account(account_id)
    return {"stopped": True}


@app.post("/admin/accounts/{account_id}/focus", dependencies=[Depends(require_admin_key)])
async def focus_pool_account(account_id: str) -> dict[str, Any]:
    return await require_pool().focus_account(account_id)


@app.post(
    "/admin/accounts/{account_id}/login",
    dependencies=[Depends(require_admin_key)],
)
async def login_pool_account(
    account_id: str, body: AccountLoginRequest
) -> dict[str, Any]:
    return await require_pool().login_account(account_id, wait=body.wait)


@app.post(
    "/admin/accounts/{account_id}/subaccounts/refresh",
    dependencies=[Depends(require_admin_key)],
)
async def refresh_pool_subaccounts(
    account_id: str, body: SubaccountRefreshRequest
) -> dict[str, Any]:
    return await require_pool().refresh_subaccounts(
        account_id, check_access=body.check_access
    )


@app.patch(
    "/admin/accounts/{account_id}/subaccounts/{advertiser_id}",
    dependencies=[Depends(require_admin_key)],
)
async def update_pool_subaccount(
    account_id: str,
    advertiser_id: str,
    body: SubaccountUpdateRequest,
) -> dict[str, Any]:
    return await require_pool().set_subaccount_enabled(
        account_id, advertiser_id, enabled=body.enabled
    )


@app.get("/admin/config/status", dependencies=[Depends(require_admin_key)])
async def admin_config_status() -> dict[str, Any]:
    return {
        "mode": settings.sd2api_mode,
        "auto_login": settings.sd2api_auto_login,
        "auto_accept_terms": settings.sd2api_auto_accept_terms,
        "credential_encryption": bool(settings.credential_master_key),
        "temp_mail_configured": bool(
            settings.sd2api_temp_mail_base_url and settings.sd2api_temp_mail_api_key
        ),
        "temp_mail_base_url": settings.sd2api_temp_mail_base_url,
        "login_timeout": settings.sd2api_login_timeout,
        "relogin_interval": settings.sd2api_relogin_interval,
        "protocol_transport": "curl_cffi/chrome",
        "protocol_upload_concurrency": settings.sd2api_protocol_upload_concurrency,
    }


@app.get("/admin/pool/status", dependencies=[Depends(require_admin_key)])
async def pool_status() -> dict[str, Any]:
    return await require_pool().status()


@app.get("/admin/tasks", dependencies=[Depends(require_admin_key)])
async def list_admin_tasks(
    limit: int = Query(default=100, ge=1, le=1000),
    account_id: str | None = None,
) -> dict[str, Any]:
    records = store.list(limit=limit, account_id=account_id)
    return {
        "data": [
            {
                "id": record.id,
                "account_id": record.account_id,
                "advertiser_id": record.advertiser_id,
                "status": record.status,
                "progress": record.progress,
                "model": record.model,
                "seconds": record.seconds,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "error_code": record.error_code,
                "error_message": record.error_message,
            }
            for record in records
        ]
    }


@app.post("/api/v3/contents/generations/tasks", dependencies=[Depends(require_api_key)])
async def create_seedance_video(body: SeedanceCreateRequest) -> dict[str, Any]:
    mode = body.generation_mode
    if mode == "first_last":
        raise HTTPException(
            status_code=501,
            detail="Strict first-and-last-frame mode is not implemented yet; use role=reference_image for Reference to video",
        )
    if mode != "text" and not isinstance(client, (BrowserTikTokClient, BrowserPoolClient)):
        raise HTTPException(
            status_code=501,
            detail="Image and reference video modes require SD2API_MODE=browser or browser_pool",
        )

    staged: list[StagedMedia] = []
    try:
        for kind, url, _role in body.media:
            staged.append(await uploads.save_media_url(url, kind=kind))
        if mode == "image":
            task_id = await client.create_image_video(
                prompt=body.prompt,
                model=body.model,
                duration=body.duration,
                image_path=staged[0].path,
            )
        elif mode == "reference":
            task_id = await client.create_reference_video(
                prompt=body.prompt,
                model=body.model,
                duration=body.duration,
                media=staged,
            )
        else:
            task_id = await client.create_text_video(
                prompt=body.prompt,
                model=body.model,
                duration=body.duration,
            )
    except Exception:
        uploads.cleanup(staged)
        raise
    record = store.create(
        task_id=task_id,
        api="seedance",
        model=body.model,
        prompt=body.prompt,
        seconds=body.duration,
        ratio=body.ratio,
        resolution=body.resolution,
        account_id=client.account_for_task(task_id) if isinstance(client, BrowserPoolClient) else None,
        advertiser_id=(
            client.advertiser_for_task(task_id)
            if isinstance(client, BrowserPoolClient)
            else None
        ),
    )
    return seedance_task(record)


@app.get("/api/v3/contents/generations/tasks/{task_id}", dependencies=[Depends(require_api_key)])
async def retrieve_seedance_video(task_id: str) -> dict[str, Any]:
    record = store.get(task_id)
    if record is None:
        raise not_found(task_id)
    return seedance_task(await refresh(record))


@app.get("/api/v3/contents/generations/tasks", dependencies=[Depends(require_api_key)])
async def list_seedance_videos(
    limit: int = Query(default=20, ge=1, le=100),
    after: str | None = None,
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    records = store.list(limit=limit, after=after, order=order)
    return {"items": [seedance_task(record) for record in records], "has_more": len(records) == limit}


@app.delete("/api/v3/contents/generations/tasks/{task_id}", dependencies=[Depends(require_api_key)])
async def delete_seedance_video(task_id: str) -> dict[str, Any]:
    if not store.delete(task_id):
        raise not_found(task_id)
    return {"id": task_id, "deleted": True}


async def parse_openai_create_request(
    request: Request,
) -> tuple[OpenAICreateVideoRequest, StagedMedia | None, list[StagedMedia]]:
    input_image: StagedMedia | None = None
    reference_media: list[StagedMedia] = []
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            data: dict[str, Any] = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Request body is not valid JSON") from exc
    elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        upload = form.get("input_reference")
        if isinstance(upload, UploadFile):
            input_image = await uploads.save_media_upload(upload, expected_kind="image")
        data = {
            key: value
            for key in ("prompt", "model", "seconds", "size")
            if (value := form.get(key)) is not None
        }
        if upload is not None and not isinstance(upload, UploadFile):
            try:
                import json

                data["input_reference"] = json.loads(str(upload))
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail="input_reference must be a file or JSON object",
                ) from exc
        references_value = form.get("references")
        if references_value is not None:
            if isinstance(references_value, UploadFile):
                if input_image:
                    uploads.cleanup([input_image])
                raise HTTPException(
                    status_code=422,
                    detail="Upload reference files with the reference_media field",
                )
            try:
                import json

                data["references"] = json.loads(str(references_value))
            except ValueError as exc:
                if input_image:
                    uploads.cleanup([input_image])
                raise HTTPException(
                    status_code=422,
                    detail="references must be a JSON array",
                ) from exc

        reference_fields = {
            "reference_media": None,
            "reference_image": "image",
            "reference_video": "video",
            "reference_audio": "audio",
        }
        try:
            for field, expected_kind in reference_fields.items():
                for value in form.getlist(field):
                    if not isinstance(value, UploadFile):
                        raise HTTPException(
                            status_code=422,
                            detail=f"{field} must contain uploaded files",
                        )
                    reference_media.append(
                        await uploads.save_media_upload(value, expected_kind=expected_kind)
                    )
        except Exception:
            uploads.cleanup(reference_media)
            if input_image:
                uploads.cleanup([input_image])
            raise
    else:
        raise HTTPException(
            status_code=415,
            detail="Use application/json or multipart/form-data",
        )
    try:
        return OpenAICreateVideoRequest.model_validate(data), input_image, reference_media
    except ValidationError as exc:
        uploads.cleanup(reference_media)
        if input_image:
            uploads.cleanup([input_image])
        raise RequestValidationError(exc.errors()) from exc


def reference_content_url(item: ImageURLContent | VideoURLContent | AudioURLContent) -> str:
    if isinstance(item, ImageURLContent):
        return item.image_url if isinstance(item.image_url, str) else item.image_url.url
    if isinstance(item, VideoURLContent):
        return item.video_url if isinstance(item.video_url, str) else item.video_url.url
    return item.audio_url if isinstance(item.audio_url, str) else item.audio_url.url


def reference_content_kind(
    item: ImageURLContent | VideoURLContent | AudioURLContent,
) -> Literal["image", "video", "audio"]:
    if isinstance(item, ImageURLContent):
        return "image"
    if isinstance(item, VideoURLContent):
        return "video"
    return "audio"


def validate_reference_media(media: list[StagedMedia]) -> None:
    images = sum(item.kind == "image" for item in media)
    videos = sum(item.kind == "video" for item in media)
    audios = sum(item.kind == "audio" for item in media)
    if not (images or videos):
        raise HTTPException(
            status_code=422,
            detail="Reference to video requires at least one image or video",
        )
    if images > 9 or videos > 3 or audios > 3:
        raise HTTPException(
            status_code=422,
            detail="Reference limits are 9 images, 3 videos, and 3 audio clips",
        )


@app.post("/v1/videos", dependencies=[Depends(require_api_key)])
async def create_openai_video(request: Request) -> dict[str, Any]:
    body, input_image, reference_media = await parse_openai_create_request(request)
    try:
        if body.input_reference is not None:
            if body.input_reference.file_id:
                raise HTTPException(
                    status_code=501,
                    detail="OpenAI file_id references are not supported; upload the image as multipart or use image_url",
                )
            input_image = await uploads.save_media_url(
                body.input_reference.image_url or "",
                kind="image",
            )
        for item in body.references:
            reference_media.append(
                await uploads.save_media_url(
                    reference_content_url(item),
                    kind=reference_content_kind(item),
                )
            )
        if input_image and reference_media:
            raise HTTPException(
                status_code=422,
                detail="input_reference and references/reference_media are mutually exclusive",
            )
        if (input_image or reference_media) and not isinstance(
            client, (BrowserTikTokClient, BrowserPoolClient)
        ):
            raise HTTPException(
                status_code=501,
                detail="Image and reference video modes require SD2API_MODE=browser or browser_pool",
            )
        if reference_media:
            validate_reference_media(reference_media)
    except Exception:
        uploads.cleanup(reference_media)
        if input_image:
            uploads.cleanup([input_image])
        raise

    seconds = int(body.seconds)
    try:
        if reference_media:
            task_id = await client.create_reference_video(
                prompt=body.prompt,
                model=body.model,
                duration=seconds,
                media=reference_media,
            )
        elif input_image:
            task_id = await client.create_image_video(
                prompt=body.prompt,
                model=body.model,
                duration=seconds,
                image_path=input_image.path,
            )
        else:
            task_id = await client.create_text_video(
                prompt=body.prompt,
                model=body.model,
                duration=seconds,
            )
    except Exception:
        uploads.cleanup(reference_media)
        if input_image:
            uploads.cleanup([input_image])
        raise
    record = store.create(
        task_id=task_id,
        api="openai",
        model=body.model,
        prompt=body.prompt,
        seconds=seconds,
        size=body.size,
        account_id=client.account_for_task(task_id) if isinstance(client, BrowserPoolClient) else None,
        advertiser_id=(
            client.advertiser_for_task(task_id)
            if isinstance(client, BrowserPoolClient)
            else None
        ),
    )
    return openai_video(record)


@app.get("/v1/videos/{video_id}", dependencies=[Depends(require_api_key)])
async def retrieve_openai_video(video_id: str) -> dict[str, Any]:
    record = store.get(video_id)
    if record is None:
        raise not_found(video_id)
    return openai_video(await refresh(record))


@app.get("/v1/videos", dependencies=[Depends(require_api_key)])
async def list_openai_videos(
    limit: int = Query(default=20, ge=1, le=100),
    after: str | None = None,
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    records = store.list(limit=limit, after=after, order=order)
    return {
        "object": "list",
        "data": [openai_video(record) for record in records],
        "has_more": len(records) == limit,
    }


@app.delete("/v1/videos/{video_id}", dependencies=[Depends(require_api_key)])
async def delete_openai_video(video_id: str) -> dict[str, Any]:
    if not store.delete(video_id):
        raise not_found(video_id)
    return {"id": video_id, "object": "video.deleted", "deleted": True}


@app.get("/v1/videos/{video_id}/content", dependencies=[Depends(require_api_key)])
async def download_openai_video(video_id: str) -> Response:
    record = store.get(video_id)
    if record is None:
        raise not_found(video_id)
    record = await refresh(
        record,
        force=isinstance(client, BrowserPoolClient)
        and not record.id.startswith("browser_"),
    )
    if record.status != "succeeded":
        raise HTTPException(status_code=409, detail=f"Video is {openai_status(record.status)}")
    if not record.video_url:
        raise TikTokUpstreamError("TikTok marked the task complete without a video URL")

    if isinstance(client, BrowserPoolClient):
        body, content_type = await client.fetch_video(record.video_url, record.account_id)
        return Response(
            content=body,
            media_type=content_type,
            headers={"content-disposition": f'attachment; filename="{video_id}.mp4"'},
        )

    if isinstance(client, BrowserTikTokClient):
        body, content_type = await client.fetch_video(record.video_url)
        return Response(
            content=body,
            media_type=content_type,
            headers={"content-disposition": f'attachment; filename="{video_id}.mp4"'},
        )

    download_client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    response = await download_client.send(
        download_client.build_request("GET", record.video_url), stream=True
    )
    if response.status_code >= 400:
        await response.aclose()
        await download_client.aclose()
        raise TikTokUpstreamError(f"TikTok CDN returned HTTP {response.status_code}")

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await download_client.aclose()

    headers = {"content-disposition": f'attachment; filename="{video_id}.mp4"'}
    if response.headers.get("content-length"):
        headers["content-length"] = response.headers["content-length"]
    return StreamingResponse(stream(), media_type="video/mp4", headers=headers)

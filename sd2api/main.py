from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import re
import time
from typing import Any, Literal
import xml.etree.ElementTree as ET

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from .config import CONFIG_PATH, RuntimeConfig, save_runtime_config, settings
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
admin_static_dir = Path(__file__).with_name("static")
TTOH_RSS_URL = "https://ttoh.app/rss.xml"
TTOH_HOME_URL = "https://ttoh.app/"
TTOH_DATE_PATTERN = re.compile(r"(?:^|\|)\s*(Start|End):\s*([^|]+)", re.IGNORECASE)
TTOH_REGION_PATTERN = re.compile(r"(?:^|\|)\s*Region:\s*([^|]+)", re.IGNORECASE)
client: TikTokClient | BrowserTikTokClient | BrowserPoolClient
if settings.sd2api_mode.lower() == "browser_pool":
    client = BrowserPoolClient(settings, store)
elif settings.sd2api_mode.lower() == "browser":
    client = BrowserTikTokClient(settings)
else:
    client = TikTokClient(settings)


def audit_event(
    level: str,
    category: str,
    message: str,
    *,
    account_id: str | None = None,
    task_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        store.add_event(
            level=level,
            category=category,
            message=message,
            account_id=account_id,
            task_id=task_id,
            details=details,
        )
    except Exception:
        logger.exception("Could not persist admin event")


@asynccontextmanager
async def lifespan(_: FastAPI):
    audit_event("info", "system", "sd2api 服务已启动")
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
        audit_event("info", "system", "sd2api 服务已停止")


app = FastAPI(
    title="sd2api",
    version="1.0",
    description=(
        "TikTok Symphony Seedance adapter with Seedance and OpenAI-compatible video APIs. "
        "当前 TikTok 网页协议只有 4–15 秒时长会真实生效；size、ratio、resolution "
        "等兼容字段不会改变上游输出。"
    ),
    lifespan=lifespan,
)


SEEDANCE_CREATE_DESCRIPTION = """
创建 TikTok Seedance 视频任务，支持文生视频、单首帧图生视频和多模态 Reference to video。

### 参数能力

| 参数 | 当前行为 |
|---|---|
| `duration` | **真实生效**；支持 4–15 秒的任意整数，当前页面显示预计 Credits 与秒数相同。 |
| `ratio` / `resolution` | 仅兼容记录和回显，**不会发送给 TikTok**，不能改变实际画幅或分辨率。 |
| `seed` / `camera_fixed` / `watermark` / `generate_audio` | 仅兼容接收，**不会发送给 TikTok**。 |

T2V、I2V、R2V 的成功任务当前均实测为竖屏 `720 × 1280`。单首帧可用；严格的首帧+尾帧模式尚未实现，会返回 HTTP 501。
"""


OPENAI_CREATE_DESCRIPTION = """
以 OpenAI Videos 风格创建 TikTok Seedance 视频。既接受 `application/json`，也接受上传文件用的 `multipart/form-data`。

- `seconds` 是唯一可控的视频规格参数：支持 **4–15 秒**的任意整数。
- `size` 只为 OpenAI SDK 兼容而保存和回显，**不会发送给 TikTok**；当前实测输出固定为竖屏 `720 × 1280`。
- `sora-2` 是本服务映射到 Dreamina Seedance 2.0 的兼容别名，并非 OpenAI Sora。
- JSON 的 `input_reference` 或 multipart 的同名文件用于单首帧 I2V。
- JSON 的 `references` 或 multipart 的 `reference_media` 用于 R2V；最多 9 张图片、3 个视频、3 段音频，且必须至少包含一张图片或一个视频。
"""


OPENAI_JSON_REFERENCE_ITEM_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "required": ["type", "image_url"],
            "properties": {
                "type": {"type": "string", "const": "image_url"},
                "image_url": {
                    "oneOf": [
                        {"type": "string", "format": "uri"},
                        {
                            "type": "object",
                            "required": ["url"],
                            "properties": {"url": {"type": "string", "format": "uri"}},
                        },
                    ]
                },
                "role": {
                    "type": "string",
                    "enum": ["first_frame", "last_frame", "reference_image"],
                },
            },
        },
        {
            "type": "object",
            "required": ["type", "video_url"],
            "properties": {
                "type": {"type": "string", "const": "video_url"},
                "video_url": {
                    "oneOf": [
                        {"type": "string", "format": "uri"},
                        {
                            "type": "object",
                            "required": ["url"],
                            "properties": {"url": {"type": "string", "format": "uri"}},
                        },
                    ]
                },
                "role": {"type": "string", "const": "reference_video"},
            },
        },
        {
            "type": "object",
            "required": ["type", "audio_url"],
            "properties": {
                "type": {"type": "string", "const": "audio_url"},
                "audio_url": {
                    "oneOf": [
                        {"type": "string", "format": "uri"},
                        {
                            "type": "object",
                            "required": ["url"],
                            "properties": {"url": {"type": "string", "format": "uri"}},
                        },
                    ]
                },
                "role": {"type": "string", "const": "reference_audio"},
            },
        },
    ]
}


OPENAI_JSON_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["prompt"],
    "additionalProperties": False,
    "properties": {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "maxLength": 32000,
            "description": "视频提示词。",
        },
        "model": {
            "type": "string",
            "default": "sora-2",
            "description": "OpenAI 兼容别名；sora-2 映射到 TikTok Dreamina Seedance 2.0。",
        },
        "seconds": {
            "type": "integer",
            "minimum": 4,
            "maximum": 15,
            "default": 4,
            "description": "真实生效的视频时长，支持 4–15 秒的任意整数。",
        },
        "size": {
            "type": "string",
            "enum": ["720x1280", "1280x720", "1024x1792", "1792x1024"],
            "default": "720x1280",
            "description": "仅兼容记录和回显，不会发送给 TikTok，也不会改变实际输出尺寸。",
        },
        "input_reference": {
            "type": "object",
            "description": "单首帧 I2V。仅 image_url 可用；file_id 尚未实现。",
            "properties": {
                "image_url": {"type": "string", "format": "uri"},
                "file_id": {
                    "type": "string",
                    "description": "尚未实现；提交后返回 HTTP 501。",
                },
            },
        },
        "references": {
            "type": "array",
            "description": "R2V 多模态参考素材。最多 9 图、3 视频、3 音频。",
            "items": OPENAI_JSON_REFERENCE_ITEM_SCHEMA,
        },
    },
}


OPENAI_MULTIPART_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["prompt"],
    "properties": {
        "prompt": {"type": "string", "description": "视频提示词。"},
        "model": {"type": "string", "default": "sora-2"},
        "seconds": {
            "type": "integer",
            "minimum": 4,
            "maximum": 15,
            "default": 4,
            "description": "真实生效的视频时长。",
        },
        "size": {
            "type": "string",
            "enum": ["720x1280", "1280x720", "1024x1792", "1792x1024"],
            "default": "720x1280",
            "description": "仅兼容记录和回显，不控制 TikTok 输出。",
        },
        "input_reference": {
            "type": "string",
            "format": "binary",
            "description": "单张首帧图片；不能和 reference_media 同时使用。",
        },
        "reference_media": {
            "type": "array",
            "items": {"type": "string", "format": "binary"},
            "description": "R2V 混合参考文件；最多 9 图、3 视频、3 音频。",
        },
        "reference_image": {
            "type": "array",
            "items": {"type": "string", "format": "binary"},
            "description": "可重复提交的 R2V 图片文件字段。",
        },
        "reference_video": {
            "type": "array",
            "items": {"type": "string", "format": "binary"},
            "description": "可重复提交的 R2V 视频文件字段。",
        },
        "reference_audio": {
            "type": "array",
            "items": {"type": "string", "format": "binary"},
            "description": "可重复提交的 R2V 音频文件字段。",
        },
        "references": {
            "type": "string",
            "description": "可选的 JSON 字符串形式 references 数组。",
        },
    },
}


def require_api_key(request: Request) -> None:
    expected = settings.sd2api_api_key
    if not expected:
        return
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    candidates = [expected]
    if settings.sd2api_admin_key and settings.sd2api_admin_key != expected:
        candidates.append(settings.sd2api_admin_key)
    if not supplied or not any(
        secrets.compare_digest(supplied, candidate) for candidate in candidates
    ):
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
    audit_event(
        "error",
        "system",
        "TikTok 上游请求失败",
        details={"code": exc.code, "message": str(exc), "path": request.url.path},
    )
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
    updated = store.update(
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
    if updated.status != record.status:
        if updated.status == "succeeded":
            audit_event(
                "success",
                "video",
                "视频生成完成",
                account_id=updated.account_id,
                task_id=updated.id,
            )
        elif updated.status == "failed":
            audit_event(
                "error",
                "video",
                "视频生成失败",
                account_id=updated.account_id,
                task_id=updated.id,
                details={
                    "error_code": updated.error_code,
                    "error_message": updated.error_message,
                },
            )
        elif updated.status == "running":
            audit_event(
                "info",
                "video",
                "视频开始生成",
                account_id=updated.account_id,
                task_id=updated.id,
            )
    return updated


def upstream_model_name(model: str) -> str:
    normalized = model.strip().lower().replace("_", "-")
    if normalized in {
        "sora-2",
        "sora2",
        "seedance-2.0",
        "seedance-2-0",
        "dreamina-seedance-2.0",
        "dreamina-seedance-2-0",
    }:
        return "seedance-2.0"
    if normalized in {
        "seedance-2.5",
        "seedance-2-5",
        "dreamina-seedance-2.5",
        "dreamina-seedance-2-5",
    }:
        return "seedance-2.5"
    return model


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
    return FileResponse(admin_static_dir / "admin.html")


@app.get("/admin/assets/admin.css", include_in_schema=False)
async def admin_styles() -> FileResponse:
    return FileResponse(admin_static_dir / "admin.css", media_type="text/css")


@app.get("/admin/assets/admin.js", include_in_schema=False)
async def admin_script() -> FileResponse:
    return FileResponse(
        admin_static_dir / "admin.js", media_type="application/javascript"
    )


def parse_ttoh_datetime(value: str | None) -> int | None:
    if not value:
        return None
    from datetime import datetime

    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00Z"
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_ttoh_featured_offers(xml_body: str, now: int) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_body)
    offers: list[dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        categories = {
            (category.text or "").strip().lower()
            for category in item.findall("category")
        }
        if "featured" not in categories:
            continue
        description = (item.findtext("description") or "").strip()
        dates = {key.lower(): value.strip() for key, value in TTOH_DATE_PATTERN.findall(description)}
        start_at = parse_ttoh_datetime(dates.get("start"))
        end_at = parse_ttoh_datetime(dates.get("end"))
        # A date-only end is inclusive through the end of that local campaign day.
        if end_at is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", dates.get("end", "")):
            end_at += 86_399
        if start_at is None or start_at > now or (end_at is not None and end_at < now):
            continue
        region_match = TTOH_REGION_PATTERN.search(description)
        summary = description.split("|", 1)[0].strip()
        offers.append(
            {
                "title": (item.findtext("title") or "TikTok Ads offer").strip(),
                "description": summary,
                "link": (item.findtext("link") or TTOH_HOME_URL).strip(),
                "region": region_match.group(1).strip() if region_match else "",
                "start": dates.get("start", ""),
                "end": dates.get("end", ""),
                "start_at": start_at,
            }
        )
    offers.sort(key=lambda offer: offer["start_at"] or 0, reverse=True)
    for offer in offers:
        offer.pop("start_at", None)
    return offers


@app.get("/admin/ttoh/offers", include_in_schema=False)
async def ttoh_offers() -> JSONResponse:
    import time

    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as rss_client:
            response = await rss_client.get(
                TTOH_RSS_URL,
                headers={"Accept": "application/rss+xml, application/xml;q=0.9"},
            )
            response.raise_for_status()
        offers = parse_ttoh_featured_offers(response.text, int(time.time()))
        return JSONResponse(
            {"offers": offers},
            headers={"Cache-Control": "public, max-age=900, stale-if-error=86400"},
        )
    except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
        logger.warning("Could not load ttoh RSS: %s", exc)
        return JSONResponse(
            {"offers": []},
            headers={"Cache-Control": "public, max-age=60"},
        )


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
    result = await require_pool().add_account(
        account_id=body.id,
        name=body.name,
        start=body.start,
        username=body.username,
        password=body.password.get_secret_value(),
        auto_login=body.auto_login,
    )
    audit_event(
        "info",
        "account",
        "账号已加入号池",
        account_id=result.get("id"),
        details={"start": body.start, "auto_login": body.auto_login},
    )
    return result


@app.get("/admin/accounts/{account_id}", dependencies=[Depends(require_admin_key)])
async def get_pool_account(account_id: str) -> dict[str, Any]:
    return await require_pool().account_status(account_id)


@app.patch("/admin/accounts/{account_id}", dependencies=[Depends(require_admin_key)])
async def update_pool_account(account_id: str, body: AccountUpdateRequest) -> dict[str, Any]:
    result = await require_pool().update_account(
        account_id,
        name=body.name,
        enabled=body.enabled,
        username=body.username,
        password=body.password.get_secret_value() if body.password else None,
        auto_login=body.auto_login,
    )
    audit_event("info", "account", "账号设置已更新", account_id=account_id)
    return result


@app.delete("/admin/accounts/{account_id}", dependencies=[Depends(require_admin_key)])
async def delete_pool_account(account_id: str) -> dict[str, Any]:
    await require_pool().delete_account(account_id)
    audit_event("warning", "account", "账号已从号池移除", account_id=account_id)
    return {
        "id": account_id,
        "deleted": True,
        "profile_retained": True,
    }


@app.post("/admin/accounts/{account_id}/start", dependencies=[Depends(require_admin_key)])
async def start_pool_account(account_id: str) -> dict[str, Any]:
    result = await require_pool().start_account(account_id)
    audit_event("info", "account", "账号已启动", account_id=account_id)
    return result


@app.post("/admin/accounts/{account_id}/stop", dependencies=[Depends(require_admin_key)])
async def stop_pool_account(account_id: str) -> dict[str, bool]:
    await require_pool().stop_account(account_id)
    audit_event("warning", "account", "账号已停止", account_id=account_id)
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
    result = await require_pool().login_account(account_id, wait=body.wait)
    audit_event("info", "login", "账号登录流程已启动", account_id=account_id)
    return result


@app.post(
    "/admin/accounts/{account_id}/subaccounts/refresh",
    dependencies=[Depends(require_admin_key)],
)
async def refresh_pool_subaccounts(
    account_id: str, body: SubaccountRefreshRequest
) -> dict[str, Any]:
    result = await require_pool().refresh_subaccounts(
        account_id, check_access=body.check_access
    )
    audit_event(
        "info",
        "account",
        "子账号与 Credits 已刷新",
        account_id=account_id,
    )
    return result


@app.patch(
    "/admin/accounts/{account_id}/subaccounts/{advertiser_id}",
    dependencies=[Depends(require_admin_key)],
)
async def update_pool_subaccount(
    account_id: str,
    advertiser_id: str,
    body: SubaccountUpdateRequest,
) -> dict[str, Any]:
    result = await require_pool().set_subaccount_enabled(
        account_id, advertiser_id, enabled=body.enabled
    )
    audit_event(
        "info",
        "account",
        "子账号已启用" if body.enabled else "子账号已停用",
        account_id=account_id,
        details={"advertiser_id": advertiser_id},
    )
    return result


@app.get("/admin/config/status", dependencies=[Depends(require_admin_key)])
async def admin_config_status() -> dict[str, Any]:
    return {
        "mode": settings.sd2api_mode,
        "auto_login": settings.sd2api_auto_login,
        "credential_encryption": bool(settings.credential_master_key),
        "temp_mail_configured": bool(
            settings.sd2api_temp_mail_base_url and settings.sd2api_temp_mail_api_key
        ),
        "temp_mail_base_url": settings.sd2api_temp_mail_base_url,
        "login_timeout": settings.sd2api_login_timeout,
        "relogin_interval": settings.sd2api_relogin_interval,
        "protocol_transport": "curl_cffi/chrome",
        "protocol_upload_concurrency": settings.sd2api_protocol_upload_concurrency,
        "subaccount_concurrency": settings.sd2api_pool_subaccount_concurrency,
        "quota_cooldown": settings.sd2api_pool_quota_cooldown,
        "daily_quota_codes": settings.sd2api_pool_daily_quota_codes,
        "config_file": str(CONFIG_PATH.resolve()),
        "config_source": settings.config_source,
    }


@app.get("/admin/config", dependencies=[Depends(require_admin_key)])
async def admin_runtime_config() -> dict[str, Any]:
    return {
        "config": settings.runtime.model_dump(mode="json"),
        "path": str(CONFIG_PATH.resolve()),
        "source": settings.config_source,
        "restart_required": False,
    }


@app.put("/admin/config", dependencies=[Depends(require_admin_key)])
async def update_admin_runtime_config(body: RuntimeConfig) -> dict[str, Any]:
    previous = settings.runtime
    save_runtime_config(body, CONFIG_PATH)
    settings.replace_runtime(body)
    changed = sorted(
        name
        for name in RuntimeConfig.model_fields
        if getattr(previous, name) != getattr(body, name)
    )
    restart_fields = {
        "tiktok_base_url",
        "tiktok_user_agent",
        "database",
        "request_timeout",
        "mode",
        "browser_profile",
        "browser_channel",
        "browser_headless",
        "browser_autostart",
        "pool_start_concurrency",
        "protocol_upload_concurrency",
        "upload_dir",
    }
    restart_required = bool(restart_fields.intersection(changed))
    audit_event(
        "warning" if restart_required else "info",
        "system",
        "运行配置已更新",
        details={
            "changed": changed,
            "restart_required": restart_required,
            "path": str(CONFIG_PATH.resolve()),
        },
    )
    return {
        "config": body.model_dump(mode="json"),
        "path": str(CONFIG_PATH.resolve()),
        "source": "file",
        "changed": changed,
        "restart_required": restart_required,
    }


@app.get("/admin/pool/status", dependencies=[Depends(require_admin_key)])
async def pool_status() -> dict[str, Any]:
    return await require_pool().status()


def admin_task(record: TaskRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "api": record.api,
        "account_id": record.account_id,
        "advertiser_id": record.advertiser_id,
        "status": record.status,
        "progress": record.progress,
        "model": record.model,
        "upstream_model": upstream_model_name(record.model),
        "prompt": record.prompt,
        "seconds": record.seconds,
        "size": record.size,
        "ratio": record.ratio,
        "resolution": record.resolution,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "completed_at": record.completed_at,
        "downloadable": record.status == "succeeded" and bool(record.video_url),
        "error_code": record.error_code,
        "error_message": record.error_message,
    }


def duration_level(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds <= 30 * 60:
        return "recommended"
    if seconds <= 60 * 60:
        return "normal"
    return "busy"


def hourly_duration_profile(
    rows: list[dict[str, Any]], timezone_offset_minutes: int
) -> list[dict[str, Any]]:
    timezone_shift = timezone_offset_minutes * 60
    hourly = [{"hour": hour, "durations": []} for hour in range(24)]
    for row in rows:
        if row["status"] != "succeeded" or row["completed_at"] is None:
            continue
        created_at = int(row["created_at"])
        hour = ((created_at + timezone_shift) // 3600) % 24
        hourly[hour]["durations"].append(
            max(0, int(row["completed_at"]) - created_at)
        )
    result = []
    for bucket in hourly:
        durations = bucket["durations"]
        average = round(sum(durations) / len(durations)) if durations else None
        count = len(durations)
        result.append({
            "hour": bucket["hour"],
            "average_seconds": average,
            "sample_count": count,
            "level": duration_level(average),
            "confidence": "high" if count >= 5 else "medium" if count >= 2 else "low" if count else "none",
        })
    return result


def daily_hour_heatmap(
    rows: list[dict[str, Any]], timezone_offset_minutes: int
) -> list[dict[str, Any]]:
    from datetime import datetime, timezone

    timezone_shift = timezone_offset_minutes * 60
    days: dict[str, list[list[int]]] = {}
    for row in rows:
        if row["status"] != "succeeded" or row["completed_at"] is None:
            continue
        created_at = int(row["created_at"])
        local_timestamp = created_at + timezone_shift
        date = datetime.fromtimestamp(local_timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
        hour = (local_timestamp // 3600) % 24
        days.setdefault(date, [[] for _ in range(24)])[hour].append(
            max(0, int(row["completed_at"]) - created_at)
        )
    return [
        {
            "date": date,
            "hours": [
                {
                    "hour": hour,
                    "average_seconds": round(sum(values) / len(values)) if values else None,
                    "sample_count": len(values),
                    "level": duration_level(round(sum(values) / len(values)) if values else None),
                }
                for hour, values in enumerate(hour_values)
            ],
        }
        for date, hour_values in sorted(days.items())
    ]


@app.get("/admin/tasks", dependencies=[Depends(require_admin_key)])
async def list_admin_tasks(
    limit: int = Query(default=50, ge=1, le=1000),
    page: int = Query(default=1, ge=1),
    account_id: str | None = None,
    status: Literal["all", "queued", "running", "succeeded", "failed"] = "all",
    search: str | None = Query(default=None, max_length=256),
    refresh_pending: bool = False,
) -> dict[str, Any]:
    selected_status = None if status == "all" else status
    records = store.list(
        limit=limit,
        offset=(page - 1) * limit,
        account_id=account_id,
        status=selected_status,
        search=search,
    )
    if refresh_pending:
        pending = [record for record in records if record.status in {"queued", "running"}]
        semaphore = asyncio.Semaphore(4)

        async def refresh_one(record: TaskRecord) -> None:
            async with semaphore:
                try:
                    await refresh(record)
                except Exception as exc:
                    logger.warning("Could not refresh task %s: %s", record.id, exc)

        await asyncio.gather(*(refresh_one(record) for record in pending))
        records = store.list(
            limit=limit,
            offset=(page - 1) * limit,
            account_id=account_id,
            status=selected_status,
            search=search,
        )
    return {
        "data": [admin_task(record) for record in records],
        "summary": store.task_counts(),
        "pagination": {
            "page": page,
            "page_size": limit,
            "total": store.count_tasks(
                account_id=account_id, status=selected_status, search=search
            ),
        },
    }


@app.get("/admin/analytics/durations", dependencies=[Depends(require_admin_key)])
async def admin_duration_analytics(
    range: Literal["7d", "30d", "90d"] = "30d",
    timezone_offset: int = Query(default=480, ge=-720, le=840),
) -> dict[str, Any]:
    now = int(time.time())
    range_seconds = {"7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400}[range]
    since = now - range_seconds
    rows = store.duration_analytics_rows(since=since, until=now)
    hourly = hourly_duration_profile(rows, timezone_offset)
    return {
        "range": range,
        "since": since,
        "until": now,
        "hourly": hourly,
        "heatmap": daily_hour_heatmap(rows, timezone_offset),
        "recommended_hours": [item["hour"] for item in hourly if item["level"] == "recommended"],
        "busy_hours": [item["hour"] for item in hourly if item["level"] == "busy"],
        "completed_samples": sum(item["sample_count"] for item in hourly),
    }


@app.get("/admin/logs", dependencies=[Depends(require_admin_key)])
async def list_admin_logs(
    limit: int = Query(default=50, ge=1, le=1000),
    page: int = Query(default=1, ge=1),
    level: Literal["all", "info", "success", "warning", "error"] = "all",
    category: Literal["all", "system", "account", "login", "video"] = "all",
    search: str | None = Query(default=None, max_length=256),
) -> dict[str, Any]:
    events = store.list_events(
        limit=limit,
        offset=(page - 1) * limit,
        level=None if level == "all" else level,
        category=None if category == "all" else category,
        search=search,
    )
    selected_level = None if level == "all" else level
    selected_category = None if category == "all" else category
    return {
        "data": [
            {
                "id": event.id,
                "created_at": event.created_at,
                "level": event.level,
                "category": event.category,
                "message": event.message,
                "account_id": event.account_id,
                "task_id": event.task_id,
                "details": event.details,
            }
            for event in events
        ],
        "pagination": {
            "page": page,
            "page_size": limit,
            "total": store.count_events(
                level=selected_level, category=selected_category, search=search
            ),
        },
    }


@app.post(
    "/api/v3/contents/generations/tasks",
    dependencies=[Depends(require_api_key)],
    summary="创建 Seedance 视频",
    description=SEEDANCE_CREATE_DESCRIPTION,
    responses={
        403: {
            "description": "TikTok 子账号没有所请求模型的使用权限",
        }
    },
)
async def create_seedance_video(
    body: SeedanceCreateRequest = Body(
        openapi_examples={
            "text_to_video": {
                "summary": "文生视频（T2V）",
                "value": {
                    "model": "seedance-2.0",
                    "content": [{"type": "text", "text": "一颗红球在白色桌面上缓慢滚动"}],
                    "duration": 5,
                },
            },
            "image_to_video": {
                "summary": "单首帧图生视频（I2V）",
                "value": {
                    "model": "seedance-2.0",
                    "content": [
                        {"type": "text", "text": "让画面中的云朵缓慢移动"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/first-frame.png"},
                            "role": "first_frame",
                        },
                    ],
                    "duration": 5,
                },
            },
            "reference_to_video": {
                "summary": "多模态参考生视频（R2V）",
                "value": {
                    "model": "seedance-2.0",
                    "content": [
                        {"type": "text", "text": "参考图片主体和音频节奏生成视频"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/reference.png"},
                            "role": "reference_image",
                        },
                        {
                            "type": "audio_url",
                            "audio_url": {"url": "https://example.com/reference.mp3"},
                            "role": "reference_audio",
                        },
                    ],
                    "duration": 5,
                },
            },
        }
    ),
) -> dict[str, Any]:
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
    audit_event(
        "info",
        "video",
        "视频任务已提交",
        account_id=record.account_id,
        task_id=record.id,
        details={
            "mode": mode,
            "model": upstream_model_name(record.model),
            "duration": record.seconds,
        },
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


@app.post(
    "/v1/videos",
    dependencies=[Depends(require_api_key)],
    summary="创建视频（OpenAI 兼容）",
    description=OPENAI_CREATE_DESCRIPTION,
    responses={
        403: {
            "description": "TikTok 子账号没有所请求模型的使用权限",
        }
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": OPENAI_JSON_CREATE_SCHEMA,
                    "examples": {
                        "text_to_video": {
                            "summary": "文生视频（T2V）",
                            "value": {
                                "model": "sora-2",
                                "prompt": "一颗红球在白色桌面上缓慢滚动",
                                "seconds": 5,
                                "size": "720x1280",
                            },
                        },
                        "image_to_video": {
                            "summary": "图片 URL 单首帧（I2V）",
                            "value": {
                                "model": "sora-2",
                                "prompt": "让画面中的云朵缓慢移动",
                                "seconds": 5,
                                "input_reference": {
                                    "image_url": "https://example.com/first-frame.png"
                                },
                            },
                        },
                        "reference_to_video": {
                            "summary": "多模态参考（R2V）",
                            "value": {
                                "model": "sora-2",
                                "prompt": "参考图片主体和音频节奏生成视频",
                                "seconds": 5,
                                "references": [
                                    {
                                        "type": "image_url",
                                        "image_url": "https://example.com/reference.png",
                                        "role": "reference_image",
                                    },
                                    {
                                        "type": "audio_url",
                                        "audio_url": "https://example.com/reference.mp3",
                                        "role": "reference_audio",
                                    },
                                ],
                            },
                        },
                    },
                },
                "multipart/form-data": {"schema": OPENAI_MULTIPART_CREATE_SCHEMA},
            },
        }
    },
)
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
    audit_event(
        "info",
        "video",
        "视频任务已提交",
        account_id=record.account_id,
        task_id=record.id,
        details={
            "mode": "reference" if reference_media else "image" if input_image else "text",
            "model": upstream_model_name(record.model),
            "duration": record.seconds,
        },
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

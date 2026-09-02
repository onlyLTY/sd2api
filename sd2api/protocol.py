from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import inspect
import json
import mimetypes
import time
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession
from curl_cffi.requests import Cookies as CurlCookies
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

from .config import Settings
from .models import UpstreamTask
from .tiktok import (
    I2V_MODELS,
    MODEL_PERMISSION_CODES,
    R2V_MODELS,
    T2V_MODELS,
    TikTokUpstreamError,
    _deep_find,
    _first,
    is_tiktok_authentication_error,
    tiktok_authentication_error,
)
from .uploads import StagedMedia


ACCOUNT_LIST_PATH = "/CreativeOne/Client/ClientGetAccountList"
ACCOUNT_INFO_PATH = "/CreativeOne/Client/ClientGetAccountInfo"
CREDIT_ACCOUNT_PATH = "/CreativeOne/SymphonyPlatform/QueryCreditAccount"
USER_LEVEL_PATH = "/CreativeOne/SymphonyPlatform/QueryUserLevelDetail"
MINIAPP_PERMISSION_PATH = (
    "/creative_bff_i18n/api/cue/get_miniapp_permission_with_allowlist"
)
UPLOAD_TOKEN_PATH = "/creative_bff_i18n/api/cue/upload/token"
T2V_CREATE_PATH = "/creative_bff_i18n/api/cue/t2v/create_generate_task"
I2V_CREATE_PATH = "/creative_bff_i18n/api/cue/i2v/create_generate_task"
R2V_CREATE_PATH = "/creative_bff_i18n/api/cue/i2v/gen_r2v_video"
CHECK_TASK_PATH = "/creative_bff_i18n/api/cue/generate-task/check"
CHECK_TASK_MAX_RETRIES = 3
CHECK_TASK_RETRY_DELAY_SECONDS = 0.5
BIND_VIDEOS_PATH = "/creative_bff_i18n/api/cue/lego/bind_videos"
VIDEO_INFO_PATH = "/creative_bff_i18n/api/cue/lego/get_video_info"
ACCOUNT_CONTEXT_COOKIE = "s_aio_client_id"
SEEDANCE2_ALLOWLIST_TOOL = "cue_mini_i2v_seedance_2"
SEEDANCE2_USER_TIERS = {"T00", "T0", "T1"}

UPLOAD_PROXY_PATH = "/creative/creativestudio/upload-proxy"
UPLOAD_USER_ID = "ad_creative_tools_unknown_user"
IMAGE_SERVICE_ID = "n2703mo9gi"
VIDEO_SPACE_NAME = "ad_site"
REFERENCE_IMAGE = 1
REFERENCE_VIDEO = 2
REFERENCE_AUDIO = 101

@dataclass(frozen=True, slots=True)
class ProtocolSession:
    cookies: tuple[dict[str, Any], ...]
    device_id: str
    user_agent: str
    fp_id: str = ""
    sec_ch_ua: str = ""
    sec_ch_ua_mobile: str = ""
    sec_ch_ua_platform: str = ""
    captured_at: int = 0
    version: int = 1

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProtocolSession":
        cookies = value.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            raise ValueError("The stored TikTok protocol session has no cookies")
        normalized = tuple(dict(item) for item in cookies if isinstance(item, dict))
        if not normalized:
            raise ValueError("The stored TikTok protocol session has invalid cookies")
        device_id = str(value.get("device_id") or "")
        if not device_id:
            device_cookie_names = {
                "monitor_device_id",
                "monitor_web_id",
                "web_id",
                "webid",
                "device_id",
                "did",
            }
            for item in normalized:
                if str(item.get("name") or "").lower() in device_cookie_names:
                    device_id = str(item.get("value") or "")
                    if device_id:
                        break
        return cls(
            cookies=normalized,
            device_id=device_id,
            user_agent=str(value.get("user_agent") or "Mozilla/5.0"),
            fp_id=str(value.get("fp_id") or ""),
            sec_ch_ua=str(value.get("sec_ch_ua") or ""),
            sec_ch_ua_mobile=str(value.get("sec_ch_ua_mobile") or ""),
            sec_ch_ua_platform=str(value.get("sec_ch_ua_platform") or ""),
            captured_at=int(value.get("captured_at") or 0),
            version=int(value.get("version") or 1),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "captured_at": self.captured_at,
            "cookies": [dict(item) for item in self.cookies],
            "device_id": self.device_id,
            "user_agent": self.user_agent,
            "fp_id": self.fp_id,
            "sec_ch_ua": self.sec_ch_ua,
            "sec_ch_ua_mobile": self.sec_ch_ua_mobile,
            "sec_ch_ua_platform": self.sec_ch_ua_platform,
        }

    def cookie_value(self, *names: str) -> str:
        wanted = {name.lower() for name in names}
        for item in self.cookies:
            if str(item.get("name") or "").lower() in wanted:
                return str(item.get("value") or "")
        return ""

    def cookie_jar(self, advertiser_id: str | None = None) -> httpx.Cookies:
        jar = httpx.Cookies()
        for item in self.cookies:
            name = str(item.get("name") or "")
            if not name or (advertiser_id and name == ACCOUNT_CONTEXT_COOKIE):
                continue
            value = str(item.get("value") or "")
            domain = str(item.get("domain") or ".tiktok.com")
            path = str(item.get("path") or "/")
            jar.set(name, value, domain=domain, path=path)
        if advertiser_id:
            jar.set(
                ACCOUNT_CONTEXT_COOKIE,
                advertiser_id,
                domain=".tiktok.com",
                path="/",
            )
        return jar


@dataclass(frozen=True, slots=True)
class UploadedMedia:
    kind: Literal["image", "video", "audio"]
    image_url: str | None = None
    image_uri: str | None = None
    vid: str | None = None
    poster_url: str | None = None


def _find_mapping(value: Any, required: set[str]) -> dict[str, Any] | None:
    if isinstance(value, dict):
        lowered = {str(key).lower() for key in value}
        if {item.lower() for item in required}.issubset(lowered):
            return value
        for child in value.values():
            found = _find_mapping(child, required)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_mapping(child, required)
            if found is not None:
                return found
    return None


def _dict_value(value: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): item for key, item in value.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _unwrap_data(payload: dict[str, Any]) -> Any:
    return payload.get("data", payload)


def _quote(value: Any) -> str:
    return quote(str(value), safe="-_.~")


def _query_string(params: dict[str, Any]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            pairs.append((_quote(key), _quote(item)))
    pairs.sort()
    return "&".join(f"{key}={value}" for key, value in pairs)


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _sts_time(token: dict[str, Any]) -> dt.datetime:
    current = _dict_value(token, "CurrentTime")
    if isinstance(current, (int, float)):
        stamp = float(current)
        if stamp > 1_000_000_000_000:
            stamp /= 1000
        return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc)
    if isinstance(current, str) and current:
        try:
            return dt.datetime.fromisoformat(current.replace("Z", "+00:00")).astimezone(
                dt.timezone.utc
            )
        except ValueError:
            pass
    return dt.datetime.now(dt.timezone.utc)


def _sign_gateway_request(
    *,
    method: str,
    path: str,
    params: dict[str, Any],
    token: dict[str, Any],
    service: str,
    body: bytes = b"",
    content_type: str | None = None,
) -> tuple[str, dict[str, str]]:
    access_key = str(_dict_value(token, "AccessKeyId", "AccessKeyID") or "")
    secret_key = str(_dict_value(token, "SecretAccessKey") or "")
    session_token = str(_dict_value(token, "SessionToken") or "")
    if not (access_key and secret_key and session_token):
        raise TikTokUpstreamError(
            "TikTok returned an incomplete upload STS token",
            code="upload_token_invalid",
        )
    now = _sts_time(token)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    region = "i18n"
    headers = {
        "X-Amz-Date": amz_date,
        "x-amz-security-token": session_token,
    }
    if body:
        headers["X-Amz-Content-Sha256"] = _sha256_hex(body)
    if content_type:
        headers["Content-Type"] = content_type

    signed = {
        key.lower(): " ".join(value.strip().split())
        for key, value in headers.items()
        if key.lower().startswith("x-amz-")
    }
    signed_names = ";".join(sorted(signed))
    canonical_headers = "".join(f"{key}:{signed[key]}\n" for key in sorted(signed))
    payload_hash = headers.get("X-Amz-Content-Sha256", _sha256_hex(b""))
    canonical_request = "\n".join(
        [
            method.upper(),
            path,
            _query_string(params),
            canonical_headers,
            signed_names,
            payload_hash,
        ]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, _sha256_hex(canonical_request.encode())]
    )
    date_key = _hmac(("AWS4" + secret_key).encode(), date_stamp)
    region_key = _hmac(date_key, region)
    service_key = _hmac(region_key, service)
    signing_key = _hmac(service_key, "aws4_request")
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    query = _query_string(params)
    return f"{path}?{query}" if query else path, headers


class ProtocolTikTokClient:
    """Pure-HTTP TikTok Creative Studio client for one subaccount context."""

    def __init__(
        self,
        settings: Settings,
        session: ProtocolSession,
        *,
        account_id: str,
        advertiser_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.account_id = account_id
        self.advertiser_id = advertiser_id
        self.transport = transport
        self._client: httpx.AsyncClient | CurlAsyncSession | None = None
        self._client_lock = asyncio.Lock()
        self._active_tasks: set[str] = set()
        self._inflight = 0
        self._upload_semaphore = asyncio.Semaphore(
            settings.sd2api_protocol_upload_concurrency
        )

    @property
    def load(self) -> int:
        return len(self._active_tasks) + self._inflight

    @property
    def params(self) -> dict[str, str]:
        return {
            "aid": "585599",
            "app_name": "creative_aio_client",
            "device_platform": "web",
        }

    @property
    def headers(self) -> dict[str, str]:
        csrf = self.session.cookie_value("csrftoken", "csrf_token", "tt_csrf_token")
        creative_csrf = self.session.cookie_value(
            "creative_csrf_token", "creative-csrf-token", "x-creative-csrf-token"
        ) or csrf
        result = {
            "accept": "application/json, text/plain, */*",
            "origin": self.settings.tiktok_base_url.rstrip("/"),
            "referer": self.settings.tiktok_base_url.rstrip("/")
            + "/creative/creativestudio/image-to-video"
            + "?subApp=CreativeStudio/MiniApp/TextToVideo",
            "user-agent": self.session.user_agent,
            "x-creative-source": "CreativeStudio/MiniApp/ImageToVideo",
            "agw-js-conv": "str",
        }
        if csrf:
            result["x-csrftoken"] = csrf
        if creative_csrf:
            result["x-creative-csrf-token"] = creative_csrf
        if self.session.fp_id:
            result["x-fp-id"] = self.session.fp_id
        if self.session.sec_ch_ua:
            result["sec-ch-ua"] = self.session.sec_ch_ua
        if self.session.sec_ch_ua_mobile:
            result["sec-ch-ua-mobile"] = self.session.sec_ch_ua_mobile
        if self.session.sec_ch_ua_platform:
            result["sec-ch-ua-platform"] = self.session.sec_ch_ua_platform
        return result

    async def _get_client(self) -> httpx.AsyncClient | CurlAsyncSession:
        async with self._client_lock:
            if self._client is None:
                if self.transport is not None:
                    # MockTransport keeps protocol tests deterministic. Real
                    # traffic uses curl_cffi below so TikTok sees Chromium's
                    # HTTP/2 and TLS fingerprint instead of Python/OpenSSL.
                    self._client = httpx.AsyncClient(
                        base_url=self.settings.tiktok_base_url,
                        headers=self.headers,
                        cookies=self.session.cookie_jar(self.advertiser_id),
                        timeout=self.settings.sd2api_request_timeout,
                        follow_redirects=True,
                        transport=self.transport,
                    )
                else:
                    cookies = CurlCookies()
                    for item in self.session.cookies:
                        name = str(item.get("name") or "")
                        if not name or (
                            self.advertiser_id and name == ACCOUNT_CONTEXT_COOKIE
                        ):
                            continue
                        cookies.set(
                            name,
                            str(item.get("value") or ""),
                            domain=str(item.get("domain") or ".tiktok.com"),
                            path=str(item.get("path") or "/"),
                            secure=bool(item.get("secure")),
                        )
                    if self.advertiser_id:
                        cookies.set(
                            ACCOUNT_CONTEXT_COOKIE,
                            self.advertiser_id,
                            domain=".tiktok.com",
                            path="/",
                            secure=True,
                        )
                    self._client = CurlAsyncSession(
                        base_url=self.settings.tiktok_base_url,
                        headers=self.headers,
                        cookies=cookies,
                        timeout=self.settings.sd2api_request_timeout,
                        allow_redirects=True,
                        impersonate="chrome",
                        default_headers=False,
                        max_clients=max(
                            4, self.settings.sd2api_protocol_upload_concurrency + 2
                        ),
                    )
            return self._client

    async def close(self) -> None:
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            close = getattr(client, "aclose", None) or getattr(client, "close")
            result = close()
            if inspect.isawaitable(result):
                await result

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        try:
            request_params: dict[str, Any] = dict(self.params)
            if params:
                request_params.update(params)
            response = await client.request(
                method,
                path,
                json=json_body,
                data=data,
                params=request_params,
            )
        except (httpx.HTTPError, CurlRequestException) as exc:
            raise TikTokUpstreamError(
                f"TikTok request failed: {exc.__class__.__name__}"
            ) from exc
        if response.status_code == 401:
            raise TikTokUpstreamError(
                "TikTok session expired; the account must log in again",
                status_code=401,
                code="tiktok_authentication_error",
            )
        if response.status_code == 403:
            try:
                forbidden = response.json()
            except ValueError:
                forbidden = {}
            forbidden_base = forbidden.get("BaseResp") if isinstance(forbidden, dict) else None
            code = (
                forbidden_base.get("StatusCode")
                if isinstance(forbidden_base, dict)
                else None
            ) or (
                _first(forbidden, ("code", "status_code", "statusCode"))
                if isinstance(forbidden, dict)
                else None
            )
            message = (
                _first(forbidden, ("message", "msg", "status_message"))
                if isinstance(forbidden, dict)
                else None
            ) or (
                forbidden_base.get("StatusMessage")
                if isinstance(forbidden_base, dict)
                else None
            )
            error = TikTokUpstreamError(
                str(message or "TikTok denied access to this model or operation"),
                status_code=403,
                code=str(code or "tiktok_permission_denied"),
            )
            if is_tiktok_authentication_error(error):
                raise tiktok_authentication_error() from error
            raise error
        if response.status_code >= 400:
            try:
                rejected = response.json()
            except ValueError:
                rejected = {}
            rejected_base = (
                rejected.get("BaseResp") if isinstance(rejected, dict) else None
            )
            rejected_code = (
                rejected_base.get("StatusCode")
                if isinstance(rejected_base, dict)
                else None
            ) or (
                _first(rejected, ("code", "status_code", "statusCode"))
                if isinstance(rejected, dict)
                else None
            )
            rejected_message = (
                _first(rejected, ("message", "msg", "status_message"))
                if isinstance(rejected, dict)
                else None
            ) or (
                rejected_base.get("StatusMessage")
                if isinstance(rejected_base, dict)
                else None
            )
            error = TikTokUpstreamError(
                str(
                    rejected_message
                    or f"TikTok returned HTTP {response.status_code} for {path}"
                ),
                status_code=response.status_code,
                code=str(rejected_code or f"tiktok_http_{response.status_code}"),
            )
            if is_tiktok_authentication_error(error):
                raise tiktok_authentication_error() from error
            raise error
        try:
            payload = response.json()
        except ValueError as exc:
            raise TikTokUpstreamError("TikTok returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise TikTokUpstreamError("TikTok returned an unexpected response shape")
        base_response = payload.get("BaseResp")
        base_code = (
            base_response.get("StatusCode") if isinstance(base_response, dict) else None
        )
        code = _first(payload, ("code", "status_code", "statusCode"))
        effective = base_code if base_code not in (None, 0, "0") else code
        if effective not in (None, 0, "0", 200, "200"):
            message = (
                _first(payload, ("message", "msg", "status_message"))
                or (
                    base_response.get("StatusMessage")
                    if isinstance(base_response, dict)
                    else None
                )
                or "TikTok rejected the request"
            )
            error = TikTokUpstreamError(
                str(message),
                status_code=(
                    403 if str(effective) in MODEL_PERMISSION_CODES else 502
                ),
                code=str(effective),
            )
            if is_tiktok_authentication_error(error):
                raise tiktok_authentication_error() from error
            raise error
        return payload

    async def validate(self) -> dict[str, Any]:
        return await self._request("GET", ACCOUNT_INFO_PATH)

    async def discover_subaccounts(self) -> list[dict[str, Any]]:
        account_list, account_info = await asyncio.gather(
            self._request("GET", ACCOUNT_LIST_PATH),
            self._request("GET", ACCOUNT_INFO_PATH),
        )
        list_data = _unwrap_data(account_list)
        info_data = _unwrap_data(account_info)
        accounts = (
            list_data.get("accounts") if isinstance(list_data, dict) else None
        ) or account_list.get("accounts")
        active_account = (
            info_data.get("account") if isinstance(info_data, dict) else None
        ) or account_info.get("account") or {}
        active_id = str(active_account.get("aioClientID") or "")
        if not isinstance(accounts, list) or not accounts:
            raise TikTokUpstreamError(
                "TikTok did not return any Client or Partner accounts",
                code="subaccount_scan_failed",
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
        return result

    @staticmethod
    def _parse_credits(payload: dict[str, Any]) -> int | None:
        values = (
            _deep_find(payload, {"Credits", "credits", "creditBalance", "balance"}),
            _deep_find(payload, {"Bonus", "bonus", "bonusCredits", "bonus_credits"}),
        )
        parsed: list[int] = []
        for value in values:
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
        tools = _deep_find(permissions, {"allowlist", "tools", "permissions"})
        serialized = json.dumps(tools if tools is not None else permissions)
        if SEEDANCE2_ALLOWLIST_TOOL in serialized:
            return True
        tier = _deep_find(user_level, {"user_segment_tier", "userSegmentTier"})
        return str(tier or "").upper() in SEEDANCE2_USER_TIERS

    async def account_capabilities(self) -> dict[str, Any]:
        info, credit, permissions, level = await asyncio.gather(
            self._request("GET", ACCOUNT_INFO_PATH),
            self._request("POST", CREDIT_ACCOUNT_PATH, json_body={}),
            self._request("GET", MINIAPP_PERMISSION_PATH),
            self._request("POST", USER_LEVEL_PATH, json_body={}),
        )
        info_data = _unwrap_data(info)
        account = (
            info_data.get("account") if isinstance(info_data, dict) else None
        ) or info.get("account") or {}
        return {
            "advertiser_id": str(account.get("aioClientID") or ""),
            "credits": self._parse_credits(credit),
            "seedance_access": self._seedance2_access(permissions, level),
        }

    def _require_generation_identity(self) -> None:
        if not self.session.fp_id:
            raise TikTokUpstreamError(
                "The captured TikTok session has no web fingerprint ID; refresh the login session once",
                status_code=409,
                code="protocol_fp_id_missing",
            )

    async def create_text_video(self, *, prompt: str, model: str, duration: int) -> str:
        self._require_generation_identity()
        internal = self._model(model, T2V_MODELS)
        settings = {
            "aiModel": internal,
            "duration": duration,
            "prompt": prompt,
            "useEnhancePrompt": False,
            "useReferencePrompt": False,
        }
        return await self._create_task(
            T2V_CREATE_PATH,
            {
                "prompt": prompt,
                "gokuModel": internal,
                "model": internal,
                "duration": duration,
                "settings": json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
            },
        )

    async def create_image_video(
        self, *, prompt: str, model: str, duration: int, image_path: str
    ) -> str:
        self._require_generation_identity()
        internal = self._model(model, I2V_MODELS)
        uploaded = await self.upload_media(StagedMedia(kind="image", path=image_path))
        image_url = uploaded.image_url or ""
        settings = {
            "rawImage": image_url,
            "image": image_url,
            "images": [{"previewUrl": image_url, "fileType": "image"}],
            "aiModel": internal,
            "imageIndex": 0,
            "animationType": "prompt",
            "duration": duration,
            "prompt": prompt,
        }
        return await self._create_task(
            I2V_CREATE_PATH,
            {
                "image": image_url,
                "images": [image_url],
                "prompt": prompt,
                "duration": duration,
                "model": internal,
                "settings": json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
            },
        )

    async def create_reference_video(
        self,
        *,
        prompt: str,
        model: str,
        duration: int,
        media: list[StagedMedia],
    ) -> str:
        self._require_generation_identity()
        internal = self._model(model, R2V_MODELS)
        uploaded = await asyncio.gather(*(self.upload_media(item) for item in media))
        images = [item.image_url or "" for item in uploaded if item.kind == "image"]
        mentions: list[dict[str, Any]] = []
        settings_media: list[dict[str, Any]] = []
        for item in uploaded:
            if item.kind == "image":
                identifier = item.image_url or ""
                reference_type = REFERENCE_IMAGE
                settings_media.append(
                    {"previewUrl": identifier, "fileType": "image"}
                )
            else:
                identifier = item.vid or ""
                reference_type = (
                    REFERENCE_VIDEO if item.kind == "video" else REFERENCE_AUDIO
                )
                settings_media.append(
                    {
                        "previewUrl": item.poster_url or "",
                        "fileType": item.kind,
                        "vid": identifier,
                    }
                )
            mentions.append({"type": reference_type, "id": identifier})
        settings = {
            "images": settings_media,
            "prompt": prompt,
            "aiModel": internal,
            "duration": duration,
        }
        return await self._create_task(
            R2V_CREATE_PATH,
            {
                "image": "",
                "images": images,
                "prompt": prompt,
                "duration": duration,
                "model": internal,
                "settings": json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
                "mentions": mentions,
            },
        )

    @staticmethod
    def _model(model: str, models: dict[str, str]) -> str:
        internal = models.get(model.lower())
        if not internal:
            raise TikTokUpstreamError(
                f"Unsupported model {model!r}",
                status_code=400,
                code="invalid_model",
            )
        return internal

    async def _create_task(self, path: str, body: dict[str, Any]) -> str:
        self._inflight += 1
        try:
            payload = await self._request("POST", path, json_body=body)
        finally:
            self._inflight -= 1
        task_id = _deep_find(payload, {"taskId", "task_id", "TaskId"})
        if task_id is None:
            data = payload.get("data")
            if isinstance(data, (str, int)):
                task_id = data
        if task_id is None:
            raise TikTokUpstreamError(
                "TikTok accepted the request but did not return a task ID"
            )
        result = str(task_id)
        self._active_tasks.add(result)
        return result

    async def check_task(self, task_id: str) -> UpstreamTask:
        payload = await self._check_task_payload(task_id)
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
        error_code = _first(
            draft, ("generateErrorCode", "renderErrorCode", "errorCode")
        )
        error_message = _first(
            draft,
            (
                "generateErrorMessage",
                "renderErrorMessage",
                "errorMessage",
                "message",
            ),
        )
        # Numeric status 2 means generating, not failed. TikTok transitions
        # 2/2 -> 0/0 + VID when rendering completes. A real failure carries
        # an error field or an explicit failed/negative terminal status.
        failed_values = {-1, "-1", 3, "3", "FAILED", "failed", "FAIL", "fail"}
        failed = (
            error_code not in (None, "", 0, "0")
            or bool(error_message)
            or draft_status in failed_values
            or render_status in failed_values
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
        video_url = self._extract_video_url(draft)
        poster_url = self._extract_poster_url(draft)
        if succeeded and video_id and not video_url:
            info = await self._video_info(str(video_id))
            video_url = self._extract_video_url(info)
            poster_url = poster_url or self._extract_poster_url(info)
        if status in {"failed", "succeeded"}:
            self._active_tasks.discard(task_id)
        return UpstreamTask(
            id=task_id,
            status=status,
            progress=progress,
            video_id=str(video_id) if video_id is not None else None,
            video_url=video_url,
            poster_url=poster_url,
            error_code=str(error_code) if error_code not in (None, "", 0, "0") else None,
            error_message=str(error_message) if error_message else None,
            raw=payload,
        )

    async def _check_task_payload(self, task_id: str) -> dict[str, Any]:
        for attempt in range(CHECK_TASK_MAX_RETRIES + 1):
            try:
                return await self._request(
                    "POST", CHECK_TASK_PATH, json_body={"taskId": task_id}
                )
            except TikTokUpstreamError as exc:
                retryable = (
                    500 <= exc.status_code < 600
                    and exc.code.startswith("tiktok_http_")
                )
                if not retryable or attempt >= CHECK_TASK_MAX_RETRIES:
                    raise
                await asyncio.sleep(
                    CHECK_TASK_RETRY_DELAY_SECONDS * (attempt + 1)
                )
        raise RuntimeError("Task status retry loop exited unexpectedly")

    async def _video_info(self, vid: str) -> dict[str, Any]:
        try:
            await self._request("POST", BIND_VIDEOS_PATH, json_body={"vids": [vid]})
        except TikTokUpstreamError:
            pass
        return await self._request("GET", VIDEO_INFO_PATH, params={"vid": vid})

    @classmethod
    def _extract_video_url(cls, value: Any) -> str | None:
        url_keys = (
            "MainUrl",
            "mainUrl",
            "MainHTTPUrl",
            "mainHTTPUrl",
            "BackupUrl",
            "backupUrl",
            "BackupHTTPUrl",
            "backupHTTPUrl",
            "video_url",
            "videoUrl",
            "previewLink",
            "encode_url",
            "src",
        )

        def mapping_url(mapping: Any) -> str | None:
            if not isinstance(mapping, dict):
                return None
            candidate = _first(mapping, url_keys)
            return (
                candidate
                if isinstance(candidate, str) and candidate.startswith("http")
                else None
            )

        # Prefer the original 720p asset. The transcode list is ordered from
        # low to high quality and would otherwise return its 360p first item.
        original = _deep_find(value, {"OriginalVideoInfo", "originalVideoInfo"})
        if result := mapping_url(original):
            return result
        variants = _deep_find(value, {"VideoInfos", "videoInfos"})
        if isinstance(variants, list):
            ranked = sorted(
                (item for item in variants if isinstance(item, dict)),
                key=lambda item: int(
                    _deep_find(item, {"Size", "size", "Height", "height"}) or 0
                ),
                reverse=True,
            )
            for item in ranked:
                if result := mapping_url(item):
                    return result

        def find_http(node: Any) -> str | None:
            if isinstance(node, dict):
                for key in url_keys:
                    candidate = node.get(key)
                    if isinstance(candidate, str) and candidate.startswith("http"):
                        return candidate
                for child in node.values():
                    if result := find_http(child):
                        return result
            elif isinstance(node, list):
                for child in node:
                    if result := find_http(child):
                        return result
            return None

        return find_http(value)

    @staticmethod
    def _extract_poster_url(value: Any) -> str | None:
        candidate = _deep_find(value, {"PosterUrl", "posterUrl", "coverImage"})
        return str(candidate) if isinstance(candidate, str) and candidate.startswith("http") else None

    async def fetch_video(self, video_url: str) -> tuple[bytes, str]:
        client = await self._get_client()
        response = await client.get(video_url, timeout=180)
        if response.status_code >= 400:
            raise TikTokUpstreamError(
                f"TikTok CDN returned HTTP {response.status_code}",
                code="video_download_failed",
            )
        return response.content, response.headers.get("content-type", "video/mp4").split(
            ";", 1
        )[0]

    async def upload_media(self, media: StagedMedia) -> UploadedMedia:
        async with self._upload_semaphore:
            path = Path(media.path)
            if not path.is_file():
                raise TikTokUpstreamError(
                    f"Staged {media.kind} file is unavailable",
                    status_code=422,
                    code="staged_media_missing",
                )
            token = await self._upload_token()
            return await self._upload_file(path, media.kind, token)

    async def _upload_token(self) -> dict[str, Any]:
        payload = await self._request("POST", UPLOAD_TOKEN_PATH, json_body={})
        token = _find_mapping(payload, {"SecretAccessKey", "SessionToken"})
        if token is None:
            raise TikTokUpstreamError(
                "TikTok did not return upload credentials",
                code="upload_token_invalid",
            )
        return token

    async def _upload_file(
        self,
        path: Path,
        kind: Literal["image", "video", "audio"],
        token: dict[str, Any],
    ) -> UploadedMedia:
        size = path.stat().st_size
        service = "imagex" if kind == "image" else "vod"
        apply_params: dict[str, Any]
        if kind == "image":
            apply_params = {
                "Action": "ApplyImageUpload",
                "Version": "2018-08-01",
                "ServiceId": IMAGE_SERVICE_ID,
                "FileSize": size,
                "FileExtension": path.suffix,
                "s": uuid.uuid4().hex[:12],
                "device_platform": "web",
            }
        else:
            apply_params = {
                "Action": "ApplyUploadInner",
                "Version": "2020-11-19",
                "SpaceName": VIDEO_SPACE_NAME,
                "FileType": "video",
                "IsInner": 1,
                "FileSize": size,
                "s": uuid.uuid4().hex[:12],
                "device_platform": "web",
            }
        apply_payload = await self._signed_upload_proxy(
            "GET", apply_params, token=token, service=service
        )
        upload_address = self._upload_address(apply_payload, service)
        upload_result = await self._transfer_file(path, upload_address)
        commit_payload = await self._commit_upload(
            upload_address, token=token, service=service, kind=kind
        )
        result = self._commit_result(commit_payload)
        if kind == "image":
            image_uri = str(result.get("ImageUri") or result.get("StoreUri") or "")
            image_url = str(
                result.get("ImageUrl")
                or result.get("imageUrl")
                or (
                    f"https://p19-creative-tool-sg.ibyteimg.com/{image_uri}"
                    f"~tplv-{IMAGE_SERVICE_ID}-webp:1280:1280.image"
                    if image_uri
                    else ""
                )
            )
            if not image_url:
                raise TikTokUpstreamError(
                    "TikTok image upload completed without an image URL",
                    code="upload_result_invalid",
                )
            return UploadedMedia(kind="image", image_url=image_url, image_uri=image_uri)
        vid = str(result.get("Vid") or result.get("vid") or "")
        if not vid:
            vid = str(upload_result.get("Vid") or upload_result.get("vid") or "")
        if not vid:
            raise TikTokUpstreamError(
                "TikTok media upload completed without a VID",
                code="upload_result_invalid",
            )
        await self._request("POST", BIND_VIDEOS_PATH, json_body={"vids": [vid]})
        info = await self._request("GET", VIDEO_INFO_PATH, params={"vid": vid})
        return UploadedMedia(
            kind=kind,
            vid=vid,
            poster_url=self._extract_poster_url(info),
        )

    async def _signed_upload_proxy(
        self,
        method: str,
        params: dict[str, Any],
        *,
        token: dict[str, Any],
        service: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            if body is not None
            else b""
        )
        path, headers = _sign_gateway_request(
            method=method,
            path=UPLOAD_PROXY_PATH,
            params=params,
            token=token,
            service=service,
            body=content,
            content_type="application/json" if body is not None and service == "imagex" else None,
        )
        client = await self._get_client()
        response = await client.request(method, path, headers=headers, content=content or None)
        if response.status_code >= 400:
            raise TikTokUpstreamError(
                f"TikTok upload gateway returned HTTP {response.status_code}",
                code="upload_gateway_error",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TikTokUpstreamError("TikTok upload gateway returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TikTokUpstreamError("TikTok upload gateway returned invalid data")
        metadata = payload.get("ResponseMetadata")
        if isinstance(metadata, dict) and metadata.get("Error"):
            error = metadata["Error"]
            raise TikTokUpstreamError(
                str(error.get("Message") or error.get("Code") or "Upload gateway failed"),
                code="upload_gateway_error",
            )
        return payload

    @staticmethod
    def _upload_address(payload: dict[str, Any], service: str) -> dict[str, Any]:
        result = payload.get("Result")
        if not isinstance(result, dict):
            raise TikTokUpstreamError("TikTok upload apply response has no Result")
        if service == "imagex":
            address = result.get("UploadAddress")
            if not isinstance(address, dict):
                raise TikTokUpstreamError("TikTok image upload has no UploadAddress")
            hosts = address.get("UploadHosts") or []
            host = hosts[0] if isinstance(hosts, list) and hosts else None
        else:
            inner = result.get("InnerUploadAddress")
            nodes = inner.get("UploadNodes") if isinstance(inner, dict) else None
            address = nodes[0] if isinstance(nodes, list) and nodes else None
            host = address.get("UploadHost") if isinstance(address, dict) else None
        stores = address.get("StoreInfos") if isinstance(address, dict) else None
        store = stores[0] if isinstance(stores, list) and stores else None
        if not isinstance(address, dict) or not isinstance(store, dict) or not host:
            raise TikTokUpstreamError("TikTok upload apply response is incomplete")
        return {
            "host": str(host),
            "oid": str(store.get("StoreUri") or ""),
            "signature": str(store.get("Auth") or ""),
            "upload_id": str(store.get("UploadID") or ""),
            "session_key": str(address.get("SessionKey") or ""),
            "headers": dict(address.get("UploadHeader") or {}),
        }

    async def _transfer_file(
        self, path: Path, address: dict[str, Any]
    ) -> dict[str, Any]:
        size = path.stat().st_size
        if size <= self.settings.sd2api_protocol_direct_upload_bytes:
            content = await asyncio.to_thread(path.read_bytes)
            return await self._upload_binary(
                f"https://{address['host']}/upload/v1/{address['oid']}",
                content,
                address,
            )
        return await self._upload_parts(path, address)

    async def _upload_parts(
        self, path: Path, address: dict[str, Any]
    ) -> dict[str, Any]:
        base = f"https://{address['host']}/upload/v1/{address['oid']}"
        headers = self._storage_headers(address)
        init = await (await self._get_client()).post(
            base + "?uploadmode=part&phase=init", headers=headers
        )
        init_payload = self._storage_json(init, "initialize")
        upload_id = str(_deep_find(init_payload, {"uploadid", "uploadId"}) or "")
        if not upload_id:
            raise TikTokUpstreamError("TikTok multipart upload returned no upload ID")
        crcs: list[str] = []
        part_number = 0
        with path.open("rb") as source:
            while chunk := source.read(self.settings.sd2api_protocol_slice_bytes):
                part_number += 1
                crc = f"{zlib.crc32(chunk) & 0xFFFFFFFF:08x}"
                url = (
                    f"{base}?uploadid={_quote(upload_id)}&part_number={part_number}"
                    "&phase=transfer"
                )
                await self._upload_binary(url, chunk, address, crc=crc)
                crcs.append(crc)
        body = ",".join(f"{index}:{crc}" for index, crc in enumerate(crcs, 1))
        finish_url = (
            f"{base}?uploadmode=part&phase=finish&uploadid={_quote(upload_id)}"
            f"&size={path.stat().st_size}"
        )
        finish = await (await self._get_client()).post(
            finish_url, headers=headers, content=body.encode()
        )
        return self._storage_json(finish, "finish")

    async def _upload_binary(
        self,
        url: str,
        content: bytes,
        address: dict[str, Any],
        *,
        crc: str | None = None,
    ) -> dict[str, Any]:
        headers = self._storage_headers(address)
        headers.update(
            {
                "Content-CRC32": crc or f"{zlib.crc32(content) & 0xFFFFFFFF:08x}",
                "Content-Type": "application/octet-stream",
            }
        )
        response = await (await self._get_client()).post(
            url, headers=headers, content=content, timeout=1800
        )
        return self._storage_json(response, "transfer")

    @staticmethod
    def _storage_headers(address: dict[str, Any]) -> dict[str, str]:
        return {
            "Authorization": str(address["signature"]),
            "X-Storage-U": UPLOAD_USER_ID,
            **{str(key): str(value) for key, value in address.get("headers", {}).items()},
        }

    @staticmethod
    def _storage_json(response: httpx.Response, stage: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise TikTokUpstreamError(
                f"TikTok storage {stage} returned HTTP {response.status_code}",
                code="storage_upload_error",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TikTokUpstreamError(
                f"TikTok storage {stage} returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict) or int(payload.get("code") or 0) != 2000:
            raise TikTokUpstreamError(
                str(payload.get("message") if isinstance(payload, dict) else "Storage failed"),
                code="storage_upload_error",
            )
        return payload

    async def _commit_upload(
        self,
        address: dict[str, Any],
        *,
        token: dict[str, Any],
        service: str,
        kind: str,
    ) -> dict[str, Any]:
        if service == "imagex":
            params = {
                "Action": "CommitImageUpload",
                "Version": "2018-08-01",
                "ServiceId": IMAGE_SERVICE_ID,
            }
            body: dict[str, Any] = {"SessionKey": address["session_key"]}
        else:
            params = {
                "Action": "CommitUploadInner",
                "Version": "2020-11-19",
                "SpaceName": VIDEO_SPACE_NAME,
            }
            body = {
                "SessionKey": address["session_key"],
                "Functions": [
                    {"name": "GetMeta"},
                    {"name": "Snapshot", "input": {"SnapshotTime": 0}},
                ],
            }
        return await self._signed_upload_proxy(
            "POST", params, token=token, service=service, body=body
        )

    @staticmethod
    def _commit_result(payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("Result")
        results = result.get("Results") if isinstance(result, dict) else None
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise TikTokUpstreamError("TikTok upload commit returned no result")
        combined = dict(results[0])
        plugins = result.get("PluginResult") if isinstance(result, dict) else None
        if isinstance(plugins, list) and plugins and isinstance(plugins[0], dict):
            combined.update(plugins[0])
        return combined

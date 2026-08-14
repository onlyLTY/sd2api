from __future__ import annotations

from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, PrivateAttr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import dotenv_values


CONFIG_PATH = Path("config.json")


class RuntimeConfig(BaseModel):
    tiktok_base_url: str = "https://ads.tiktok.com"
    tiktok_user_agent: str = "Mozilla/5.0"
    database: str = "sd2api.db"
    request_timeout: float = Field(default=60.0, gt=0, le=300)
    mode: str = "browser_pool"
    browser_profile: str = ".browser-profile"
    browser_channel: str = ""
    browser_headless: bool = False
    browser_max_wait: int = Field(default=1800, ge=60, le=7200)
    browser_autostart: bool = False
    pool_max_pending: int = Field(default=500, ge=1, le=100000)
    pool_subaccount_concurrency: int = Field(default=5, ge=1, le=20)
    pool_quota_cooldown: int = Field(default=86400, ge=300, le=604800)
    pool_daily_quota_codes: str = ""
    pool_start_concurrency: int = Field(default=3, ge=1, le=50)
    protocol_upload_concurrency: int = Field(default=3, ge=1, le=16)
    protocol_direct_upload_bytes: int = Field(
        default=5 * 1024 * 1024, ge=256 * 1024, le=64 * 1024 * 1024
    )
    protocol_slice_bytes: int = Field(
        default=3 * 1024 * 1024, ge=1024 * 1024, le=32 * 1024 * 1024
    )
    auto_login: bool = True
    login_timeout: int = Field(default=600, ge=60, le=3600)
    relogin_interval: int = Field(default=300, ge=30, le=86400)
    session_keepalive_interval: int = Field(default=21600, ge=3600, le=86400)
    temp_mail_poll_seconds: float = Field(default=3.0, ge=1, le=30)
    temp_mail_timeout: int = Field(default=180, ge=30, le=900)
    upload_dir: str = "uploads"
    upload_max_bytes: int = Field(
        default=200 * 1024 * 1024, ge=1024, le=500 * 1024 * 1024
    )
    upload_image_max_bytes: int = Field(
        default=30 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024
    )
    upload_video_max_bytes: int = Field(
        default=200 * 1024 * 1024, ge=1024, le=500 * 1024 * 1024
    )
    upload_audio_max_bytes: int = Field(
        default=15 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024
    )
    upload_max_pixels: int = Field(
        default=40_000_000, ge=1_000_000, le=200_000_000
    )


LEGACY_ENV_FIELDS = {
    "TIKTOK_BASE_URL": "tiktok_base_url",
    "TIKTOK_USER_AGENT": "tiktok_user_agent",
    "SD2API_DATABASE": "database",
    "SD2API_REQUEST_TIMEOUT": "request_timeout",
    "SD2API_MODE": "mode",
    "SD2API_BROWSER_PROFILE": "browser_profile",
    "SD2API_BROWSER_CHANNEL": "browser_channel",
    "SD2API_BROWSER_HEADLESS": "browser_headless",
    "SD2API_BROWSER_MAX_WAIT": "browser_max_wait",
    "SD2API_BROWSER_AUTOSTART": "browser_autostart",
    "SD2API_POOL_MAX_PENDING": "pool_max_pending",
    "SD2API_POOL_SUBACCOUNT_CONCURRENCY": "pool_subaccount_concurrency",
    "SD2API_POOL_QUOTA_COOLDOWN": "pool_quota_cooldown",
    "SD2API_POOL_DAILY_QUOTA_CODES": "pool_daily_quota_codes",
    "SD2API_POOL_START_CONCURRENCY": "pool_start_concurrency",
    "SD2API_PROTOCOL_UPLOAD_CONCURRENCY": "protocol_upload_concurrency",
    "SD2API_PROTOCOL_DIRECT_UPLOAD_BYTES": "protocol_direct_upload_bytes",
    "SD2API_PROTOCOL_SLICE_BYTES": "protocol_slice_bytes",
    "SD2API_AUTO_LOGIN": "auto_login",
    "SD2API_LOGIN_TIMEOUT": "login_timeout",
    "SD2API_RELOGIN_INTERVAL": "relogin_interval",
    "SD2API_SESSION_KEEPALIVE_INTERVAL": "session_keepalive_interval",
    "SD2API_TEMP_MAIL_POLL_SECONDS": "temp_mail_poll_seconds",
    "SD2API_TEMP_MAIL_TIMEOUT": "temp_mail_timeout",
    "SD2API_UPLOAD_DIR": "upload_dir",
    "SD2API_UPLOAD_MAX_BYTES": "upload_max_bytes",
    "SD2API_UPLOAD_IMAGE_MAX_BYTES": "upload_image_max_bytes",
    "SD2API_UPLOAD_VIDEO_MAX_BYTES": "upload_video_max_bytes",
    "SD2API_UPLOAD_AUDIO_MAX_BYTES": "upload_audio_max_bytes",
    "SD2API_UPLOAD_MAX_PIXELS": "upload_max_pixels",
}


def load_runtime_config(path: Path = CONFIG_PATH) -> tuple[RuntimeConfig, str]:
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read {path}: {exc}") from exc
        try:
            return RuntimeConfig.model_validate(payload), "file"
        except ValidationError as exc:
            raise RuntimeError(f"Invalid {path}: {exc}") from exc

    environment: dict[str, Any] = {}
    for env_file in (Path(".env"), Path(".env.docker")):
        if env_file.exists():
            environment.update(
                {key: value for key, value in dotenv_values(env_file).items() if value is not None}
            )
    environment.update(os.environ)
    legacy = {
        field: environment[name]
        for name, field in LEGACY_ENV_FIELDS.items()
        if name in environment
    }
    try:
        return RuntimeConfig.model_validate(legacy), "legacy_env" if legacy else "defaults"
    except ValidationError as exc:
        raise RuntimeError(f"Invalid legacy environment configuration: {exc}") from exc


def save_runtime_config(config: RuntimeConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.docker"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    tiktok_cookie: str = ""
    tiktok_csrf_token: str = ""
    tiktok_creative_csrf_token: str = ""
    tiktok_fp_id: str = ""
    sd2api_api_key: str = ""
    sd2api_admin_key: str = ""
    sd2api_credential_key: str = ""
    sd2api_temp_mail_base_url: str = ""
    sd2api_temp_mail_api_key: str = ""
    _runtime: RuntimeConfig = PrivateAttr()
    _config_source: str = PrivateAttr()

    _runtime_fields: ClassVar[dict[str, str]] = {
        f"sd2api_{name}": name
        for name in RuntimeConfig.model_fields
        if not name.startswith("tiktok_")
    } | {
        "tiktok_base_url": "tiktok_base_url",
        "tiktok_user_agent": "tiktok_user_agent",
    }

    def __init__(self, **values: Any) -> None:
        runtime_overrides: dict[str, Any] = {}
        for public_name, runtime_name in self._runtime_fields.items():
            if public_name in values:
                runtime_overrides[runtime_name] = values.pop(public_name)
        super().__init__(**values)
        if runtime_overrides:
            self.replace_runtime(self.runtime.model_copy(update=runtime_overrides), source="explicit")

    def model_post_init(self, __context: Any) -> None:
        runtime, source = load_runtime_config()
        self._runtime = runtime
        self._config_source = source

    def __getattr__(self, name: str) -> Any:
        private = object.__getattribute__(self, "__pydantic_private__")
        if name == "runtime":
            return private["_runtime"]
        if name == "config_source":
            return private["_config_source"]
        runtime_name = self._runtime_fields.get(name)
        if runtime_name:
            return getattr(private["_runtime"], runtime_name)
        raise AttributeError(name)

    def replace_runtime(self, runtime: RuntimeConfig, *, source: str = "file") -> None:
        self._runtime = runtime
        self._config_source = source

    @property
    def credential_master_key(self) -> str:
        return self.sd2api_credential_key or self.sd2api_admin_key

    @property
    def cookie_values(self) -> dict[str, str]:
        parsed = SimpleCookie()
        try:
            parsed.load(self.tiktok_cookie)
            return {key: value.value for key, value in parsed.items()}
        except Exception:
            result: dict[str, str] = {}
            for part in self.tiktok_cookie.split(";"):
                key, separator, value = part.strip().partition("=")
                if separator and key:
                    result[key] = value
            return result

    @property
    def csrf_token(self) -> str:
        if self.tiktok_csrf_token:
            return self.tiktok_csrf_token
        cookies = self.cookie_values
        for name in ("csrftoken", "csrf_token", "tt_csrf_token"):
            if cookies.get(name):
                return cookies[name]
        return ""

    @property
    def creative_csrf_token(self) -> str:
        if self.tiktok_creative_csrf_token:
            return self.tiktok_creative_csrf_token
        cookies = self.cookie_values
        for name in ("creative_csrf_token", "creative-csrf-token", "x-creative-csrf-token"):
            if cookies.get(name):
                return cookies[name]
        return self.csrf_token

    @property
    def session_device_id(self) -> str:
        cookies = self.cookie_values
        for name in ("MONITOR_DEVICE_ID", "monitor_device_id", "device_id", "did"):
            if cookies.get(name):
                return cookies[name]
        return ""

    def validate_tiktok_auth(self) -> None:
        if not self.tiktok_cookie.strip():
            raise RuntimeError("Missing TikTok configuration: TIKTOK_COOKIE")


settings = Settings()

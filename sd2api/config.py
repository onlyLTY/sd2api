from __future__ import annotations

from http.cookies import SimpleCookie

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    tiktok_cookie: str = ""
    tiktok_csrf_token: str = ""
    tiktok_creative_csrf_token: str = ""
    tiktok_fp_id: str = ""
    tiktok_user_agent: str = "Mozilla/5.0"
    tiktok_base_url: str = "https://ads.tiktok.com"

    sd2api_api_key: str = ""
    sd2api_admin_key: str = ""
    sd2api_database: str = "sd2api.db"
    sd2api_request_timeout: float = Field(default=60.0, gt=0, le=300)
    sd2api_mode: str = "direct"
    sd2api_browser_profile: str = ".browser-profile"
    sd2api_browser_channel: str = "msedge"
    sd2api_browser_headless: bool = False
    sd2api_browser_max_wait: int = Field(default=1800, ge=60, le=7200)
    sd2api_browser_autostart: bool = False
    sd2api_pool_max_pending: int = Field(default=500, ge=1, le=100000)
    sd2api_pool_start_concurrency: int = Field(default=3, ge=1, le=50)
    sd2api_credential_key: str = ""
    sd2api_auto_login: bool = True
    sd2api_auto_accept_terms: bool = False
    sd2api_login_timeout: int = Field(default=600, ge=60, le=3600)
    sd2api_relogin_interval: int = Field(default=300, ge=30, le=86400)
    sd2api_temp_mail_base_url: str = ""
    sd2api_temp_mail_api_key: str = ""
    sd2api_temp_mail_poll_seconds: float = Field(default=3.0, ge=1, le=30)
    sd2api_temp_mail_timeout: int = Field(default=180, ge=30, le=900)
    sd2api_upload_dir: str = "uploads"
    sd2api_upload_max_bytes: int = Field(
        default=200 * 1024 * 1024,
        ge=1024,
        le=500 * 1024 * 1024,
    )
    sd2api_upload_image_max_bytes: int = Field(
        default=30 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )
    sd2api_upload_video_max_bytes: int = Field(
        default=200 * 1024 * 1024,
        ge=1024,
        le=500 * 1024 * 1024,
    )
    sd2api_upload_audio_max_bytes: int = Field(
        default=15 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )
    sd2api_upload_max_pixels: int = Field(default=40_000_000, ge=1_000_000, le=200_000_000)

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
        for name in (
            "creative_csrf_token",
            "creative-csrf-token",
            "x-creative-csrf-token",
        ):
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
        missing = []
        if not self.tiktok_cookie.strip():
            missing.append("TIKTOK_COOKIE")
        if missing:
            raise RuntimeError("Missing TikTok configuration: " + ", ".join(missing))


settings = Settings()

from __future__ import annotations

import asyncio
import time
import base64
import json
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
import wave

import httpx
import pytest
from curl_cffi.requests import AsyncSession as CurlAsyncSession
from fastapi.testclient import TestClient
from PIL import Image

from sd2api.config import Settings
from sd2api.browser_client import BrowserTikTokClient, IMAGE_STUDIO_URL
from sd2api.browser_pool import BrowserPoolClient
from sd2api.models import OpenAICreateVideoRequest, SeedanceCreateRequest, UpstreamTask
from sd2api.protocol import ProtocolSession, ProtocolTikTokClient, _sign_gateway_request
from sd2api.security import CredentialError, CredentialVault
from sd2api.store import TaskStore
from sd2api.temp_mail import TempMailClient
from sd2api.tiktok import TikTokClient, TikTokUpstreamError
from sd2api.uploads import StagedMedia, UploadManager


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 24), (40, 100, 220)).save(output, format="PNG")
    return output.getvalue()


def wav_bytes() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x00" * 32_000)
    return output.getvalue()


def mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


class _VisibleLocator:
    def __init__(self, visible: bool) -> None:
        self.visible = visible

    async def count(self) -> int:
        return int(self.visible)

    def nth(self, index: int) -> "_VisibleLocator":
        return self

    async def is_visible(self) -> bool:
        return self.visible


class _LoginStatePage:
    def __init__(
        self,
        url: str,
        *,
        login_visible: bool = False,
        logout_visible: bool = False,
        generation_visible: bool = False,
    ) -> None:
        self.url = url
        self.login_visible = login_visible
        self.logout_visible = logout_visible
        self.generation_visible = generation_visible

    def get_by_role(self, role: str, *, name) -> _VisibleLocator:
        assert role == "button"
        return _VisibleLocator(self.login_visible)

    def locator(self, selector: str) -> _VisibleLocator:
        return _VisibleLocator(False)

    def get_by_text(self, name, *, exact: bool) -> _VisibleLocator:
        return _VisibleLocator(
            self.logout_visible if exact else self.generation_visible
        )


@pytest.mark.asyncio
async def test_public_studio_landing_page_is_not_logged_in() -> None:
    page = _LoginStatePage(
        "https://ads.tiktok.com/creative/creativestudio/home/en",
        login_visible=True,
    )
    assert await BrowserTikTokClient._is_logged_in(page) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_loading_generation_route_is_not_logged_in() -> None:
    page = _LoginStatePage(
        "https://ads.tiktok.com/creative/creativestudio/image-to-video"
    )
    assert await BrowserTikTokClient._is_logged_in(page) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_generation_workspace_is_logged_in() -> None:
    page = _LoginStatePage(
        "https://ads.tiktok.com/creative/creativestudio/image-to-video",
        generation_visible=True,
    )
    assert await BrowserTikTokClient._is_logged_in(page) is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_new_create_workspace_route_is_logged_in() -> None:
    page = _LoginStatePage(
        "https://ads.tiktok.com/creative/creativestudio/create",
        generation_visible=True,
    )
    assert await BrowserTikTokClient._is_logged_in(page) is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_chat_workspace_route_is_logged_in() -> None:
    page = _LoginStatePage(
        "https://ads.tiktok.com/creative/creativestudio/chat",
        generation_visible=True,
    )
    assert await BrowserTikTokClient._is_logged_in(page) is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_session_cookie_is_a_login_candidate_outside_login_page(
    tmp_path: Path,
) -> None:
    class Context:
        async def cookies(self, urls):
            assert urls == ["https://ads.tiktok.com"]
            return [{"name": "sessionid_ads", "value": "captured-session"}]

    client = BrowserTikTokClient(
        Settings(sd2api_browser_profile=str(tmp_path / "profile")),
        account_id="cookie-login",
    )
    client._context = Context()  # type: ignore[assignment]

    chat = _LoginStatePage("https://ads.tiktok.com/creative/creativestudio/chat")
    login = _LoginStatePage("https://ads.tiktok.com/i18n/login")

    assert await client._has_browser_login_candidate(chat) is True  # type: ignore[arg-type]
    assert await client._has_browser_login_candidate(login) is False  # type: ignore[arg-type]


def test_store_round_trip(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    created = store.create(
        task_id="task-1",
        api="openai",
        model="sora-2",
        prompt="A red ball",
        seconds=5,
    )
    assert created.status == "queued"
    updated = store.update("task-1", status="succeeded", progress=100, video_url="https://cdn/video.mp4")
    assert updated.completed_at is not None
    assert store.get("task-1") == updated
    assert store.list()[0].id == "task-1"
    assert store.list(status="succeeded")[0].id == "task-1"
    assert store.list(search="red ball")[0].id == "task-1"
    assert store.list(limit=1, offset=1) == []
    assert store.count_tasks() == 1
    assert store.count_tasks(status="succeeded", search="red ball") == 1
    assert store.task_counts() == {
        "total": 1,
        "queued": 0,
        "running": 0,
        "succeeded": 1,
        "failed": 0,
    }

    event = store.add_event(
        level="success",
        category="video",
        message="视频生成完成",
        task_id="task-1",
        details={"model": "seedance-2.0"},
    )
    assert event.id > 0
    assert event.details == {"model": "seedance-2.0"}
    assert store.list_events(level="success")[0].task_id == "task-1"
    assert store.list_events(category="video", search="seedance")[0].id == event.id
    assert store.list_events(limit=1, offset=1) == []
    assert store.count_events(level="success", search="seedance") == 1
    analytics_rows = store.duration_analytics_rows(
        since=created.created_at - 1,
        until=updated.completed_at + 1,
    )
    assert analytics_rows[0]["status"] == "succeeded"
    assert analytics_rows[0]["completed_at"] == updated.completed_at

    account = store.create_account(account_id="account-a", name="Account A")
    assert account["enabled"] is True
    assert store.update_account("account-a", enabled=False)["enabled"] is False
    assert store.list_accounts()[0]["id"] == "account-a"


def test_credential_vault_and_store_do_not_expose_password(tmp_path: Path) -> None:
    vault = CredentialVault("a-long-admin-key-for-tests")
    encrypted = vault.encrypt("correct horse battery staple")
    assert encrypted != "correct horse battery staple"
    assert vault.decrypt(encrypted) == "correct horse battery staple"
    with pytest.raises(CredentialError):
        CredentialVault("a-different-long-test-key").decrypt(encrypted)

    store = TaskStore(str(tmp_path / "credentials.db"))
    account = store.create_account(
        account_id="secure",
        name="Secure account",
        username="user@example.com",
        password_ciphertext=encrypted,
        email_address="codes@example.com",
    )
    assert account["credentials_configured"] is True
    assert "password_ciphertext" not in account
    assert store.account_credentials("secure") == {
        "username": "user@example.com",
        "password_ciphertext": encrypted,
        "email_address": "codes@example.com",
    }


def test_store_tracks_subaccounts_and_preserves_user_selection(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "subaccounts.db"))
    store.create_account(account_id="login-a", name="Login A")
    store.upsert_subaccounts(
        "login-a",
        [
            {
                "advertiser_id": "7668593711596617729",
                "name": "symphony-0731-3",
                "account_type": "partner",
                "seedance_access": True,
                "credits": 2000,
                "active": True,
            },
            {
                "advertiser_id": "7668491362075033608",
                "name": "Namcoi LLC_a5mq2y",
                "account_type": "client",
                "seedance_access": False,
                "credits": 2000,
            },
        ],
    )
    selected = store.set_subaccount_enabled(
        "login-a", "7668593711596617729", True
    )
    assert selected["enabled"] is True
    assert selected["seedance_access"] is True

    store.upsert_subaccounts(
        "login-a",
        [
            {
                "advertiser_id": "7668593711596617729",
                "name": "Updated partner name",
                "account_type": "partner",
                "seedance_access": True,
                "credits": 1995,
            }
        ],
    )
    refreshed = store.list_subaccounts("login-a")
    partner = next(
        item for item in refreshed if item["advertiser_id"] == "7668593711596617729"
    )
    assert partner["enabled"] is True
    assert partner["credits"] == 1995


def test_browser_profile_is_reused_by_login_email(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    store = TaskStore(str(tmp_path / "profiles.db"))
    store.create_account(
        account_id="old-account",
        name="Old",
        username="Login@Example.com",
    )
    legacy = root / "old-account"
    legacy.mkdir(parents=True)
    pool = BrowserPoolClient(
        Settings(sd2api_browser_profile=str(root)),
        store,
    )
    first = Path(pool._profile_path("old-account"))
    assert first.name.startswith("user_")
    assert first.exists()
    assert not legacy.exists()

    store.delete_account("old-account")
    store.create_account(
        account_id="new-account",
        name="New",
        username="login@example.com",
    )
    assert Path(pool._profile_path("new-account")) == first


@pytest.mark.asyncio
async def test_temp_mail_client_reads_tiktok_verification_code() -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "mail-1",
                        "sender": "TikTok Business <notice@tiktok.com>",
                        "subject": "Your verification code",
                        "receivedAt": datetime.now(timezone.utc).isoformat(),
                        "verificationCode": "654321",
                    }
                ]
            },
        )

    client = TempMailClient(
        base_url="https://mail.example",
        api_key="mail-secret",
        poll_seconds=0.01,
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    code = await client.wait_for_code(to_address="login@example.com", since=0)
    assert code == "654321"
    assert requested[0].url.params["to_address"] == "login@example.com"
    assert requested[0].headers["authorization"] == "Bearer mail-secret"


def test_temp_mail_client_extracts_code_from_chinese_message() -> None:
    code = TempMailClient._extract_code(
        {
            "subject": "TikTok 登录验证",
            "textBody": "你的验证码是 482731，请勿向他人透露。",
        }
    )
    assert code == "482731"


def test_temp_mail_client_extracts_alphanumeric_tiktok_html_code() -> None:
    code = TempMailClient._extract_code(
        {
            "subject": "TikTok for Business login verification",
            "htmlBody": """
                <p>Enter this code on the login verification page.</p>
                <strong>SPNRZX</strong>
                <p>Your verification code is valid for 10 minutes.</p>
            """,
        }
    )
    assert code == "SPNRZX"


def test_temp_mail_client_extracts_code_from_tiktok_table_cell() -> None:
    code = TempMailClient._extract_code(
        {
            "subject": "TikTok for Business Verification help",
            "htmlBody": (
                "<table><tr><td> TikTok verification </td></tr>"
                "<tr><td> Your code: ABC12Z </td></tr></table>"
            ),
        }
    )
    assert code == "ABC12Z"


@pytest.mark.asyncio
async def test_multi_box_otp_uses_key_event_for_final_character() -> None:
    class Field:
        def __init__(self) -> None:
            self.events: list[tuple[str, str | None]] = []

        async def fill(self, value: str) -> None:
            self.events.append(("fill", value))

        async def click(self) -> None:
            self.events.append(("click", None))

        async def press(self, value: str) -> None:
            self.events.append(("press", value))

    fields = [Field() for _ in range(6)]
    await BrowserTikTokClient._fill_code_inputs(fields, "ABC12Z")
    assert fields[0].events == [("fill", "A")]
    assert fields[-1].events == [
        ("fill", ""),
        ("click", None),
        ("press", "Z"),
    ]


@pytest.mark.asyncio
async def test_login_detects_manual_success_while_mail_polling(tmp_path: Path) -> None:
    class FakePage:
        url = "https://ads.tiktok.com/login"
        frames: list = []

        @staticmethod
        def is_closed() -> bool:
            return False

        @staticmethod
        async def wait_for_timeout(value: int) -> None:
            pass

    class FakeInput:
        async def fill(self, value: str) -> None:
            pass

    class PendingMail:
        configured = True
        cancelled = False

        async def wait_for_code(self, **kwargs) -> str:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
            return "NEVER"

    class ManualLoginClient(BrowserTikTokClient):
        def __init__(self) -> None:
            super().__init__(
                Settings(sd2api_browser_profile=str(tmp_path / "profile")),
                account_id="manual-login",
            )
            self._page = FakePage()  # type: ignore[assignment]
            self._context = object()  # type: ignore[assignment]
            self.login_checks = 0

        async def start(self) -> dict:
            return {}

        async def status(self) -> dict:
            return {"logged_in": True, "login_state": self._login_state}

        async def _is_logged_in(self, page) -> bool:
            self.login_checks += 1
            return self.login_checks >= 4

        async def _open_password_login(self, page) -> None:
            pass

        async def _wait_for_visible(self, page, selectors, *, timeout):
            return FakeInput()

        async def _first_visible(self, page, selectors):
            return FakeInput()

        async def _click_login_action(self, page, pattern) -> None:
            pass

        async def _login_error_text(self, page) -> str | None:
            return None

        async def _captcha_visible(self, page) -> bool:
            return False

        async def _visible_code_inputs(self, page) -> list:
            return [FakeInput()]

    mail = PendingMail()
    client = ManualLoginClient()
    result = await client.login(
        username="login@example.com",
        password="secret",
        email_address="login@example.com",
        mail_client=mail,  # type: ignore[arg-type]
    )
    assert result["logged_in"] is True
    assert client._login_state == "logged_in"
    assert mail.cancelled is True


@pytest.mark.asyncio
async def test_pool_validates_protocol_session_after_browser_login(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "validated-login.db"))
    pool = BrowserPoolClient(
        Settings(
            sd2api_browser_profile=str(tmp_path / "profiles"),
            sd2api_admin_key="test-admin-key-at-least-16-characters",
        ),
        store,
    )
    account = await pool.add_account(
        account_id=None,
        name=None,
        username="login@example.com",
        password="secret-password",
        start=False,
    )

    class Worker:
        stopped = False

        async def login(self, **kwargs):
            return {"login_state": "logged_in"}

        async def stop(self):
            self.stopped = True

    worker = Worker()
    pool._worker = lambda _account_id: worker  # type: ignore[method-assign]
    validation_flags: list[bool] = []

    async def capture(account_id, browser, *, validate=False, **kwargs):
        assert account_id == account["id"]
        assert browser is worker
        validation_flags.append(validate)
        return protocol_session()

    async def refresh(account_id, *, check_access=True):
        assert account_id == account["id"]
        assert check_access is True
        return {}

    pool._capture_protocol_session = capture  # type: ignore[method-assign]
    pool.refresh_subaccounts = refresh  # type: ignore[method-assign]

    await pool._run_login(account["id"])

    assert validation_flags == [True]
    assert worker.stopped is True


@pytest.mark.asyncio
async def test_start_reopens_a_closed_browser_page(tmp_path: Path) -> None:
    class ClosedPage:
        @staticmethod
        def is_closed() -> bool:
            return True

    class OpenPage:
        url = "https://ads.tiktok.com/creative/creativestudio/image-to-video"
        default_timeout = 0

        @staticmethod
        def is_closed() -> bool:
            return False

        def set_default_timeout(self, value: int) -> None:
            self.default_timeout = value

    class OpenContext:
        pages: list = []
        closed = False

        async def new_page(self):
            page = OpenPage()
            self.pages = [page]
            return page

        async def close(self) -> None:
            self.closed = True

    class FakePlaywright:
        async def stop(self) -> None:
            pass

    class RecoveryClient(BrowserTikTokClient):
        async def status(self) -> dict:
            return {"running": True}

    client = RecoveryClient(
        Settings(sd2api_browser_profile=str(tmp_path / "profile")),
        account_id="recovery",
    )
    context = OpenContext()
    client._context = context  # type: ignore[assignment]
    client._page = ClosedPage()  # type: ignore[assignment]
    client._playwright = FakePlaywright()  # type: ignore[assignment]

    result = await client.start()
    assert result["running"] is True
    assert isinstance(client._page, OpenPage)
    assert client._page.default_timeout == 30_000
    await client.stop()


@pytest.mark.parametrize(
    "account_info_url",
    [
        "https://ads.tiktok.com/passport/web/account/info/",
        "https://ads.tiktok.com/CreativeOne/Client/ClientGetAccountInfo"
        "?aid=585599&app_name=creative_aio_client&device_platform=web",
    ],
)
@pytest.mark.asyncio
async def test_browser_session_keepalive_uses_exact_image_studio_url(
    tmp_path: Path, account_info_url: str
) -> None:
    class Response:
        url = account_info_url
        status = 200

    class ResponseInfo:
        async def value(self):
            return Response()

    class ExpectResponse:
        def __init__(self, predicate) -> None:
            assert predicate(Response()) is True
            unrelated = Response()
            unrelated.url = "https://ads.tiktok.com/CreativeOne/Client/ClientGetAccountList"
            assert predicate(unrelated) is False

        async def __aenter__(self):
            info = ResponseInfo()
            info.value = info.value()
            return info

        async def __aexit__(self, *_args) -> None:
            pass

    class Context:
        calls = 0

        async def cookies(self, urls):
            assert urls == ["https://ads.tiktok.com"]
            self.calls += 1
            return [{
                "name": "sessionid_ads",
                "value": f"session-{self.calls}",
                "expires": 100 + self.calls,
            }]

    class Page:
        url = "about:blank"
        navigations: list[str] = []

        @staticmethod
        def is_closed() -> bool:
            return False

        def expect_response(self, predicate, timeout):
            assert timeout == 90_000
            return ExpectResponse(predicate)

        async def goto(self, url, **kwargs):
            self.navigations.append(url)
            self.url = url

        async def wait_for_timeout(self, value):
            assert value == 1500

    class KeepaliveClient(BrowserTikTokClient):
        async def start(self, *, target_url="unused") -> dict:
            assert target_url is None
            return {}

        async def _is_logged_in(self, page) -> bool:
            return True

    client = KeepaliveClient(
        Settings(sd2api_browser_profile=str(tmp_path / "keepalive")),
        account_id="keepalive",
    )
    client._page = Page()  # type: ignore[assignment]
    client._context = Context()  # type: ignore[assignment]

    result = await client.renew_protocol_session()

    assert client._page.navigations == [IMAGE_STUDIO_URL]
    assert result["account_info_status"] == 200
    assert result["rotated_cookie_names"] == ["sessionid_ads"]


@pytest.mark.asyncio
async def test_credits_fallback_reads_new_navigation_text() -> None:
    class Locator:
        def __init__(self, values: list[str]) -> None:
            self.values = values
            self.index = 0

        async def count(self) -> int:
            return len(self.values)

        def nth(self, index: int):
            item = Locator(self.values)
            item.index = index
            return item

        async def is_visible(self) -> bool:
            return True

        async def inner_text(self) -> str:
            return self.values[self.index]

    class Page:
        @staticmethod
        def get_by_role(role, name):
            return Locator([])

        @staticmethod
        def locator(selector):
            assert selector == "nav:visible, header:visible"
            return Locator(["English\n1975\nsymphony-0731-3"])

    assert await BrowserTikTokClient._visible_credits(Page()) == 1975


@pytest.mark.asyncio
async def test_subaccount_scan_uses_control_apis_without_ui_navigation(
    tmp_path: Path,
) -> None:
    partner_id = "7668593711596617729"
    client_id = "7668491362075033608"

    class Page:
        url = "https://ads.tiktok.com/creative/creativestudio/create"
        current_id = partner_id
        selected: list[str] = []

        @staticmethod
        def is_closed() -> bool:
            return False

        async def evaluate(self, script, payload) -> None:
            self.current_id = payload["value"]
            self.selected.append(self.current_id)

    class ApiScanClient(BrowserTikTokClient):
        async def start(self) -> dict:
            return {}

        async def _is_logged_in(self, page) -> bool:
            return True

        async def _api_json(self, method, path, *, data=None):
            current_id = self._require_page().current_id
            if path.endswith("ClientGetAccountList"):
                return {
                    "accounts": [
                        {
                            "aioClientID": partner_id,
                            "profileName": "Seedance partner",
                            "accountType": 3,
                        },
                        {
                            "aioClientID": client_id,
                            "profileName": "Client account",
                            "accountType": 1,
                        },
                    ]
                }
            if path.endswith("ClientGetAccountInfo"):
                return {"account": {"aioClientID": current_id}}
            if path.endswith("QueryCreditAccount"):
                return {"credits": "1975" if current_id == partner_id else "2000"}
            if path.endswith("QueryUserLevelDetail"):
                return {
                    "user_level_info": {
                        "user_segment_tier": (
                            "T1" if current_id == partner_id else "T4"
                        )
                    }
                }
            if path.endswith("get_miniapp_permission_with_allowlist"):
                return {
                    "data": {
                        "allowlist": [
                            {
                                "tool": "cue_mini_i2v_seedance",
                                "auth": ["visit", "entry"],
                            }
                        ]
                    }
                }
            raise AssertionError(path)

    page = Page()
    client = ApiScanClient(
        Settings(sd2api_browser_profile=str(tmp_path / "api-scan")),
        account_id="api-scan",
    )
    client._page = page  # type: ignore[assignment]
    client._context = object()  # type: ignore[assignment]

    result = await client.scan_subaccounts(check_access=True)
    assert page.selected == [partner_id, client_id, partner_id]
    assert result == [
        {
            "advertiser_id": partner_id,
            "name": "Seedance partner",
            "account_type": "partner",
            "active": True,
            "credits": 1975,
            "seedance_access": True,
            "last_error": None,
            "last_checked_at": result[0]["last_checked_at"],
        },
        {
            "advertiser_id": client_id,
            "name": "Client account",
            "account_type": "client",
            "active": False,
            "credits": 2000,
            "seedance_access": False,
            "last_error": None,
            "last_checked_at": result[1]["last_checked_at"],
        },
    ]


@pytest.mark.asyncio
async def test_terms_are_automatically_accepted_when_present(
    tmp_path: Path,
) -> None:
    class Page:
        def __init__(self) -> None:
            self.terms_visible = True
            self.accept_visible = True
            self.accept_enabled = False
            self.accept_clicked = False

        def locator(self, selector: str):
            assert selector == '[class*="ai-disclaimer"]:visible'
            return Locator(self, "terms")

        def get_by_text(self, text):
            return Locator(self, "terms")

        def get_by_role(self, role, name):
            assert role == "button"
            return Locator(self, "accept")

        async def wait_for_timeout(self, value: int) -> None:
            assert value == 500

    class Locator:
        def __init__(self, page: Page, kind: str) -> None:
            self.page = page
            self.kind = kind

        async def count(self) -> int:
            return 1

        def nth(self, index: int):
            assert index == 0
            return self

        async def is_visible(self) -> bool:
            return (
                self.page.terms_visible
                if self.kind == "terms"
                else self.page.accept_visible
            )

        async def evaluate(self, script) -> None:
            assert self.kind == "terms"
            self.page.accept_enabled = True

        async def is_enabled(self) -> bool:
            return self.page.accept_enabled

        async def click(self) -> None:
            self.page.accept_clicked = True
            self.page.accept_visible = False

        async def wait_for(self, *, state: str, timeout: int) -> None:
            assert state == "hidden"
            assert timeout == 10_000
            assert not self.page.accept_visible

    page = Page()
    client = BrowserTikTokClient(
        Settings(
            sd2api_browser_profile=str(tmp_path / "terms-auto"),
        ),
        account_id="terms-auto",
    )
    await client._ensure_terms_accepted(page)  # type: ignore[arg-type]
    assert page.accept_clicked is True

    page.terms_visible = False
    page.accept_visible = False
    await client._ensure_terms_accepted(page)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_create_and_check_success() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/create_generate_task"):
            body = json.loads(request.content)
            assert body["model"] == "5000005"
            assert body["duration"] == 5
            return httpx.Response(200, json={"code": 0, "data": {"taskId": "task-123"}})
        if request.url.path.endswith("/generate-task/check"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "draft_infos": [
                            {
                                "taskId": "task-123",
                                "draftTaskStatus": 0,
                                "renderTaskStatus": 0,
                                "vid": "v123",
                                "videoInfo": {
                                    "VideoInfos": [{"MainUrl": "https://cdn.example/video.mp4"}],
                                    "PosterUrl": "https://cdn.example/poster.jpg",
                                },
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    config = Settings(
        tiktok_cookie="sessionid=test; csrftoken=csrf; MONITOR_DEVICE_ID=device"
    )
    client = TikTokClient(config, transport=httpx.MockTransport(handler))
    task_id = await client.create_text_video(prompt="A red ball", model="sora-2", duration=5)
    result = await client.check_task(task_id)

    assert task_id == "task-123"
    assert result.status == "succeeded"
    assert result.video_url == "https://cdn.example/video.mp4"
    assert requests[0].headers["x-csrftoken"] == "csrf"
    assert requests[0].url.params["device_id"] == "device"


@pytest.mark.asyncio
async def test_direct_client_preserves_model_permission_denied_as_403() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"code": 10001100, "message": "没有模型使用权限"},
        )

    client = TikTokClient(
        Settings(tiktok_cookie="sessionid=test"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TikTokUpstreamError) as error:
        await client.create_text_video(
            prompt="A red ball", model="seedance-2.0", duration=5
        )
    assert error.value.status_code == 403
    assert error.value.code == "10001100"
    assert str(error.value) == "没有模型使用权限"


@pytest.mark.asyncio
async def test_check_processing_and_failure() -> None:
    responses = iter(
        [
            {"code": 0, "data": {"draft_infos": [{"draftTaskStatus": 1, "renderTaskStatus": 1}]}},
            {
                "code": 0,
                "data": {
                    "draft_infos": [
                        {
                            "draftTaskStatus": 2,
                            "renderTaskStatus": 1,
                            "generateErrorCode": 10001202,
                            "generateErrorMessage": "generation failed",
                        }
                    ]
                },
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    config = Settings(tiktok_cookie="sessionid=test; MONITOR_DEVICE_ID=device")
    client = TikTokClient(config, transport=httpx.MockTransport(handler))
    processing = await client.check_task("task-1")
    failed = await client.check_task("task-1")
    assert processing.status == "running"
    assert failed.status == "failed"
    assert failed.error_code == "10001202"


def test_openai_route_accepts_json_and_multipart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sd2api.main as main

    class FakeClient:
        calls: list[dict[str, object]] = []

        async def create_text_video(self, **kwargs: object) -> str:
            self.calls.append(kwargs)
            return f"task-{len(self.calls)}"

    fake = FakeClient()
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setattr(main, "store", TaskStore(str(tmp_path / "api.db")))
    api = TestClient(main.app)
    headers = {"Authorization": f"Bearer {main.settings.sd2api_api_key}"}

    json_response = api.post(
        "/v1/videos",
        headers=headers,
        json={
            "model": "sora-2",
            "prompt": "A red ball",
            "seconds": 5,
            "size": "720x1280",
        },
    )
    multipart_response = api.post(
        "/v1/videos",
        headers=headers,
        data={
            "model": "sora-2",
            "prompt": "A blue ball",
            "seconds": "5",
            "size": "1280x720",
        },
    )

    assert json_response.status_code == 200
    assert multipart_response.status_code == 200
    assert json_response.json()["status"] == "queued"
    assert multipart_response.json()["size"] == "1280x720"
    assert fake.calls == [
        {"prompt": "A red ball", "model": "sora-2", "duration": 5},
        {"prompt": "A blue ball", "model": "sora-2", "duration": 5},
    ]


def test_openai_multipart_image_routes_to_image_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sd2api.main as main

    class FakeBrowserClient:
        calls: list[dict[str, object]] = []
        reference_calls: list[dict[str, object]] = []

        async def create_image_video(self, **kwargs: object) -> str:
            self.calls.append(kwargs)
            return f"image-task-{len(self.calls)}"

        async def create_reference_video(self, **kwargs: object) -> str:
            self.reference_calls.append(kwargs)
            return f"reference-task-{len(self.reference_calls)}"

    fake = FakeBrowserClient()
    monkeypatch.setattr(main, "BrowserTikTokClient", FakeBrowserClient)
    monkeypatch.setattr(main, "client", fake)
    monkeypatch.setattr(main, "store", TaskStore(str(tmp_path / "image-api.db")))
    monkeypatch.setattr(
        main,
        "uploads",
        UploadManager(Settings(sd2api_upload_dir=str(tmp_path / "uploads"))),
    )
    api = TestClient(main.app)
    headers = {"Authorization": f"Bearer {main.settings.sd2api_api_key}"}

    response = api.post(
        "/v1/videos",
        headers=headers,
        data={
            "model": "sora-2",
            "prompt": "The blue cube slowly rotates",
            "seconds": "5",
            "size": "720x1280",
        },
        files={"input_reference": ("cube.png", png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "image-task-1"
    assert len(fake.calls) == 1
    assert Path(str(fake.calls[0]["image_path"])).is_file()
    assert fake.calls[0]["duration"] == 5

    data_url = "data:image/png;base64," + base64.b64encode(png_bytes()).decode("ascii")
    seedance_response = api.post(
        "/api/v3/contents/generations/tasks",
        headers=headers,
        json={
            "model": "seedance-2.0",
            "content": [
                {"type": "text", "text": "The blue cube rotates"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
            "duration": 5,
        },
    )
    assert seedance_response.status_code == 200
    assert seedance_response.json()["id"] == "image-task-2"
    assert Path(str(fake.calls[1]["image_path"])).is_file()

    audio_data_url = "data:audio/wav;base64," + base64.b64encode(wav_bytes()).decode("ascii")
    seedance_reference_response = api.post(
        "/api/v3/contents/generations/tasks",
        headers=headers,
        json={
            "model": "seedance-2.0",
            "content": [
                {"type": "text", "text": "Use Image 1 and the supplied audio"},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                    "role": "reference_image",
                },
                {
                    "type": "audio_url",
                    "audio_url": {"url": audio_data_url},
                    "role": "reference_audio",
                },
            ],
            "duration": 5,
        },
    )
    assert seedance_reference_response.status_code == 200
    assert seedance_reference_response.json()["id"] == "reference-task-1"
    assert [item.kind for item in fake.reference_calls[0]["media"]] == ["image", "audio"]

    audio_only_response = api.post(
        "/api/v3/contents/generations/tasks",
        headers=headers,
        json={
            "model": "seedance-2.0",
            "content": [
                {"type": "text", "text": "Audio alone is not a visual reference"},
                {
                    "type": "audio_url",
                    "audio_url": {"url": audio_data_url},
                    "role": "reference_audio",
                },
            ],
            "duration": 5,
        },
    )
    assert audio_only_response.status_code == 422

    openai_reference_response = api.post(
        "/v1/videos",
        headers=headers,
        data={
            "model": "sora-2",
            "prompt": "Use all supplied references",
            "seconds": "5",
            "size": "720x1280",
        },
        files=[
            ("reference_media", ("cube.png", png_bytes(), "image/png")),
            ("reference_media", ("motion.mp4", mp4_bytes(), "video/mp4")),
            ("reference_media", ("tone.wav", wav_bytes(), "audio/wav")),
        ],
    )
    assert openai_reference_response.status_code == 200
    assert openai_reference_response.json()["id"] == "reference-task-2"
    assert [item.kind for item in fake.reference_calls[1]["media"]] == [
        "image",
        "video",
        "audio",
    ]


@pytest.mark.asyncio
async def test_upload_manager_rejects_private_image_url(tmp_path: Path) -> None:
    manager = UploadManager(Settings(sd2api_upload_dir=str(tmp_path / "uploads")))
    with pytest.raises(TikTokUpstreamError) as error:
        await manager.save_url("http://127.0.0.1/private.png")
    assert error.value.code == "image_url_blocked"


def protocol_session() -> ProtocolSession:
    return ProtocolSession.from_dict(
        {
            "version": 1,
            "captured_at": 1,
            "device_id": "device-123",
            "fp_id": "0123456789abcdef0123456789abcdef",
            "sec_ch_ua": '"Chromium";v="151", "Not=A?Brand";v="99"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
            "user_agent": "Protocol Test Browser",
            "cookies": [
                {
                    "name": "sessionid",
                    "value": "session-value",
                    "domain": ".tiktok.com",
                    "path": "/",
                },
                {
                    "name": "csrftoken",
                    "value": "csrf-value",
                    "domain": ".tiktok.com",
                    "path": "/",
                },
                {
                    "name": "s_aio_client_id",
                    "value": "old-advertiser",
                    "domain": ".tiktok.com",
                    "path": "/",
                },
            ],
        }
    )


@pytest.mark.asyncio
async def test_protocol_real_client_uses_chrome_impersonation_backend() -> None:
    client = ProtocolTikTokClient(
        Settings(), protocol_session(), account_id="account-a"
    )
    raw = await client._get_client()
    assert isinstance(raw, CurlAsyncSession)
    assert raw.impersonate == "chrome"
    await client.close()


@pytest.mark.asyncio
async def test_protocol_client_uses_isolated_subaccount_cookie_and_login_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"BaseResp": {"StatusCode": 0}, "account": {"aioClientID": "123456789012"}},
        )

    client = ProtocolTikTokClient(
        Settings(),
        protocol_session(),
        account_id="account-a",
        advertiser_id="123456789012",
        transport=httpx.MockTransport(handler),
    )
    await client.validate()
    await client.close()

    request = requests[0]
    assert "device_id" not in request.url.params
    assert request.headers["x-csrftoken"] == "csrf-value"
    assert request.headers["x-fp-id"] == "0123456789abcdef0123456789abcdef"
    assert request.headers["sec-ch-ua"] == '"Chromium";v="151", "Not=A?Brand";v="99"'
    assert request.headers["sec-ch-ua-mobile"] == "?0"
    assert request.headers["sec-ch-ua-platform"] == '"Windows"'
    assert request.headers["user-agent"] == "Protocol Test Browser"
    assert request.headers["x-creative-source"] == "CreativeStudio/MiniApp/ImageToVideo"
    assert "s_aio_client_id=123456789012" in request.headers["cookie"]
    assert "old-advertiser" not in request.headers["cookie"]


@pytest.mark.asyncio
async def test_protocol_http_error_preserves_upstream_code_and_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "BaseResp": {
                    "StatusCode": "daily-quota-code",
                    "StatusMessage": "Daily generation limit reached",
                }
            },
        )

    client = ProtocolTikTokClient(
        Settings(),
        protocol_session(),
        account_id="account-a",
        advertiser_id="123456789012",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TikTokUpstreamError) as error:
        await client.create_text_video(
            prompt="quota", model="seedance-2.0", duration=5
        )
    await client.close()
    assert error.value.status_code == 429
    assert error.value.code == "daily-quota-code"
    assert str(error.value) == "Daily generation limit reached"


@pytest.mark.asyncio
async def test_protocol_invalid_login_business_error_is_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "BaseResp": {
                    "StatusCode": "38001001",
                    "StatusMessage": "InvalidLogin",
                }
            },
        )

    client = ProtocolTikTokClient(
        Settings(),
        protocol_session(),
        account_id="account-a",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TikTokUpstreamError) as error:
        await client.validate()
    await client.close()

    assert error.value.status_code == 401
    assert error.value.code == "tiktok_authentication_error"
    assert str(error.value) == (
        "TikTok session expired; the account must log in again"
    )


@pytest.mark.asyncio
async def test_refresh_subaccounts_clears_session_on_invalid_login(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "refresh-invalid-login.db"))
    store.create_account(account_id="expired", name="Expired")
    store.update_account(
        "expired",
        session_ciphertext="stored-session",
        session_updated_at=int(time.time()),
        login_state="logged_in",
    )
    pool = BrowserPoolClient(Settings(), store)

    class InvalidLoginClient:
        load = 0

        async def discover_subaccounts(self) -> list[dict[str, Any]]:
            raise TikTokUpstreamError(
                "InvalidLogin",
                status_code=502,
                code="38001001",
            )

    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, advertiser_id=None: InvalidLoginClient()
    )

    with pytest.raises(TikTokUpstreamError) as error:
        await pool.refresh_subaccounts("expired")

    assert error.value.status_code == 401
    assert error.value.code == "tiktok_authentication_error"
    account = store.get_account("expired")
    assert account is not None
    assert account["login_state"] == "pending"
    assert account["session_available"] is False
    assert account["last_error"] == (
        "TikTok session expired; the account must log in again"
    )


@pytest.mark.asyncio
async def test_protocol_status_two_is_running_until_zero_with_vid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "draft_infos": [
                        {
                            "taskId": "task-running",
                            "draftTaskStatus": 2,
                            "renderTaskStatus": 2,
                            "vid": "",
                            "generateErrorCode": None,
                            "generateErrorMessage": None,
                        }
                    ]
                },
            },
        )

    client = ProtocolTikTokClient(
        Settings(),
        protocol_session(),
        account_id="account-a",
        advertiser_id="123456789012",
        transport=httpx.MockTransport(handler),
    )
    result = await client.check_task("task-running")
    await client.close()
    assert result.status == "running"
    assert result.progress == 50
    assert result.error_code is None


def test_protocol_video_url_ignores_empty_preview_and_prefers_original() -> None:
    payload = {
        "previewLink": "",
        "videoInfo": {
            "VideoInfos": [
                {"MainUrl": "https://cdn.example/360.mp4", "VideoMeta": {"Size": "10"}},
                {"MainUrl": "https://cdn.example/720.mp4", "VideoMeta": {"Size": "20"}},
            ],
            "OriginalVideoInfo": {"MainUrl": "https://cdn.example/original.mp4"},
        },
    }
    assert (
        ProtocolTikTokClient._extract_video_url(payload)
        == "https://cdn.example/original.mp4"
    )


def test_upload_gateway_sigv4_is_derived_from_sts_token() -> None:
    path, headers = _sign_gateway_request(
        method="GET",
        path="/creative/creativestudio/upload-proxy",
        params={"Action": "ApplyImageUpload", "Version": "2018-08-01"},
        token={
            "AccessKeyID": "access",
            "SecretAccessKey": "secret",
            "SessionToken": "session-token",
            "CurrentTime": "2026-08-02T00:00:00Z",
        },
        service="imagex",
    )
    assert path.endswith("Action=ApplyImageUpload&Version=2018-08-01")
    assert headers["X-Amz-Date"] == "20260802T000000Z"
    assert headers["x-amz-security-token"] == "session-token"
    assert "Credential=access/20260802/i18n/imagex/aws4_request" in headers["Authorization"]


@pytest.mark.asyncio
async def test_protocol_image_upload_apply_transfer_commit(tmp_path: Path) -> None:
    image = tmp_path / "reference.png"
    image.write_bytes(png_bytes())
    stages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cue/upload/token"):
            stages.append("token")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "AccessKeyID": "access",
                        "SecretAccessKey": "secret",
                        "SessionToken": "session-token",
                        "CurrentTime": "2026-08-02T00:00:00Z",
                    },
                },
            )
        if request.url.path.endswith("/upload-proxy") and request.method == "GET":
            stages.append("apply")
            assert request.url.params["Action"] == "ApplyImageUpload"
            assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256")
            return httpx.Response(
                200,
                json={
                    "Result": {
                        "UploadAddress": {
                            "UploadHosts": ["upload.example.com"],
                            "SessionKey": "session-key",
                            "StoreInfos": [
                                {
                                    "StoreUri": "tos-sg/image-object",
                                    "Auth": "storage-signature",
                                }
                            ],
                        }
                    }
                },
            )
        if request.url.host == "upload.example.com":
            stages.append("transfer")
            assert request.headers["authorization"] == "storage-signature"
            assert len(request.headers["content-crc32"]) == 8
            return httpx.Response(200, json={"code": 2000, "data": {"crc32": "ok"}})
        if request.url.path.endswith("/upload-proxy") and request.method == "POST":
            stages.append("commit")
            assert request.url.params["Action"] == "CommitImageUpload"
            assert json.loads(request.content)["SessionKey"] == "session-key"
            return httpx.Response(
                200,
                json={"Result": {"Results": [{"ImageUri": "tos-sg/image-object"}]}},
            )
        raise AssertionError(f"Unexpected request {request.method} {request.url}")

    client = ProtocolTikTokClient(
        Settings(),
        protocol_session(),
        account_id="account-a",
        advertiser_id="123456789012",
        transport=httpx.MockTransport(handler),
    )
    result = await client.upload_media(StagedMedia(kind="image", path=str(image)))
    await client.close()
    assert stages == ["token", "apply", "transfer", "commit"]
    assert result.image_uri == "tos-sg/image-object"
    assert result.image_url and result.image_url.startswith("https://")


@pytest.mark.asyncio
async def test_protocol_multipart_transfer_uses_isolated_parts_and_finish(
    tmp_path: Path,
) -> None:
    media = tmp_path / "large.mp4"
    media.write_bytes(b"m" * (2 * 1024 * 1024 + 123))
    phases: list[str] = []
    parts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        phase = request.url.params.get("phase")
        if phase == "init":
            phases.append("init")
            return httpx.Response(
                200, json={"code": 2000, "data": {"uploadid": "upload-1"}}
            )
        if phase == "transfer":
            phases.append("transfer")
            parts.append(int(request.url.params["part_number"]))
            assert request.headers["authorization"] == "storage-signature"
            return httpx.Response(200, json={"code": 2000})
        if phase == "finish":
            phases.append("finish")
            body = request.content.decode()
            assert [item.split(":", 1)[0] for item in body.split(",")] == [
                "1",
                "2",
                "3",
            ]
            return httpx.Response(200, json={"code": 2000, "data": {"vid": "v1"}})
        raise AssertionError(f"Unexpected request {request.method} {request.url}")

    client = ProtocolTikTokClient(
        Settings(
            sd2api_protocol_direct_upload_bytes=256 * 1024,
            sd2api_protocol_slice_bytes=1024 * 1024,
        ),
        protocol_session(),
        account_id="account-a",
        advertiser_id="123456789012",
        transport=httpx.MockTransport(handler),
    )
    result = await client._transfer_file(
        media,
        {
            "host": "upload.example.com",
            "oid": "video-object",
            "signature": "storage-signature",
            "headers": {},
        },
    )
    await client.close()
    assert phases == ["init", "transfer", "transfer", "transfer", "finish"]
    assert parts == [1, 2, 3]
    assert result["data"]["vid"] == "v1"


@pytest.mark.asyncio
async def test_protocol_reference_request_uses_image_video_audio_mentions(
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/i2v/gen_r2v_video"):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "data": {"taskId": "r2v-task"}})
        raise AssertionError(f"Unexpected request {request.method} {request.url}")

    client = ProtocolTikTokClient(
        Settings(),
        protocol_session(),
        account_id="account-a",
        advertiser_id="123456789012",
        transport=httpx.MockTransport(handler),
    )
    uploaded = iter(
        [
            type("Media", (), {"kind": "image", "image_url": "https://img/1", "vid": None, "poster_url": None})(),
            type("Media", (), {"kind": "video", "image_url": None, "vid": "video-1", "poster_url": "https://poster/1"})(),
            type("Media", (), {"kind": "audio", "image_url": None, "vid": "audio-1", "poster_url": None})(),
        ]
    )

    async def fake_upload(media: StagedMedia):
        return next(uploaded)

    client.upload_media = fake_upload  # type: ignore[method-assign]
    task_id = await client.create_reference_video(
        prompt="Use all references",
        model="seedance-2.0",
        duration=5,
        media=[
            StagedMedia(kind="image", path=str(tmp_path / "a.png")),
            StagedMedia(kind="video", path=str(tmp_path / "b.mp4")),
            StagedMedia(kind="audio", path=str(tmp_path / "c.wav")),
        ],
    )
    await client.close()
    assert task_id == "r2v-task"
    assert captured["images"] == ["https://img/1"]
    assert captured["mentions"] == [
        {"type": 1, "id": "https://img/1"},
        {"type": 2, "id": "video-1"},
        {"type": 101, "id": "audio-1"},
    ]
    assert captured["model"] == "2000004"
    assert json.loads(captured["settings"])["aiModel"] == "2000004"


@pytest.mark.asyncio
async def test_protocol_mode_specific_model_ids(tmp_path: Path) -> None:
    image = tmp_path / "reference.png"
    image.write_bytes(png_bytes())
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        return httpx.Response(200, json={"code": 0, "data": {"taskId": "task"}})

    client = ProtocolTikTokClient(
        Settings(),
        protocol_session(),
        account_id="account-a",
        advertiser_id="123456789012",
        transport=httpx.MockTransport(handler),
    )

    async def fake_upload(media: StagedMedia):
        return type(
            "Media",
            (),
            {
                "kind": media.kind,
                "image_url": "https://img/reference",
                "vid": None,
                "poster_url": None,
            },
        )()

    client.upload_media = fake_upload  # type: ignore[method-assign]
    await client.create_text_video(prompt="text", model="seedance-2.5", duration=5)
    await client.create_image_video(
        prompt="image", model="seedance-2.5", duration=5, image_path=str(image)
    )
    await client.create_reference_video(
        prompt="reference",
        model="seedance-2.5",
        duration=5,
        media=[StagedMedia(kind="image", path=str(image))],
    )
    await client.close()

    assert [body["model"] for _, body in requests] == [
        "5000007",
        "4000008",
        "2000008",
    ]
    assert [json.loads(body["settings"])["aiModel"] for _, body in requests] == [
        "5000007",
        "4000008",
        "2000008",
    ]
    display_only = {
        "ratio",
        "resolution",
        "size",
        "seed",
        "camera_fixed",
        "watermark",
        "generate_audio",
    }
    for _, body in requests:
        assert display_only.isdisjoint(body)
        assert display_only.isdisjoint(json.loads(body["settings"]))


@pytest.mark.asyncio
async def test_protocol_client_preserves_model_permission_denied_as_403() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"code": 10001100, "message": "没有模型使用权限"},
        )

    client = ProtocolTikTokClient(
        Settings(),
        protocol_session(),
        account_id="account-a",
        advertiser_id="no-access-advertiser",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TikTokUpstreamError) as error:
        await client.create_text_video(
            prompt="A red ball", model="seedance-2.0", duration=5
        )
    await client.close()
    assert error.value.status_code == 403
    assert error.value.code == "10001100"
    assert str(error.value) == "没有模型使用权限"


def test_parameter_schema_marks_display_only_fields_as_not_forwarded() -> None:
    seedance_schema = SeedanceCreateRequest.model_json_schema()["properties"]
    openai_schema = OpenAICreateVideoRequest.model_json_schema()["properties"]
    assert "not forwarded" in seedance_schema["ratio"]["description"]
    assert "not forwarded" in seedance_schema["resolution"]["description"]
    assert "not forwarded" in seedance_schema["seed"]["description"]
    assert "not forwarded" in openai_schema["size"]["description"]
    assert seedance_schema["duration"]["minimum"] == 4
    assert seedance_schema["duration"]["maximum"] == 15


def test_openapi_documents_video_parameters_and_both_openai_body_formats() -> None:
    import sd2api.main as main

    schema = main.app.openapi()
    seedance_operation = schema["paths"]["/api/v3/contents/generations/tasks"]["post"]
    assert seedance_operation["summary"] == "创建 Seedance 视频"
    assert "720 × 1280" in seedance_operation["description"]
    assert set(seedance_operation["requestBody"]["content"]["application/json"]["examples"]) == {
        "text_to_video",
        "image_to_video",
        "reference_to_video",
    }

    openai_operation = schema["paths"]["/v1/videos"]["post"]
    assert openai_operation["summary"] == "创建视频（OpenAI 兼容）"
    assert "720 × 1280" in openai_operation["description"]
    assert "403" in openai_operation["responses"]
    assert "403" in seedance_operation["responses"]
    content = openai_operation["requestBody"]["content"]
    assert set(content) == {"application/json", "multipart/form-data"}
    json_properties = content["application/json"]["schema"]["properties"]
    assert json_properties["seconds"]["minimum"] == 4
    assert json_properties["seconds"]["maximum"] == 15
    assert "不会发送给 TikTok" in json_properties["size"]["description"]
    assert set(content["application/json"]["examples"]) == {
        "text_to_video",
        "image_to_video",
        "reference_to_video",
    }
    multipart_properties = content["multipart/form-data"]["schema"]["properties"]
    assert multipart_properties["input_reference"]["format"] == "binary"
    assert multipart_properties["reference_media"]["items"]["format"] == "binary"


def test_protocol_session_derives_device_id_from_browser_cookie() -> None:
    session = ProtocolSession.from_dict(
        {
            "cookies": [
                {
                    "name": "MONITOR_WEB_ID",
                    "value": "7612345678901234567",
                    "domain": ".tiktok.com",
                    "path": "/",
                }
            ],
            "user_agent": "Browser",
        }
    )
    assert session.device_id == "7612345678901234567"


@pytest.mark.asyncio
async def test_protocol_generation_rejects_session_without_fp_id() -> None:
    session = ProtocolSession.from_dict(
        {
            "cookies": [
                {
                    "name": "sessionid",
                    "value": "session-value",
                    "domain": ".tiktok.com",
                    "path": "/",
                }
            ],
            "user_agent": "Browser",
        }
    )
    client = ProtocolTikTokClient(
        Settings(), session, account_id="account-a", advertiser_id="advertiser-a"
    )
    with pytest.raises(TikTokUpstreamError) as error:
        await client.create_text_video(
            prompt="text", model="seedance-2.0", duration=5
        )
    assert error.value.code == "protocol_fp_id_missing"


@pytest.mark.asyncio
async def test_pool_returns_403_when_online_accounts_have_no_seedance_access(
    tmp_path: Path,
) -> None:
    pool = BrowserPoolClient(Settings(), TaskStore(str(tmp_path / "no-access.db")))

    async def list_accounts() -> list[dict[str, Any]]:
        return [
            {
                "id": "account-a",
                "enabled": True,
                "running": True,
                "logged_in": True,
                "subaccounts": [
                    {
                        "advertiser_id": "advertiser-no-access",
                        "enabled": False,
                        "seedance_access": False,
                        "credits": 2000,
                    }
                ],
            }
        ]

    pool.list_accounts = list_accounts  # type: ignore[method-assign]
    with pytest.raises(TikTokUpstreamError) as error:
        await pool.create_text_video(
            prompt="A red ball", model="seedance-2.0", duration=5
        )
    assert error.value.status_code == 403
    assert error.value.code == "seedance_access_required"


@pytest.mark.asyncio
async def test_pool_keepalive_tracks_success_and_closes_browser(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "keepalive-pool.db"))
    now = 1_800_000_000
    store.create_account(account_id="due", name="due")
    store.update_account(
        "due",
        session_ciphertext="encrypted-due",
        session_updated_at=now - 21600,
        login_state="logged_in",
    )
    pool = BrowserPoolClient(
        Settings(
            sd2api_browser_profile=str(tmp_path / "profiles"),
            sd2api_session_keepalive_interval=21600,
        ),
        store,
    )
    pool._started_accounts.add("due")

    class Worker:
        load = 0
        stopped = 0

        async def renew_protocol_session(self):
            return {
                "url": IMAGE_STUDIO_URL,
                "account_info_status": 200,
                "core_cookie_count": 8,
                "rotated_cookie_names": ["sessionid_ads"],
            }

        async def stop(self):
            self.stopped += 1

    worker = Worker()
    pool._worker = lambda _account_id: worker  # type: ignore[method-assign]
    pool._protocol_session = lambda _account_id: ProtocolSession(  # type: ignore[method-assign]
        cookies=({"name": "sessionid_ads", "value": "old"},),
        device_id="device-12345678",
        user_agent="Mozilla/5.0",
        fp_id="fp-12345678",
    )

    async def capture(account_id, browser, **kwargs):
        assert account_id == "due"
        assert browser is worker
        assert kwargs == {
            "bootstrap_identity": False,
            "identity_fallback": pool._protocol_session(account_id),
            "validate": True,
        }
        store.update_account(account_id, session_updated_at=now)
        return pool._protocol_session(account_id)

    pool._capture_protocol_session = capture  # type: ignore[method-assign]

    await pool._run_session_keepalive_once(now=now)

    account = store.get_account("due")
    assert account is not None
    assert account["keepalive_state"] == "succeeded"
    assert account["keepalive_error"] is None
    assert worker.stopped == 1
    assert "due" not in pool._keepalive_accounts
    messages = [event.message for event in store.list_events(limit=10)]
    assert "Session keepalive started; Chromium profile is opening" in messages
    assert "Session keepalive completed and protocol validation passed" in messages


@pytest.mark.asyncio
async def test_pool_keepalive_failure_does_not_schedule_login(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "keepalive-failure.db"))
    store.create_account(account_id="due", name="Due")
    store.update_account(
        "due",
        session_ciphertext="encrypted-session",
        session_updated_at=1,
        login_state="logged_in",
    )
    pool = BrowserPoolClient(Settings(sd2api_session_keepalive_interval=3600), store)
    pool._started_accounts.add("due")

    class Worker:
        load = 0
        stopped = False

        async def renew_protocol_session(self):
            raise TikTokUpstreamError(
                "Profile signed out", status_code=401, code="browser_login_required"
            )

        async def stop(self):
            self.stopped = True

    worker = Worker()
    pool._worker = lambda _account_id: worker  # type: ignore[method-assign]
    pool._protocol_session = lambda _account_id: ProtocolSession(  # type: ignore[method-assign]
        cookies=({"name": "sessionid_ads", "value": "old"},),
        device_id="device-12345678",
        user_agent="Mozilla/5.0",
    )
    scheduled: list[str] = []
    pool._schedule_login = lambda account_id: scheduled.append(account_id)  # type: ignore[method-assign]

    await pool._run_session_keepalive_once(now=7201)

    account = store.get_account("due")
    assert scheduled == []
    assert worker.stopped is True
    assert account is not None and account["session_available"] is True
    assert account["keepalive_state"] == "failed"
    assert "Profile signed out" in account["keepalive_error"]


@pytest.mark.asyncio
async def test_pool_schedules_concurrent_jobs_across_accounts(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "pool.db"))
    store.create_account(account_id="a", name="Account A")
    store.create_account(account_id="b", name="Account B")
    for account_id in ("a", "b"):
        store.upsert_subaccounts(
            account_id,
            [
                {
                    "advertiser_id": f"{account_id}-advertiser",
                    "name": f"Advertiser {account_id.upper()}",
                    "account_type": "partner",
                    "seedance_access": True,
                    "credits": 100,
                }
            ],
        )
        store.set_subaccount_enabled(account_id, f"{account_id}-advertiser", True)
    config = Settings(
        sd2api_browser_profile=str(tmp_path / "profiles"),
        sd2api_pool_max_pending=10,
    )
    pool = BrowserPoolClient(config, store)

    class FakeWorker:
        def __init__(self, account_id: str) -> None:
            self.account_id = account_id
            self.load = 0
            self.counter = 0
            self.reference_calls: list[dict[str, object]] = []

        async def status(self) -> dict[str, object]:
            return {
                "account_id": self.account_id,
                "running": True,
                "logged_in": True,
                "queued": self.load,
                "busy": self.load > 0,
                "url": "https://ads.tiktok.com/creative/creativestudio/image-to-video",
            }

        async def create_text_video(self, **kwargs: object) -> str:
            self.counter += 1
            self.load += 1
            return f"{self.account_id}-task-{self.counter}"

        async def create_reference_video(self, **kwargs: object) -> str:
            self.reference_calls.append(kwargs)
            self.counter += 1
            self.load += 1
            return f"{self.account_id}-reference-{self.counter}"

        async def check_task(self, task_id: str) -> UpstreamTask:
            return UpstreamTask(id=task_id, status="running", progress=10)

    worker_a = FakeWorker("a")
    worker_b = FakeWorker("b")
    pool._workers = {"a": worker_a, "b": worker_b}  # type: ignore[assignment]

    tasks = await asyncio.gather(
        *(
            pool.create_text_video(prompt=f"job-{index}", model="sora-2", duration=5)
            for index in range(4)
        )
    )

    assert [task.split("-", 1)[0] for task in tasks] == ["a", "b", "a", "b"]
    assert [pool.account_for_task(task) for task in tasks] == ["a", "b", "a", "b"]
    assert [pool.advertiser_for_task(task) for task in tasks] == [
        "a-advertiser",
        "b-advertiser",
        "a-advertiser",
        "b-advertiser",
    ]

    reference_task = await pool.create_reference_video(
        prompt="Use the supplied image and audio",
        model="seedance-2.0",
        duration=5,
        media=[
            StagedMedia(kind="image", path=str(tmp_path / "image.png")),
            StagedMedia(kind="audio", path=str(tmp_path / "audio.wav")),
        ],
    )
    assert reference_task.startswith("a-reference-")
    assert [item.kind for item in worker_a.reference_calls[0]["media"]] == [
        "image",
        "audio",
    ]
    assert worker_a.reference_calls[0]["advertiser_id"] == "a-advertiser"


@pytest.mark.asyncio
async def test_pool_request_waits_until_only_account_finishes_keepalive(
    tmp_path: Path,
) -> None:
    pool = BrowserPoolClient(
        Settings(sd2api_database=str(tmp_path / "keepalive-wait.db")),
        TaskStore(str(tmp_path / "keepalive-wait.db")),
    )
    keepalive_active = True
    account = {
        "id": "login-a",
        "enabled": True,
        "running": True,
        "logged_in": True,
        "session_available": True,
        "subaccounts": [{
            "advertiser_id": "sub-a",
            "enabled": True,
            "seedance_access": True,
            "credits": 100,
        }],
    }

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [{**account, "keepalive_active": keepalive_active}]

    class FakeProtocol:
        load = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            self.load += 1
            return "sub-a-task"

    protocol = FakeProtocol()
    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    pool._protocol_client = lambda *_args, **_kwargs: protocol  # type: ignore[method-assign]

    request = asyncio.create_task(
        pool.create_text_video(prompt="wait", model="seedance-2.0", duration=5)
    )
    for _ in range(20):
        if pool._keepalive_waiters == 1:
            break
        await asyncio.sleep(0)

    assert pool._keepalive_waiters == 1
    assert request.done() is False

    keepalive_active = False
    await pool._notify_keepalive_finished()

    assert await asyncio.wait_for(request, timeout=1) == "sub-a-task"
    assert pool._keepalive_waiters == 0


@pytest.mark.asyncio
async def test_pool_uses_other_account_without_waiting_for_keepalive(
    tmp_path: Path,
) -> None:
    pool = BrowserPoolClient(
        Settings(sd2api_database=str(tmp_path / "keepalive-failover.db")),
        TaskStore(str(tmp_path / "keepalive-failover.db")),
    )

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [
            {
                "id": "login-a",
                "enabled": True,
                "running": True,
                "logged_in": True,
                "session_available": True,
                "keepalive_active": True,
                "subaccounts": [{
                    "advertiser_id": "sub-a",
                    "enabled": True,
                    "seedance_access": True,
                    "credits": 100,
                }],
            },
            {
                "id": "login-b",
                "enabled": True,
                "running": True,
                "logged_in": True,
                "session_available": True,
                "keepalive_active": False,
                "subaccounts": [{
                    "advertiser_id": "sub-b",
                    "enabled": True,
                    "seedance_access": True,
                    "credits": 100,
                }],
            },
        ]

    class FakeProtocol:
        load = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            self.load += 1
            return "sub-b-task"

    protocol = FakeProtocol()
    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    pool._protocol_client = lambda *_args, **_kwargs: protocol  # type: ignore[method-assign]

    task_id = await asyncio.wait_for(
        pool.create_text_video(prompt="fail over", model="seedance-2.0", duration=5),
        timeout=1,
    )

    assert task_id == "sub-b-task"
    assert pool.account_for_task(task_id) == "login-b"
    assert pool._keepalive_waiters == 0


@pytest.mark.asyncio
async def test_protocol_pool_balances_concurrency_across_same_login_subaccounts(
    tmp_path: Path,
) -> None:
    pool = BrowserPoolClient(
        Settings(sd2api_database=str(tmp_path / "protocol-pool.db")),
        TaskStore(str(tmp_path / "protocol-pool.db")),
    )
    account = {
        "id": "login-a",
        "enabled": True,
        "running": True,
        "logged_in": True,
        "session_available": True,
        "queued": 0,
        "busy": False,
        "subaccounts": [
            {
                "advertiser_id": "sub-a",
                "enabled": True,
                "seedance_access": True,
                "credits": 100,
            },
            {
                "advertiser_id": "sub-b",
                "enabled": True,
                "seedance_access": True,
                "credits": 100,
            },
        ],
    }

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [account]

    class FakeProtocol:
        def __init__(self, advertiser_id: str) -> None:
            self.advertiser_id = advertiser_id
            self.load = 0
            self.calls = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            self.calls += 1
            self.load += 1
            return f"{self.advertiser_id}-task-{self.calls}"

    clients = {name: FakeProtocol(name) for name in ("sub-a", "sub-b")}
    pool._protocol_clients = {
        ("login-a", name): client for name, client in clients.items()
    }  # type: ignore[assignment]
    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, advertiser_id=None: clients[str(advertiser_id)]
    )

    tasks = await asyncio.gather(
        *(
            pool.create_text_video(
                prompt=f"same-login-{index}", model="seedance-2.0", duration=5
            )
            for index in range(4)
        )
    )

    assert [task.split("-task", 1)[0] for task in tasks] == [
        "sub-a",
        "sub-b",
        "sub-a",
        "sub-b",
    ]
    assert clients["sub-a"].load == clients["sub-b"].load == 2
    status = await pool.status()
    assert status["max_parallel"] == 496


@pytest.mark.asyncio
async def test_protocol_pool_allows_one_subaccount_until_global_limit(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "global-limit.db"))
    store.create_account(account_id="login-a", name="Login A")
    store.upsert_subaccounts(
        "login-a",
        [
            {
                "advertiser_id": "sub-a",
                "name": "Sub A",
                "account_type": "partner",
                "seedance_access": True,
                "credits": 100,
            },
        ],
    )
    store.set_subaccount_enabled("login-a", "sub-a", True)
    pool = BrowserPoolClient(Settings(sd2api_pool_max_pending=7), store)

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [
            {
                "id": "login-a",
                "enabled": True,
                "running": True,
                "logged_in": True,
                "session_available": True,
                "subaccounts": pool._decorate_subaccounts(
                    "login-a", store.list_subaccounts("login-a")
                ),
            }
        ]

    class FakeProtocol:
        def __init__(self, advertiser_id: str) -> None:
            self.advertiser_id = advertiser_id
            self.load = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            self.load += 1
            return f"{self.advertiser_id}-task-{self.load}"

    clients = {"sub-a": FakeProtocol("sub-a")}
    pool._protocol_clients = {  # type: ignore[assignment]
        ("login-a", "sub-a"): clients["sub-a"]
    }
    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, advertiser_id=None: clients[str(advertiser_id)]
    )

    tasks = []
    for index in range(7):
        tasks.append(
            await pool.create_text_video(
                prompt=f"slot-{index}", model="seedance-2.0", duration=5
            )
        )
    assert len(tasks) == 7
    assert clients["sub-a"].load == 7
    with pytest.raises(TikTokUpstreamError) as error:
        await pool.create_text_video(
            prompt="overflow", model="seedance-2.0", duration=5
        )
    assert error.value.status_code == 429
    assert error.value.code == "pool_queue_full"


@pytest.mark.parametrize(
    ("upstream_error", "minimum_cooldown"),
    [
        (
            TikTokUpstreamError(
                "EndpointAccountRpmRateLimitExceeded",
                status_code=429,
                code="10043101",
            ),
            80,
        ),
        (
            TikTokUpstreamError(
                "User generation 5min limit",
                status_code=429,
                code="10001113",
            ),
            290,
        ),
        (
            TikTokUpstreamError(
                "Concurrent generation full: 5 videos are still processing",
                status_code=429,
                code="concurrency_full",
            ),
            0,
        ),
    ],
)
@pytest.mark.asyncio
async def test_protocol_pool_uses_upstream_limits_to_fail_over_subaccounts(
    tmp_path: Path,
    upstream_error: TikTokUpstreamError,
    minimum_cooldown: int,
) -> None:
    store = TaskStore(str(tmp_path / f"response-limit-{minimum_cooldown}.db"))
    for account_id, advertiser_id in (("a", "sub-a"), ("b", "sub-b")):
        store.create_account(account_id=account_id, name=account_id)
        store.upsert_subaccounts(
            account_id,
            [{
                "advertiser_id": advertiser_id,
                "name": advertiser_id,
                "account_type": "partner",
                "seedance_access": True,
                "credits": 100,
            }],
        )
        store.set_subaccount_enabled(account_id, advertiser_id, True)
    pool = BrowserPoolClient(Settings(), store)

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [
            {
                "id": account_id,
                "enabled": True,
                "running": True,
                "logged_in": True,
                "session_available": True,
                "subaccounts": pool._decorate_subaccounts(
                    account_id, store.list_subaccounts(account_id)
                ),
            }
            for account_id in ("a", "b")
        ]

    class LimitedClient:
        load = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            raise upstream_error

    class HealthyClient:
        load = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            self.load += 1
            return "healthy-task"

    clients = {"sub-a": LimitedClient(), "sub-b": HealthyClient()}
    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, advertiser_id=None: clients[str(advertiser_id)]
    )

    assert await pool.create_text_video(
        prompt="response-driven failover", model="seedance-2.0", duration=5
    ) == "healthy-task"
    limited = store.list_subaccounts("a")[0]
    remaining = int(limited.get("rate_limited_until") or 0) - int(time.time())
    if minimum_cooldown:
        assert remaining >= minimum_cooldown
        assert limited["rate_limit_reason"] == str(upstream_error)
    else:
        assert limited["rate_limited_until"] is None
        assert limited["rate_limit_reason"] is None


@pytest.mark.asyncio
async def test_pool_fails_over_when_subaccount_hits_daily_quota(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "quota-failover.db"))
    for account_id, advertiser_id in (("a", "sub-a"), ("b", "sub-b")):
        store.create_account(account_id=account_id, name=account_id)
        store.upsert_subaccounts(
            account_id,
            [
                {
                    "advertiser_id": advertiser_id,
                    "name": advertiser_id,
                    "account_type": "partner",
                    "seedance_access": True,
                    "credits": 100,
                }
            ],
        )
        store.set_subaccount_enabled(account_id, advertiser_id, True)
    pool = BrowserPoolClient(Settings(sd2api_pool_quota_cooldown=600), store)

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [
            {
                "id": account_id,
                "enabled": True,
                "running": True,
                "logged_in": True,
                "session_available": True,
                "subaccounts": pool._decorate_subaccounts(
                    account_id, store.list_subaccounts(account_id)
                ),
            }
            for account_id in ("a", "b")
        ]

    class QuotaClient:
        load = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            raise TikTokUpstreamError(
                "User Generation Day Limit: limit_level=STRATEGY",
                status_code=429,
                code="10040104",
            )

    class HealthyClient:
        load = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            self.load += 1
            return "healthy-task"

    clients = {"sub-a": QuotaClient(), "sub-b": HealthyClient()}
    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, advertiser_id=None: clients[str(advertiser_id)]
    )

    task_id = await pool.create_text_video(
        prompt="fail over", model="seedance-2.0", duration=5
    )
    assert task_id == "healthy-task"
    blocked = next(
        item for item in store.list_subaccounts("a") if item["advertiser_id"] == "sub-a"
    )
    assert blocked["quota_blocked_until"] > int(time.time())
    assert blocked["quota_reason"] == (
        "User Generation Day Limit: limit_level=STRATEGY"
    )
    assert pool.account_for_task(task_id) == "b"


@pytest.mark.asyncio
async def test_pool_retries_transient_submission_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TaskStore(str(tmp_path / "submission-retry.db"))
    store.create_account(account_id="a", name="a")
    store.upsert_subaccounts(
        "a",
        [
            {
                "advertiser_id": "sub-a",
                "name": "sub-a",
                "account_type": "partner",
                "seedance_access": True,
                "credits": 100,
            }
        ],
    )
    store.set_subaccount_enabled("a", "sub-a", True)
    pool = BrowserPoolClient(Settings(), store)

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [
            {
                "id": "a",
                "enabled": True,
                "running": True,
                "logged_in": True,
                "session_available": True,
                "subaccounts": pool._decorate_subaccounts(
                    "a", store.list_subaccounts("a")
                ),
            }
        ]

    class TransientClient:
        load = 0
        attempts = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            self.attempts += 1
            if self.attempts < 3:
                raise TikTokUpstreamError(
                    "biz error: remote or network error[remote]: "
                    "THRIFT_EGRESS request timeout connect_timeout=50ms",
                    status_code=502,
                    code="50000",
                )
            return "retried-task"

    async def no_sleep(_delay: float) -> None:
        return None

    client = TransientClient()
    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, advertiser_id=None: client
    )
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    assert await pool.create_text_video(
        prompt="retry timeout", model="seedance-2.0", duration=5
    ) == "retried-task"
    assert client.attempts == 3
    retry_events = [
        event
        for event in store.list_events()
        if event.message == "Retrying task submission after transient upstream error"
    ]
    assert len(retry_events) == 2


@pytest.mark.asyncio
async def test_pool_fails_over_to_another_login_when_session_expires(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "authentication-failover.db"))
    for account_id, advertiser_ids in (
        ("expired", ("expired-a", "expired-b")),
        ("healthy", ("healthy-a",)),
    ):
        store.create_account(account_id=account_id, name=account_id)
        store.update_account(
            account_id,
            session_ciphertext=f"{account_id}-session",
            session_updated_at=int(time.time()),
            login_state="logged_in",
        )
        store.upsert_subaccounts(
            account_id,
            [
                {
                    "advertiser_id": advertiser_id,
                    "name": advertiser_id,
                    "account_type": "partner",
                    "seedance_access": True,
                    "credits": 100,
                }
                for advertiser_id in advertiser_ids
            ],
        )
        for advertiser_id in advertiser_ids:
            store.set_subaccount_enabled(account_id, advertiser_id, True)
    pool = BrowserPoolClient(Settings(), store)

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [
            {
                "id": account_id,
                "enabled": True,
                "running": True,
                "logged_in": True,
                "session_available": True,
                "subaccounts": pool._decorate_subaccounts(
                    account_id, store.list_subaccounts(account_id)
                ),
            }
            for account_id in ("expired", "healthy")
        ]

    class ExpiredClient:
        load = 0

        def __init__(self) -> None:
            self.calls = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            self.calls += 1
            raise TikTokUpstreamError(
                "InvalidLogin",
                status_code=502,
                code="38001001",
            )

    class HealthyClient:
        load = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            return "healthy-task"

    expired_clients = {
        advertiser_id: ExpiredClient()
        for advertiser_id in ("expired-a", "expired-b")
    }
    clients: dict[str, Any] = {
        **expired_clients,
        "healthy-a": HealthyClient(),
    }
    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, advertiser_id=None: clients[str(advertiser_id)]
    )

    task_id = await pool.create_text_video(
        prompt="fail over after logout", model="seedance-2.0", duration=5
    )

    assert task_id == "healthy-task"
    assert sum(client.calls for client in expired_clients.values()) == 1
    assert pool.account_for_task(task_id) == "healthy"
    expired_account = store.get_account("expired")
    assert expired_account is not None
    assert expired_account["login_state"] == "pending"
    assert expired_account["session_available"] is False
    assert expired_account["last_error"] == (
        "TikTok session expired; the account must log in again"
    )


@pytest.mark.asyncio
async def test_pool_returns_authentication_error_only_after_all_logins_expire(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "all-authentication-expired.db"))
    for account_id in ("a", "b"):
        advertiser_id = f"sub-{account_id}"
        store.create_account(account_id=account_id, name=account_id)
        store.update_account(
            account_id,
            session_ciphertext=f"{account_id}-session",
            session_updated_at=int(time.time()),
            login_state="logged_in",
        )
        store.upsert_subaccounts(
            account_id,
            [
                {
                    "advertiser_id": advertiser_id,
                    "name": advertiser_id,
                    "account_type": "partner",
                    "seedance_access": True,
                    "credits": 100,
                }
            ],
        )
        store.set_subaccount_enabled(account_id, advertiser_id, True)
    pool = BrowserPoolClient(Settings(), store)

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [
            {
                "id": account_id,
                "enabled": True,
                "running": True,
                "logged_in": True,
                "session_available": True,
                "subaccounts": pool._decorate_subaccounts(
                    account_id, store.list_subaccounts(account_id)
                ),
            }
            for account_id in ("a", "b")
        ]

    class ExpiredClient:
        load = 0

        async def create_text_video(self, **kwargs: Any) -> str:
            raise TikTokUpstreamError(
                "TikTok session expired; the account must log in again",
                status_code=401,
                code="tiktok_authentication_error",
            )

    clients = {f"sub-{account_id}": ExpiredClient() for account_id in ("a", "b")}
    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, advertiser_id=None: clients[str(advertiser_id)]
    )

    with pytest.raises(TikTokUpstreamError) as error:
        await pool.create_text_video(
            prompt="all logged out", model="seedance-2.0", duration=5
        )

    assert error.value.status_code == 401
    assert error.value.code == "tiktok_authentication_error"
    for account_id in ("a", "b"):
        account = store.get_account(account_id)
        assert account is not None
        assert account["login_state"] == "pending"
        assert account["session_available"] is False


@pytest.mark.asyncio
async def test_pool_returns_429_when_every_selected_subaccount_is_quota_blocked(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "all-quota-blocked.db"))
    store.create_account(account_id="a", name="A")
    store.upsert_subaccounts(
        "a",
        [
            {
                "advertiser_id": "sub-a",
                "name": "Sub A",
                "account_type": "partner",
                "seedance_access": True,
                "credits": 100,
            }
        ],
    )
    store.set_subaccount_enabled("a", "sub-a", True)
    store.update_subaccount(
        "a",
        "sub-a",
        quota_blocked_until=int(time.time()) + 600,
        quota_reason="Daily generation limit reached",
        quota_updated_at=int(time.time()),
    )
    pool = BrowserPoolClient(Settings(), store)

    async def fake_list_accounts() -> list[dict[str, Any]]:
        return [
            {
                "id": "a",
                "enabled": True,
                "running": True,
                "logged_in": True,
                "session_available": True,
                "subaccounts": pool._decorate_subaccounts(
                    "a", store.list_subaccounts("a")
                ),
            }
        ]

    pool.list_accounts = fake_list_accounts  # type: ignore[method-assign]
    with pytest.raises(TikTokUpstreamError) as error:
        await pool.create_text_video(
            prompt="blocked", model="seedance-2.0", duration=5
        )
    assert error.value.status_code == 429
    assert error.value.code == "subaccount_daily_quota_exhausted"


@pytest.mark.asyncio
async def test_refresh_subaccounts_is_allowed_while_generation_is_active(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "refresh-active.db"))
    store.create_account(account_id="login-a", name="Login A")
    store.create(
        task_id="active-task",
        api="openai",
        model="seedance-2.0",
        prompt="active",
        seconds=5,
        account_id="login-a",
        advertiser_id="sub-a",
    )
    pool = BrowserPoolClient(Settings(), store)

    class RootClient:
        async def discover_subaccounts(self) -> list[dict[str, Any]]:
            return [
                {
                    "advertiser_id": "sub-a",
                    "name": "Sub A",
                    "account_type": "partner",
                }
            ]

    class SubClient:
        async def account_capabilities(self) -> dict[str, Any]:
            return {
                "advertiser_id": "sub-a",
                "credits": 95,
                "seedance_access": True,
            }

    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, advertiser_id=None: SubClient() if advertiser_id else RootClient()
    )
    result = await pool.refresh_subaccounts("login-a", check_access=True)
    assert result["subaccounts"][0]["credits"] == 95


@pytest.mark.asyncio
async def test_protocol_pool_refreshes_subaccount_credits_after_terminal_task(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "credit-pool.db"))
    pool = BrowserPoolClient(Settings(), store)
    pool._task_accounts["task-1"] = "login-a"
    pool._task_advertisers["task-1"] = "sub-a"
    updates: dict[str, Any] = {}

    class FakeProtocol:
        async def check_task(self, task_id: str) -> UpstreamTask:
            return UpstreamTask(
                id=task_id,
                status="succeeded",
                progress=100,
                video_id="vid",
                video_url="https://cdn.example/video.mp4",
            )

        async def account_capabilities(self) -> dict[str, Any]:
            return {"credits": 95}

    store.get_account = (  # type: ignore[method-assign]
        lambda _account_id: {"session_available": True}
    )

    def capture_update(_account_id: str, _advertiser_id: str, **changes: Any):
        updates.update(changes)

    store.update_subaccount = capture_update  # type: ignore[method-assign]
    pool._protocol_client = (  # type: ignore[method-assign]
        lambda _account_id, _advertiser_id=None: FakeProtocol()
    )

    task = await pool.check_task("task-1")
    assert task.status == "succeeded"
    assert updates["credits"] == 95
    assert isinstance(updates["last_checked_at"], int)


def test_ttoh_rss_only_returns_current_featured_offers() -> None:
    from sd2api.main import parse_ttoh_featured_offers

    rss = """<?xml version="1.0"?><rss><channel>
      <item><title>Current</title><link>https://go.ttoh.app/CURRENT</link><description>Save 50% | Region: SEA | Start: 2026-07-30 | End: 2026-08-10</description><category>Featured</category></item>
      <item><title>Future</title><link>https://go.ttoh.app/FUTURE</link><description>Later | Region: NA | Start: 2026-08-20 | End: 2026-09-15</description><category>Featured</category></item>
      <item><title>Expired</title><link>https://go.ttoh.app/OLD</link><description>Old | Region: EU | Start: 2026-07-01 | End: 2026-07-31</description><category>Featured</category></item>
      <item><title>Evergreen</title><link>https://go.ttoh.app/EVERGREEN</link><description>Always | Region: Global | Start: 2025-01-01</description><category>credit</category></item>
    </channel></rss>"""
    now = int(datetime(2026, 8, 6, 12, tzinfo=timezone.utc).timestamp())

    assert parse_ttoh_featured_offers(rss, now) == [
        {
            "title": "Current",
            "description": "Save 50%",
            "link": "https://go.ttoh.app/CURRENT",
            "region": "SEA",
            "start": "2026-07-30",
            "end": "2026-08-10",
        }
    ]


def test_admin_account_routes_without_starting_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sd2api.main as main

    pool_store = TaskStore(str(tmp_path / "admin.db"))
    pool = BrowserPoolClient(
        Settings(
            sd2api_browser_profile=str(tmp_path / "profiles"),
            sd2api_admin_key="test-admin-key-at-least-16-characters",
        ),
        pool_store,
    )
    monkeypatch.setattr(main, "store", pool_store)
    monkeypatch.setattr(main, "client", pool)
    api = TestClient(main.app)
    headers = {
        "Authorization": f"Bearer {main.settings.sd2api_admin_key or main.settings.sd2api_api_key}"
    }

    created = api.post(
        "/admin/accounts",
        headers=headers,
        json={
            "username": "primary@example.com",
            "password": "primary-password",
            "start": False,
        },
    )
    listed = api.get("/admin/accounts", headers=headers)
    status = api.get("/admin/pool/status", headers=headers)
    dashboard = api.get("/admin")
    version = api.get("/admin/version")
    styles = api.get("/admin/assets/admin.css")
    script = api.get("/admin/assets/admin.js")
    logs = api.get("/admin/logs", headers=headers)
    paged_logs = api.get("/admin/logs?limit=50&page=1", headers=headers)
    paged_tasks = api.get("/admin/tasks?limit=50&page=1", headers=headers)
    secured = api.post(
        "/admin/accounts",
        headers=headers,
        json={
            "id": "account_secure",
            "name": "Secure account",
            "username": "login@example.com",
            "password": "not-stored-in-plain-text",
            "start": False,
        },
    )

    assert created.status_code == 200
    assert created.json()["id"].startswith("account_")
    assert created.json()["name"] == "primary@example.com"
    assert created.json()["running"] is False
    assert listed.json()["data"][0]["name"] == "primary@example.com"
    assert status.json()["max_parallel"] == 0
    assert dashboard.status_code == 200
    assert version.json() == {"version": "1.2.1"}
    assert 'id="appVersion"' in dashboard.text
    assert 'href="https://github.com/usdfan/sd2api"' in dashboard.text
    assert "sd2api 控制台" in dashboard.text
    assert all(label in dashboard.text for label in ("生视频", "号池管理", "日志", "视频管理", "系统配置"))
    assert 'name="email_address"' not in dashboard.text
    assert styles.status_code == 200
    assert ".sidebar" in styles.text
    assert ".sidebar-foot { margin-top: auto;" in styles.text
    assert script.status_code == 200
    assert "refreshVideos" in script.text
    assert "/admin/version" in script.text
    assert "sd2api_ttoh_dismissed_date" in script.text
    assert "dismissTtohAdsToday" in script.text
    assert "data-ttoh-close" in script.text
    assert 'class="switch-track"' in script.text
    assert "加入调度" in script.text
    assert "待调度" not in script.text
    assert '<span class="pill info">当前</span>' not in script.text
    assert logs.status_code == 200
    assert paged_logs.json()["pagination"]["page_size"] == 50
    assert paged_logs.json()["pagination"]["total"] >= 1
    assert paged_tasks.json()["pagination"] == {"page": 1, "page_size": 50, "total": 0}
    assert 'id="logPagination"' in dashboard.text
    assert 'id="videoPagination"' in dashboard.text
    assert "[50, 100, 200, 500]" in script.text
    assert any(item["message"] == "账号已加入号池" for item in logs.json()["data"])
    assert secured.status_code == 200
    assert secured.json()["credentials_configured"] is True
    assert "password_ciphertext" not in secured.json()
    stored = pool_store.get_account("account_secure", include_secret=True)
    assert stored is not None
    assert stored["password_ciphertext"] != "not-stored-in-plain-text"
    assert stored["email_address"] == "login@example.com"

    pool_store.upsert_subaccounts(
        "account_secure",
        [
            {
                "advertiser_id": "7668593711596617729",
                "name": "Partner account",
                "account_type": "partner",
                "seedance_access": True,
                "credits": 2000,
            }
        ],
    )
    selected = api.patch(
        "/admin/accounts/account_secure/subaccounts/7668593711596617729",
        headers=headers,
        json={"enabled": True},
    )
    assert selected.status_code == 200
    assert selected.json()["enabled"] is True
    assert selected.json()["credits"] == 2000


def test_runtime_config_file_round_trip_and_admin_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sd2api.main as main
    from sd2api.config import RuntimeConfig, load_runtime_config, save_runtime_config

    path = tmp_path / "config.json"
    original = RuntimeConfig(
        database=str(tmp_path / "runtime.db"),
        browser_profile=str(tmp_path / "profiles"),
        upload_dir=str(tmp_path / "uploads"),
        pool_max_pending=500,
    )
    save_runtime_config(original, path)
    loaded, source = load_runtime_config(path)
    assert source == "file"
    assert loaded == original

    monkeypatch.setattr(main, "CONFIG_PATH", path)
    previous_runtime, previous_source = main.settings.runtime, main.settings.config_source
    main.settings.replace_runtime(original)
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "admin-key")
    api = TestClient(main.app)
    headers = {"Authorization": "Bearer admin-key"}

    read = api.get("/admin/config", headers=headers)
    assert read.status_code == 200
    assert read.json()["config"]["pool_max_pending"] == 500
    assert read.json()["config"]["novnc_public_port"] == 6080
    assert "temp_mail_base_url" not in read.json()["config"]
    assert "auto_accept_terms" not in read.json()["config"]
    assert "sd2api_api_key" not in read.text
    updated = original.model_copy(
        update={
            "pool_max_pending": 700,
            "temp_mail_poll_seconds": 4.0,
        }
    )
    response = api.put(
        "/admin/config", headers=headers, json=updated.model_dump(mode="json")
    )
    assert response.status_code == 200
    assert response.json()["restart_required"] is False
    assert set(response.json()["changed"]) == {
        "pool_max_pending",
        "temp_mail_poll_seconds",
    }
    persisted, _ = load_runtime_config(path)
    assert persisted.pool_max_pending == 700
    assert persisted.temp_mail_poll_seconds == 4.0
    assert main.settings.sd2api_pool_max_pending == 700
    main.settings.replace_runtime(previous_runtime, source=previous_source)


def test_admin_can_manage_multiple_api_keys_and_tasks_record_masked_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sd2api.main as main

    class FakeClient:
        async def create_text_video(self, **kwargs: object) -> str:
            return "managed-key-task"

    task_store = TaskStore(str(tmp_path / "managed-keys.db"))
    monkeypatch.setattr(main, "store", task_store)
    monkeypatch.setattr(main, "client", FakeClient())
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "admin-key-for-api-key-management")
    monkeypatch.setattr(main.settings, "sd2api_api_key", "legacy-api-key-for-compatibility")
    api = TestClient(main.app)
    admin_headers = {"Authorization": "Bearer admin-key-for-api-key-management"}

    created = api.post(
        "/admin/api-keys",
        headers=admin_headers,
        json={"name": "Render worker", "key": "worker-api-key-1234567890"},
    )
    assert created.status_code == 200
    assert created.json()["key"] == "worker-api-key-1234567890"
    assert created.json()["masked_key"] == "wo***90"

    listed = api.get("/admin/api-keys", headers=admin_headers)
    assert listed.status_code == 200
    assert [item["masked_key"] for item in listed.json()["data"]] == [
        "le***ty",
        "wo***90",
    ]
    assert "worker-api-key-1234567890" not in listed.text

    generated = api.post(
        "/v1/videos",
        headers={"Authorization": "Bearer worker-api-key-1234567890"},
        json={"model": "seedance-2.0", "prompt": "A calm lake", "seconds": 5},
    )
    assert generated.status_code == 200
    stored = task_store.get("managed-key-task")
    assert stored is not None
    assert stored.api_key_mask == "wo***90"

    admin_tasks = api.get("/admin/tasks", headers=admin_headers)
    assert admin_tasks.json()["data"][0]["api_key"] == "wo***90"
    assert "worker-api-key-1234567890" not in admin_tasks.text

    deleted = api.delete(
        f"/admin/api-keys/{created.json()['id']}", headers=admin_headers
    )
    assert deleted.status_code == 200
    denied = api.post(
        "/v1/videos",
        headers={"Authorization": "Bearer worker-api-key-1234567890"},
        json={"model": "seedance-2.0", "prompt": "A calm lake", "seconds": 5},
    )
    assert denied.status_code == 401


def test_runtime_config_migrates_old_dotenv_when_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sd2api.config import load_runtime_config

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "SD2API_MODE=browser\nSD2API_POOL_SUBACCOUNT_CONCURRENCY=8\n",
        encoding="utf-8",
    )
    config, source = load_runtime_config(tmp_path / "missing.json")
    assert source == "legacy_env"
    assert config.mode == "browser"
    assert "pool_subaccount_concurrency" not in config.model_dump()


def test_temp_mail_base_url_is_loaded_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sd2api.config import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SD2API_TEMP_MAIL_BASE_URL", "https://mail.example.com")
    configured = Settings()

    assert configured.sd2api_temp_mail_base_url == "https://mail.example.com"
    assert "temp_mail_base_url" not in configured.runtime.model_dump()


def test_runtime_config_admin_marks_path_changes_for_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sd2api.main as main
    from sd2api.config import RuntimeConfig

    path = tmp_path / "config.json"
    current = RuntimeConfig()
    monkeypatch.setattr(main, "CONFIG_PATH", path)
    previous_runtime, previous_source = main.settings.runtime, main.settings.config_source
    main.settings.replace_runtime(current, source="defaults")
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "admin-key")
    api = TestClient(main.app)
    changed = current.model_copy(update={"database": str(tmp_path / "new.db")})
    response = api.put(
        "/admin/config",
        headers={"Authorization": "Bearer admin-key"},
        json=changed.model_dump(mode="json"),
    )
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert response.json()["changed"] == ["database"]
    main.settings.replace_runtime(previous_runtime, source=previous_source)


def test_admin_tasks_refresh_pending_records_and_expose_actual_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sd2api.main as main

    class FakeClient:
        async def check_task(self, task_id: str) -> UpstreamTask:
            assert task_id == "task-refresh"
            return UpstreamTask(
                id=task_id,
                status="succeeded",
                progress=100,
                video_url="https://cdn.example/video.mp4",
            )

    task_store = TaskStore(str(tmp_path / "task-refresh.db"))
    task_store.create(
        task_id="task-refresh",
        api="openai",
        model="sora-2",
        prompt="A portrait",
        seconds=5,
    )
    monkeypatch.setattr(main, "store", task_store)
    monkeypatch.setattr(main, "client", FakeClient())
    api = TestClient(main.app)
    headers = {
        "Authorization": f"Bearer {main.settings.sd2api_admin_key or main.settings.sd2api_api_key}"
    }

    response = api.get(
        "/admin/tasks?refresh_pending=true&search=portrait", headers=headers
    )
    assert response.status_code == 200
    task = response.json()["data"][0]
    assert task["status"] == "succeeded"
    assert task["downloadable"] is True
    assert task["model"] == "sora-2"
    assert task["upstream_model"] == "seedance-2.0"
    assert response.json()["summary"]["succeeded"] == 1
    assert any(
        event.message == "视频生成完成" for event in task_store.list_events()
    )


def test_admin_tasks_and_logs_support_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sd2api.main as main

    task_store = TaskStore(str(tmp_path / "pagination.db"))
    for index in range(3):
        task_store.create(
            task_id=f"task-{index}",
            api="openai",
            model="sora-2",
            prompt=f"Prompt {index}",
            seconds=5,
        )
        task_store.add_event(
            level="info", category="video", message=f"Event {index}"
        )
    monkeypatch.setattr(main, "store", task_store)
    api = TestClient(main.app)
    headers = {
        "Authorization": f"Bearer {main.settings.sd2api_admin_key or main.settings.sd2api_api_key}"
    }

    tasks = api.get("/admin/tasks?limit=2&page=2", headers=headers)
    logs = api.get("/admin/logs?limit=2&page=2", headers=headers)

    assert tasks.status_code == 200
    assert len(tasks.json()["data"]) == 1
    assert tasks.json()["pagination"] == {
        "page": 2, "page_size": 2, "total": 3
    }
    assert logs.status_code == 200
    assert len(logs.json()["data"]) == 1
    assert logs.json()["pagination"] == {
        "page": 2, "page_size": 2, "total": 3
    }


def test_admin_duration_analytics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sd2api.main as main

    task_store = TaskStore(str(tmp_path / "analytics.db"))
    now = int(time.time())
    # Use the previous complete local day so the test is stable before 14:00.
    local_day_start = now - ((now + 8 * 3600) % 86400) - 86400
    for task_id, status, created_at, duration in [
        ("fast", "succeeded", local_day_start + 2 * 3600, 300),
        ("slow", "succeeded", local_day_start + 14 * 3600, 7200),
        ("failed", "failed", local_day_start + 14 * 3600 + 600, 600),
    ]:
        task_store.create(
            task_id=task_id,
            api="openai",
            model="sora-2",
            prompt=task_id,
            seconds=5,
            account_id="account-a",
        )
        with task_store._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, progress = 100, created_at = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, created_at, created_at + duration, created_at + duration, task_id),
            )

    monkeypatch.setattr(main, "store", task_store)
    api = TestClient(main.app)
    headers = {
        "Authorization": f"Bearer {main.settings.sd2api_admin_key or main.settings.sd2api_api_key}"
    }
    response = api.get(
        "/admin/analytics/durations?range=7d&timezone_offset=480",
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["completed_samples"] == 2
    assert data["hourly"][2] == {
        "hour": 2,
        "average_seconds": 300,
        "sample_count": 1,
        "level": "recommended",
        "confidence": "low",
    }
    assert data["hourly"][14]["average_seconds"] == 7200
    assert data["hourly"][14]["level"] == "busy"
    assert data["recommended_hours"] == [2]
    assert data["busy_hours"] == [14]
    assert sum(hour["sample_count"] for day in data["heatmap"] for hour in day["hours"]) == 2


def test_duration_level_uses_thirty_minutes_as_recommended_cutoff() -> None:
    from sd2api.main import duration_level

    assert duration_level(30 * 60) == "recommended"
    assert duration_level(30 * 60 + 1) == "normal"
    assert duration_level(60 * 60) == "normal"
    assert duration_level(60 * 60 + 1) == "busy"


def test_admin_key_can_submit_openai_compatible_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sd2api.main as main

    class FakeClient:
        async def create_text_video(self, **kwargs: object) -> str:
            assert kwargs["model"] == "seedance-2.0"
            return "admin-created-task"

    monkeypatch.setattr(main.settings, "sd2api_api_key", "separate-api-key")
    monkeypatch.setattr(main.settings, "sd2api_admin_key", "separate-admin-key")
    monkeypatch.setattr(main, "client", FakeClient())
    monkeypatch.setattr(main, "store", TaskStore(str(tmp_path / "admin-generate.db")))
    api = TestClient(main.app)

    response = api.post(
        "/v1/videos",
        headers={"Authorization": "Bearer separate-admin-key"},
        json={
            "model": "seedance-2.0",
            "prompt": "A quiet portrait",
            "seconds": 5,
        },
    )
    assert response.status_code == 200
    assert response.json()["id"] == "admin-created-task"


def test_video_api_returns_upstream_model_permission_error_as_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sd2api.main as main

    class DeniedClient:
        async def create_text_video(self, **kwargs: object) -> str:
            raise TikTokUpstreamError(
                "没有模型使用权限",
                status_code=403,
                code="10001100",
            )

    monkeypatch.setattr(main, "client", DeniedClient())
    monkeypatch.setattr(main, "store", TaskStore(str(tmp_path / "denied.db")))
    api = TestClient(main.app)
    headers = {"Authorization": f"Bearer {main.settings.sd2api_api_key}"}

    response = api.post(
        "/v1/videos",
        headers=headers,
        json={
            "model": "seedance-2.0",
            "prompt": "A red ball",
            "seconds": 5,
        },
    )
    assert response.status_code == 403
    assert response.json()["error"] == {
        "message": "没有模型使用权限",
        "type": "upstream_error",
        "param": None,
        "code": "10001100",
    }

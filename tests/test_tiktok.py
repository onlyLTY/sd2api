from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sd2api.config import Settings
from sd2api.browser_pool import BrowserPoolClient
from sd2api.models import UpstreamTask
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

    config = Settings(tiktok_cookie="sessionid=test; csrftoken=csrf", tiktok_device_id="device")
    client = TikTokClient(config, transport=httpx.MockTransport(handler))
    task_id = await client.create_text_video(prompt="A red ball", model="sora-2", duration=5)
    result = await client.check_task(task_id)

    assert task_id == "task-123"
    assert result.status == "succeeded"
    assert result.video_url == "https://cdn.example/video.mp4"
    assert requests[0].headers["x-csrftoken"] == "csrf"
    assert requests[0].url.params["device_id"] == "device"


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

    config = Settings(tiktok_cookie="sessionid=test", tiktok_device_id="device")
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


@pytest.mark.asyncio
async def test_pool_schedules_concurrent_jobs_across_accounts(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "pool.db"))
    store.create_account(account_id="a", name="Account A")
    store.create_account(account_id="b", name="Account B")
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
        json={"id": "account_001", "name": "Main account", "start": False},
    )
    listed = api.get("/admin/accounts", headers=headers)
    status = api.get("/admin/pool/status", headers=headers)
    dashboard = api.get("/admin")
    secured = api.post(
        "/admin/accounts",
        headers=headers,
        json={
            "id": "account_secure",
            "name": "Secure account",
            "username": "login@example.com",
            "password": "not-stored-in-plain-text",
            "email_address": "codes@example.com",
            "start": False,
        },
    )

    assert created.status_code == 200
    assert created.json()["id"] == "account_001"
    assert created.json()["running"] is False
    assert listed.json()["data"][0]["name"] == "Main account"
    assert status.json()["max_parallel"] == 0
    assert dashboard.status_code == 200
    assert "TikTok Ads 账号池" in dashboard.text
    assert secured.status_code == 200
    assert secured.json()["credentials_configured"] is True
    assert "password_ciphertext" not in secured.json()
    stored = pool_store.get_account("account_secure", include_secret=True)
    assert stored is not None
    assert stored["password_ciphertext"] != "not-stored-in-plain-text"

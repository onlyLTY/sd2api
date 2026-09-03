from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger("sd2api.feishu")
FEISHU_BASE_URL = "https://open.feishu.cn"
MANUAL_ACTION_STATES = {
    "captcha_required",
    "waiting_email_code_manual",
    "browser_error",
    "login_failed",
    "not_configured",
}


class FeishuError(RuntimeError):
    pass


class FeishuClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._token = ""
        self._token_expires_at = 0.0
        self._token_credentials: tuple[str, str] | None = None

    def _credentials(self) -> tuple[str, str]:
        app_id = self.settings.sd2api_feishu_app_id.strip()
        app_secret = self.settings.sd2api_feishu_app_secret.strip()
        if not app_id or not app_secret:
            raise FeishuError("飞书 App ID 和 App Secret 尚未配置")
        return app_id, app_secret

    def _configuration(self) -> tuple[str, str, str, str]:
        app_id, app_secret = self._credentials()
        receive_id_type = self.settings.sd2api_feishu_receive_id_type.strip()
        receive_id = self.settings.sd2api_feishu_receive_id.strip()
        if not receive_id:
            raise FeishuError("尚未选择飞书接收对象")
        return app_id, app_secret, receive_id_type, receive_id

    @staticmethod
    def _response_payload(response: httpx.Response, action: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            if response.is_error:
                raise FeishuError(f"{action}: HTTP {response.status_code}") from exc
            raise FeishuError(f"{action}: 飞书返回了无效的 JSON") from exc
        if response.is_error or payload.get("code") != 0:
            code = payload.get("code")
            message = payload.get("msg") or payload.get("message") or response.reason_phrase
            suffix = f" (code={code})" if code is not None else ""
            raise FeishuError(f"{action}: {message}{suffix}")
        return payload

    async def _tenant_token(self, app_id: str, app_secret: str) -> str:
        credentials = (app_id, app_secret)
        if (
            self._token
            and self._token_credentials == credentials
            and self._token_expires_at > time.monotonic() + 60
        ):
            return self._token
        async with httpx.AsyncClient(
            base_url=FEISHU_BASE_URL,
            timeout=self.settings.sd2api_request_timeout,
            transport=self.transport,
        ) as client:
            try:
                response = await client.post(
                    "/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret},
                )
            except httpx.HTTPError as exc:
                raise FeishuError(f"获取飞书 tenant token 失败: {exc}") from exc
        payload = self._response_payload(response, "获取飞书 tenant token 失败")
        if not payload.get("tenant_access_token"):
            raise FeishuError("获取飞书 tenant token 失败: 响应中没有 token")
        self._token = str(payload["tenant_access_token"])
        self._token_credentials = credentials
        self._token_expires_at = time.monotonic() + max(
            60, int(payload.get("expire") or 7200)
        )
        return self._token

    async def send_text(self, text: str) -> str:
        app_id, app_secret, receive_id_type, receive_id = self._configuration()
        token = await self._tenant_token(app_id, app_secret)
        async with httpx.AsyncClient(
            base_url=FEISHU_BASE_URL,
            timeout=self.settings.sd2api_request_timeout,
            transport=self.transport,
        ) as client:
            try:
                response = await client.post(
                    "/open-apis/im/v1/messages",
                    params={"receive_id_type": receive_id_type},
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": receive_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": text}, ensure_ascii=False),
                    },
                )
            except httpx.HTTPError as exc:
                raise FeishuError(f"发送飞书消息失败: {exc}") from exc
        payload = self._response_payload(response, "发送飞书消息失败")
        return str((payload.get("data") or {}).get("message_id") or "")

    async def list_targets(self, receive_id_type: str) -> list[dict[str, str]]:
        app_id, app_secret = self._credentials()
        token = await self._tenant_token(app_id, app_secret)
        if receive_id_type not in {"chat_id", "open_id"}:
            raise FeishuError("通讯录选择仅支持个人或群聊")
        async with httpx.AsyncClient(
            base_url=FEISHU_BASE_URL,
            timeout=self.settings.sd2api_request_timeout,
            transport=self.transport,
        ) as client:
            if receive_id_type == "chat_id":
                results = await self._list_chats(client, token)
            else:
                results = await self._list_authorized_users(client, token)
        return sorted(results, key=lambda item: item["name"].casefold())

    async def _get(
        self,
        client: httpx.AsyncClient,
        token: str,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = await client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise FeishuError(f"读取飞书通讯录失败: {exc}") from exc
        return self._response_payload(response, "读取飞书通讯录失败")

    async def _list_chats(
        self, client: httpx.AsyncClient, token: str
    ) -> list[dict[str, str]]:
        results: dict[str, str] = {}
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            payload = await self._get(client, token, "/open-apis/im/v1/chats", params)
            data = payload.get("data") or {}
            for item in data.get("items") or []:
                if item.get("chat_id"):
                    results[str(item["chat_id"])] = str(item.get("name") or item["chat_id"])
            if not data.get("has_more") or not data.get("page_token"):
                break
            page_token = str(data["page_token"])
        return [{"id": target_id, "name": name} for target_id, name in results.items()]

    async def _list_authorized_users(
        self, client: httpx.AsyncClient, token: str
    ) -> list[dict[str, str]]:
        user_ids: set[str] = set()
        department_ids: set[str] = set()
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "user_id_type": "open_id",
                "department_id_type": "open_department_id",
                "page_size": 100,
            }
            if page_token:
                params["page_token"] = page_token
            payload = await self._get(
                client, token, "/open-apis/contact/v3/scopes", params
            )
            data = payload.get("data") or {}
            user_ids.update(str(value) for value in data.get("user_ids") or [])
            department_ids.update(
                str(value) for value in data.get("department_ids") or []
            )
            if not data.get("has_more") or not data.get("page_token"):
                break
            page_token = str(data["page_token"])

        results: dict[str, str] = {}
        for department_id in department_ids:
            page_token = ""
            while True:
                params = {
                    "department_id": department_id,
                    "department_id_type": "open_department_id",
                    "fetch_child": True,
                    "page_size": 50,
                    "user_id_type": "open_id",
                }
                if page_token:
                    params["page_token"] = page_token
                payload = await self._get(
                    client,
                    token,
                    "/open-apis/contact/v3/users/find_by_department",
                    params,
                )
                data = payload.get("data") or {}
                for user in data.get("items") or []:
                    if user.get("open_id"):
                        results[str(user["open_id"])] = str(
                            user.get("name") or user["open_id"]
                        )
                if not data.get("has_more") or not data.get("page_token"):
                    break
                page_token = str(data["page_token"])

        semaphore = asyncio.Semaphore(10)

        async def load_user(user_id: str) -> tuple[str, str]:
            async with semaphore:
                payload = await self._get(
                    client,
                    token,
                    f"/open-apis/contact/v3/users/{user_id}",
                    {"user_id_type": "open_id"},
                )
            user = (payload.get("data") or {}).get("user") or {}
            return user_id, str(user.get("name") or user_id)

        direct_users = await asyncio.gather(
            *(load_user(user_id) for user_id in user_ids if user_id not in results)
        )
        results.update(direct_users)
        return [{"id": target_id, "name": name} for target_id, name in results.items()]


class FeishuNotifier:
    def __init__(
        self,
        settings: Settings,
        *,
        feishu_client: FeishuClient | None = None,
        poll_seconds: float = 5.0,
    ) -> None:
        self.settings = settings
        self.client = feishu_client or FeishuClient(settings)
        self.poll_seconds = poll_seconds
        self._active_incidents: set[str] = set()

    @staticmethod
    def _needs_manual_action(account: dict[str, Any]) -> bool:
        state = str(account.get("login_state") or "")
        error = str(account.get("login_error") or account.get("last_error") or "")
        if state in MANUAL_ACTION_STATES:
            return True
        return state == "pending" and any(
            marker in error.lower()
            for marker in ("session expired", "re-login", "login required", "重新登录")
        )

    def _manual_action_message(self, account: dict[str, Any]) -> str:
        state = str(account.get("login_state") or "unknown")
        error = str(account.get("login_error") or account.get("last_error") or "无")
        lines = [
            "[sd2api] 账号需要人工操作",
            f"实例：{self.settings.sd2api_feishu_instance_name or 'sd2api'}",
            f"账号：{account.get('name') or account.get('username') or account.get('id')}",
            f"状态：{state}",
            f"原因：{error}",
            f"时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ]
        if self.settings.sd2api_feishu_novnc_url.strip():
            lines.append(f"noVNC：{self.settings.sd2api_feishu_novnc_url.strip()}")
        return "\n".join(lines)

    async def check_accounts(self, accounts: list[dict[str, Any]]) -> None:
        current = {
            str(account["id"])
            for account in accounts
            if account.get("enabled", True) and self._needs_manual_action(account)
        }
        self._active_incidents.intersection_update(current)
        if not (
            self.settings.sd2api_feishu_enabled
            and self.settings.sd2api_feishu_notify_manual_action
        ):
            return
        for account in accounts:
            account_id = str(account.get("id") or "")
            if account_id not in current or account_id in self._active_incidents:
                continue
            await self.client.send_text(self._manual_action_message(account))
            self._active_incidents.add(account_id)

    async def run(
        self, account_loader: Callable[[], Awaitable[list[dict[str, Any]]]]
    ) -> None:
        while True:
            try:
                await self.check_accounts(await account_loader())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Feishu manual-action notification failed")
            await asyncio.sleep(self.poll_seconds)

    async def send_test(self) -> str:
        name = self.settings.sd2api_feishu_instance_name or "sd2api"
        return await self.client.send_text(
            f"[sd2api] 飞书通知测试成功\n实例：{name}\n"
            f"时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

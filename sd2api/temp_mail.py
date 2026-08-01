from __future__ import annotations

import asyncio
import html
import re
import time
from datetime import datetime
from typing import Any

import httpx


class TempMailError(RuntimeError):
    pass


class TempMailClient:
    """Client for coolqoo/cf_temp_mail's authenticated REST API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        poll_seconds: float = 3,
        timeout_seconds: int = 180,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    async def wait_for_code(self, *, to_address: str, since: float) -> str:
        if not self.configured:
            raise TempMailError("Temporary-mail API URL or key is not configured")
        deadline = time.monotonic() + self.timeout_seconds
        seen: set[str] = set()
        while time.monotonic() < deadline:
            items = await self._list_emails(to_address)
            candidates = sorted(items, key=self._received_timestamp, reverse=True)
            for item in candidates:
                item_id = str(item.get("id") or "")
                if item_id in seen or self._received_timestamp(item) + 5 < since:
                    continue
                seen.add(item_id)
                if not self._looks_like_tiktok(item):
                    continue
                code = self._extract_code(item)
                if not code and item_id:
                    code = self._extract_code(await self._email_detail(item_id))
                if code:
                    return code
            await asyncio.sleep(self.poll_seconds)
        raise TempMailError(
            f"No TikTok verification email arrived for {to_address!r} within "
            f"{self.timeout_seconds} seconds"
        )

    async def _list_emails(self, to_address: str) -> list[dict[str, Any]]:
        payload = await self._request(
            "/api/emails",
            params={"page": 1, "pageSize": 100, "to_address": to_address},
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        return [item for item in items if isinstance(item, dict)]

    async def _email_detail(self, item_id: str) -> dict[str, Any]:
        payload = await self._request(f"/api/emails/{item_id}")
        return payload if isinstance(payload, dict) else {}

    async def _request(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=20,
                transport=self.transport,
                headers={"Authorization": f"Bearer {self.api_key}"},
            ) as client:
                response = await client.get(self.base_url + path, params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TempMailError(f"Temporary-mail API request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise TempMailError("Temporary-mail API returned a non-object response")
        return payload

    @staticmethod
    def _received_timestamp(item: dict[str, Any]) -> float:
        raw = str(item.get("receivedAt") or item.get("received_at") or "")
        if not raw:
            return 0
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0

    @staticmethod
    def _looks_like_tiktok(item: dict[str, Any]) -> bool:
        text = " ".join(
            str(item.get(key) or "")
            for key in ("sender", "subject", "textBody", "text_body", "htmlBody")
        ).lower()
        return any(token in text for token in ("tiktok", "byte dance", "bytedance"))

    @staticmethod
    def _extract_code(item: dict[str, Any]) -> str | None:
        direct = str(item.get("verificationCode") or item.get("verification_code") or "")
        match = re.search(r"(?<![A-Z0-9])([A-Z0-9]{4,8})(?![A-Z0-9])", direct, re.I)
        if match:
            return match.group(1)
        raw_html = " ".join(
            str(item.get(key) or "") for key in ("htmlBody", "html_body")
        )
        # TikTok for Business currently places the six-character code in a
        # table cell. Keep this raw-HTML fallback before tags are stripped.
        match = re.search(
            r"(?<=\s)\b([A-Z0-9]{6})\b(?=[\s\S]*?</td)", raw_html
        )
        if match:
            return match.group(1)
        raw = " ".join(
            str(item.get(key) or "")
            for key in ("subject", "textBody", "text_body", "htmlBody", "html_body")
        )
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
        patterns = (
            # Current TikTok for Business mail: "... page. SPNRZX Your
            # verification code is valid ..."
            r"\b([A-Z0-9]{4,8})\b\s+Your verification code\b",
            r"login verification page[\s.:：-]*\b([A-Z0-9]{4,8})\b",
            # Common forward forms in English and Chinese.
            r"(?:verification|security|login)\s+code(?:\s+is)?[\s:：-]*\b([A-Z0-9]{4,8})\b",
            r"(?:确认|验证|验证码)[^A-Z0-9]{0,30}\b([A-Z0-9]{4,8})\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return match.group(1)
        return None

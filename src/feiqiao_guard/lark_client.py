from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from .config import Settings
from .models import RiskLevel


class LarkClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send_approval_card(
        self,
        *,
        request_id: str,
        terminal_session_id: str,
        command: str,
        risk_level: RiskLevel,
        expires_at: datetime,
        callback_token: str,
        extra_context: dict[str, Any] | None = None,
    ) -> bool:
        if not self._settings.lark_webhook_url:
            return False

        summary = self._safe_text((extra_context or {}).get("summary", command), max_len=220)
        callback_hint = (
            f"{self._settings.callback_base_url}/v1/callback/decision "
            f"(request_id={request_id}, token={callback_token})"
        )
        card_payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "飞桥守护｜Codex 提权审批"},
                    "template": "orange" if risk_level == RiskLevel.L3 else "blue",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**request_id**: `{request_id}`\n"
                            f"**terminal_session_id**: `{terminal_session_id}`\n"
                            f"**resume_hint**: `./scripts/run_codex_guarded.sh resume {terminal_session_id}`\n"
                            f"**risk_level**: `{risk_level.value}`\n"
                            f"**expires_at**: `{expires_at.isoformat()}`\n"
                            f"**summary**: `{summary}`\n"
                            f"**command**:\n```bash\n{command}\n```"
                        ),
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "plain_text", "content": f"回调参考：{callback_hint}"},
                        ],
                    },
                ],
            },
        }
        return self._post(card_payload)

    def _post(self, payload: dict) -> bool:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        if self._settings.lark_signing_secret:
            ts = str(int(time.time()))
            to_sign = f"{ts}\n{self._settings.lark_signing_secret}".encode("utf-8")
            sign = base64.b64encode(hmac.new(to_sign, digestmod=hashlib.sha256).digest()).decode("utf-8")
            headers["Timestamp"] = ts
            headers["Sign"] = sign

        req = urllib.request.Request(
            url=self._settings.lark_webhook_url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, TimeoutError):
            return False

    def _safe_text(self, value: Any, *, max_len: int) -> str:
        text = " ".join(str(value).split()) if value is not None else ""
        if not text:
            return "<none>"
        if len(text) <= max_len:
            return text
        return f"{text[: max_len - 3]}..."

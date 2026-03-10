from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from .config import Settings
from .models import RiskLevel


class DingTalkClient:
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
        approve_url: str,
        reject_url: str,
        extra_context: dict[str, Any] | None = None,
    ) -> bool:
        if not self._settings.dingtalk_webhook_url:
            return False
        if self._is_paused_webhook(self._settings.dingtalk_webhook_url):
            return False

        approve_link = self._to_dingtalk_link(approve_url)
        reject_link = self._to_dingtalk_link(reject_url)
        prompt_type = str((extra_context or {}).get("prompt_type", "command_execution"))
        prompt_type_label = "执行命令" if prompt_type == "command_execution" else "应用改动"
        request_tag = request_id.split("-", 1)[0]
        card_title = f"飞桥守护 | {prompt_type_label}审批 | {risk_level.value} | {request_tag}"
        summary = self._safe_text((extra_context or {}).get("summary", command), max_len=220)
        preview_block = self._build_preview_block(extra_context)
        body = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": card_title,
                "text": (
                    f"### {card_title}\n"
                    f"- request_id: `{request_id}`\n"
                    f"- terminal_session_id: `{terminal_session_id}`\n"
                    f"- resume_hint: `./scripts/run_codex_guarded.sh resume {terminal_session_id}`\n"
                    f"- risk_level: `{risk_level.value}`\n"
                    f"- expires_at: `{expires_at.isoformat()}`\n"
                    f"- approval_type: `{prompt_type}`\n"
                    f"- summary: `{summary}`\n"
                    "- command:\n"
                    f"```bash\n{command}\n```\n\n"
                    f"{preview_block}\n"
                    "点击按钮后将进入确认页，再次确认才会真正执行。\n\n"
                    f"[备用同意链接]({approve_link})\n\n"
                    f"[备用拒绝链接]({reject_link})"
                ),
                "btnOrientation": "1",
                "btns": [
                    {"title": "同意执行", "actionURL": approve_link},
                    {"title": "拒绝执行", "actionURL": reject_link},
                ],
            },
        }
        return self._post(body)

    def _post(self, payload: dict) -> bool:
        if self._is_paused_webhook(self._settings.dingtalk_webhook_url):
            return False
        webhook = self._signed_webhook(self._settings.dingtalk_webhook_url)
        req = urllib.request.Request(
            url=webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if not (200 <= resp.status < 300):
                    return False
                raw = resp.read().decode("utf-8", errors="ignore")
                if not raw:
                    return True
                data = json.loads(raw)
                return int(data.get("errcode", 0)) == 0
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return False

    def _is_paused_webhook(self, webhook: str) -> bool:
        paused_tokens = {
            str(x).strip()
            for x in (self._settings.dingtalk_paused_webhook_tokens or [])
            if str(x).strip()
        }
        if not paused_tokens:
            return False
        try:
            parsed = urllib.parse.urlparse(webhook)
            query = urllib.parse.parse_qs(parsed.query)
            token = str((query.get("access_token") or [""])[0]).strip()
            if token and token in paused_tokens:
                return True
        except Exception:
            return False
        return False

    def _signed_webhook(self, webhook: str) -> str:
        secret = self._settings.dingtalk_signing_secret.strip()
        if not secret:
            return webhook

        timestamp = str(int(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))

        joiner = "&" if "?" in webhook else "?"
        return f"{webhook}{joiner}timestamp={timestamp}&sign={sign}"

    def _to_dingtalk_link(self, url: str) -> str:
        encoded = urllib.parse.quote(url, safe="")
        return f"dingtalk://dingtalkclient/page/link?url={encoded}&pc_slide=false"

    def _safe_text(self, value: Any, *, max_len: int) -> str:
        text = " ".join(str(value).split()) if value is not None else ""
        if not text:
            return "<none>"
        if len(text) <= max_len:
            return text
        return f"{text[: max_len - 3]}..."

    def _build_preview_block(self, extra_context: dict[str, Any] | None) -> str:
        lines = []
        if extra_context:
            raw = extra_context.get("preview_lines")
            if isinstance(raw, list):
                for item in raw[:6]:
                    lines.append(self._safe_text(item, max_len=180))
        if not lines:
            return ""
        joined = "\n".join(lines)
        return f"- preview:\n```text\n{joined}\n```"

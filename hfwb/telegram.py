import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class TelegramError(Exception):
    """Base exception for Telegram Bot API errors."""


class Blocked(TelegramError):
    """Raised when the bot has been blocked by the user (HTTP 403)."""


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from log records."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secret:
            return True
        if isinstance(record.msg, str) and self.secret in record.msg:
            record.msg = record.msg.replace(self.secret, "[REDACTED]")
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    str(arg).replace(self.secret, "[REDACTED]") if self.secret in str(arg) else arg
                    for arg in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (str(v).replace(self.secret, "[REDACTED]") if self.secret in str(v) else v)
                    for k, v in record.args.items()
                }
        return True


class TelegramClient:
    """Client for Telegram Bot API using httpx."""

    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._token = token
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = client
        self._own_client = client is None

        # Install redaction filter on httpx, root and module loggers
        redact_filter = SecretRedactingFilter(token)
        logging.getLogger().addFilter(redact_filter)
        logging.getLogger("httpx").addFilter(redact_filter)
        logging.getLogger("hfwb").addFilter(redact_filter)

    def __repr__(self) -> str:
        return "<TelegramClient token='[REDACTED]'>"

    def _redact(self, text: str) -> str:
        if self._token:
            return text.replace(self._token, "[REDACTED]")
        return text

    def _get_client(self, timeout: float = 10.0) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=timeout)

    def _post(self, method: str, json_data: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
        url = f"{self._base_url}/{method}"
        active_client = self._get_client(timeout=timeout)
        try:
            resp = active_client.post(url, json=json_data)
        except httpx.HTTPError as exc:
            redacted_msg = self._redact(str(exc))
            logger.error("Telegram request %s failed: %s", method, redacted_msg)
            raise TelegramError(f"Network error in {method}: {redacted_msg}") from None
        finally:
            if self._own_client and self._client is None:
                active_client.close()

        redacted_text = self._redact(resp.text)
        try:
            body = resp.json()
        except ValueError:
            body = {}

        description = body.get("description", "")
        error_code = body.get("error_code", resp.status_code)

        if resp.status_code == 403 or "blocked" in description.lower() or "blocked" in redacted_text.lower():
            redacted_desc = self._redact(description or redacted_text)
            logger.info("Bot blocked by user: %s", redacted_desc)
            raise Blocked(f"Blocked by user (status {resp.status_code}): {redacted_desc}")

        if resp.status_code != 200 or not body.get("ok"):
            redacted_desc = self._redact(description or redacted_text)
            logger.error("Telegram API error in %s (code %s): %s", method, error_code, redacted_desc)
            raise TelegramError(f"Telegram API error {error_code}: {redacted_desc}")

        return body.get("result")

    def get_updates(self, offset: int | None = None, timeout: int = 50) -> list[dict[str, Any]]:
        """Fetch updates using long polling."""
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset

        logger.debug("Calling getUpdates (offset=%s, timeout=%s)", offset, timeout)
        result = self._post("getUpdates", json_data=payload, timeout=float(timeout + 15))
        if isinstance(result, list):
            return result
        return []

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = "HTML",
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        """Send message to a chat."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if disable_web_page_preview:
            payload["disable_web_page_preview"] = True
        logger.debug("Sending message to chat_id=%s", chat_id)
        result = self._post("sendMessage", json_data=payload, timeout=15.0)
        if isinstance(result, dict):
            return result
        return {}

    def get_me(self) -> dict[str, Any]:
        """Fetch bot info."""
        result = self._post("getMe", json_data=None, timeout=10.0)
        if isinstance(result, dict):
            return result
        return {}

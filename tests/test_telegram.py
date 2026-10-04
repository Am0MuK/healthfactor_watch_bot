import json
import logging

import httpx
import pytest

from hfwb.telegram import Blocked, TelegramClient, TelegramError


def test_get_updates_success():
    fake_token = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
    expected_updates = [
        {"update_id": 100, "message": {"chat": {"id": 123}, "text": "/start"}},
        {"update_id": 101, "message": {"chat": {"id": 123}, "text": "/list"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert fake_token in str(request.url)
        assert "getUpdates" in str(request.url)
        return httpx.Response(200, json={"ok": True, "result": expected_updates})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        tg = TelegramClient(fake_token, client=http_client)
        updates = tg.get_updates(offset=100, timeout=10)
        assert updates == expected_updates


def test_send_message_success():
    fake_token = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["chat_id"] == 123
        assert body["text"] == "Hello"
        assert body["parse_mode"] == "HTML"
        assert body["disable_web_page_preview"] is True
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        tg = TelegramClient(fake_token, client=http_client)
        res = tg.send_message(123, "Hello")
        assert res["message_id"] == 1


def test_send_message_blocked_403():
    fake_token = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot was blocked by the user",
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        tg = TelegramClient(fake_token, client=http_client)
        with pytest.raises(Blocked):
            tg.send_message(123, "Hello")


def test_token_redacted_in_exceptions():
    fake_token = "SECRET_BOT_TOKEN_12345"

    def handler(request: httpx.Request) -> httpx.Response:
        # Simulate an HTTP error where httpx could include url with token
        return httpx.Response(
            500,
            text=f"Server error on https://api.telegram.org/bot{fake_token}/sendMessage",
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        tg = TelegramClient(fake_token, client=http_client)
        with pytest.raises(TelegramError) as exc_info:
            tg.send_message(123, "Hello")
        # Ensure raw token does NOT appear in exception text or string
        assert fake_token not in str(exc_info.value)
        assert "[REDACTED]" in str(exc_info.value) or fake_token not in str(exc_info.value)


def test_token_redacted_in_network_error():
    fake_token = "SECRET_BOT_TOKEN_67890"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Failed to connect to {request.url}")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        tg = TelegramClient(fake_token, client=http_client)
        with pytest.raises(TelegramError) as exc_info:
            tg.get_updates()
        assert fake_token not in str(exc_info.value)


def test_token_redacted_in_logs(caplog):
    fake_token = "SECRET_BOT_TOKEN_LOGS_999"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": []})

    transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.DEBUG), httpx.Client(transport=transport) as http_client:
        tg = TelegramClient(fake_token, client=http_client)
        tg.get_updates()

    for record in caplog.records:
        assert fake_token not in record.getMessage()


def test_get_me():
    fake_token = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "result": {"id": 123, "is_bot": True, "username": "test_bot"}}
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        tg = TelegramClient(fake_token, client=http_client)
        me = tg.get_me()
        assert me["username"] == "test_bot"

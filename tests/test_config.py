from pathlib import Path

import httpx
import pytest

from hfwb.__main__ import main
from hfwb.config import ConfigError, load_config


def test_load_config_from_file(tmp_path: Path):
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=test_token_123\n"
        "RPC_URL=https://arb.rpc.test\n"
        "RPC_URL_FALLBACK=https://fallback.rpc.test\n"
        f"HFWB_DB={tmp_path / 'custom.db'}\n"
    )

    cfg = load_config(env_file=env_file)
    assert cfg.telegram_bot_token == "test_token_123"
    assert cfg.rpc_urls == ["https://arb.rpc.test", "https://fallback.rpc.test"]
    assert cfg.db_path == tmp_path / "custom.db"


def test_load_config_missing_token(tmp_path: Path):
    env_file = tmp_path / "test.env"
    env_file.write_text("RPC_URL=https://arb.rpc.test\n")

    with pytest.raises(ConfigError) as exc_info:
        load_config(env_file=env_file)
    assert "TELEGRAM_BOT_TOKEN" in str(exc_info.value)


def test_load_config_missing_rpc_url(tmp_path: Path):
    env_file = tmp_path / "test.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=secret_token\n")

    with pytest.raises(ConfigError) as exc_info:
        load_config(env_file=env_file)
    assert "RPC_URL" in str(exc_info.value)
    # Never leak token in exception message
    assert "secret_token" not in str(exc_info.value)


def test_cli_missing_token_exit_code_2(tmp_path: Path, monkeypatch, capsys):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    monkeypatch.setenv("HFWB_ENV_FILE", str(empty_env))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("RPC_URL", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2

    captured = capsys.readouterr()
    assert "Missing" in captured.err or "TELEGRAM_BOT_TOKEN" in captured.err


def test_cli_check_flag(tmp_path: Path, monkeypatch, capsys):
    env_file = tmp_path / "check.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=token_abc_123\n"
        "RPC_URL=https://arb.rpc.test\n"
    )
    monkeypatch.setenv("HFWB_ENV_FILE", str(env_file))

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"id": 1, "is_bot": True, "username": "healthfactor_test_bot"}},
        )

    transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.Client(transport=transport)

    with monkeypatch.context() as m:
        m.setattr("hfwb.__main__.get_http_client", lambda: mock_client)
        exit_code = main(["--check"])
        assert exit_code == 0

    captured = capsys.readouterr()
    assert "healthfactor_test_bot" in captured.out
    # Token must not appear anywhere in stdout/stderr
    assert "token_abc_123" not in captured.out
    assert "token_abc_123" not in captured.err


def test_cli_verify_markets_flag_all_ok(monkeypatch, capsys):
    from hfwb.aave import AccountData
    from hfwb.markets import Market

    dummy_market = Market(
        protocol="aave_v3",
        chain="Arbitrum",
        chain_id=42161,
        name="Main market",
        address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        rpcs=("https://arb.rpc.test",),
    )

    with monkeypatch.context() as m:
        m.setattr("hfwb.__main__.load_markets", lambda: [dummy_market])
        m.setattr(
            "hfwb.__main__.read_market",
            lambda market, addr, client=None: AccountData(None, None, None, True, False),
        )
        exit_code = main(["--verify-markets"])
        assert exit_code == 0

    captured = capsys.readouterr()
    assert "ok" in captured.out
    assert "Arbitrum" in captured.out
    assert "Main market" in captured.out


def test_cli_verify_markets_flag_failure(monkeypatch, capsys):
    from hfwb.aave import ReadError
    from hfwb.markets import Market

    dummy_market = Market(
        protocol="aave_v3",
        chain="Polygon",
        chain_id=137,
        name="Main market",
        address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
        rpcs=("https://poly.rpc.test?key=SECRET_API_KEY",),
    )

    with monkeypatch.context() as m:
        m.setattr("hfwb.__main__.load_markets", lambda: [dummy_market])

        def failing_reader(market, addr, client=None):
            raise ReadError("RPC failed: https://poly.rpc.test?key=SECRET_API_KEY returned 500")

        m.setattr("hfwb.__main__.read_market", failing_reader)
        exit_code = main(["--verify-markets"])
        assert exit_code == 1

    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "Polygon" in captured.out
    assert "SECRET_API_KEY" not in captured.out
    assert "SECRET_API_KEY" not in captured.err

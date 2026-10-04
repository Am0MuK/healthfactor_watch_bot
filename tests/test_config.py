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


def test_cli_stats_empty_db_no_token(tmp_path: Path, monkeypatch, capsys):
    from hfwb.store import init_db
    from tests.test_format import assert_no_plan7_forbidden_words

    empty_db = tmp_path / "empty_stats.db"
    init_db(empty_db)  # an existing but empty database; a missing one is an error (see below)
    empty_env = tmp_path / "empty_token.env"
    empty_env.write_text(f"HFWB_DB={empty_db}\n")

    monkeypatch.setenv("HFWB_ENV_FILE", str(empty_env))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("RPC_URL", raising=False)

    exit_code = main(["--stats"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "Chats with tracked addresses: 0" in captured.out
    assert "Watches: 0" in captured.out
    assert "Depeg alerts: 0" in captured.out
    assert "Custom levels chats: 0" in captured.out
    assert "Pro interest:" in captured.out
    assert "total: 0" in captured.out
    assert "addresses: 0" in captured.out
    assert "faster: 0" in captured.out
    assert "levels: 0" in captured.out
    assert "email: 0" in captured.out
    assert "discord: 0" in captured.out
    assert_no_plan7_forbidden_words(captured.out)


def test_cli_stats_seeded_db(tmp_path: Path, monkeypatch, capsys):
    from decimal import Decimal

    from hfwb.store import (
        add_depeg_subs,
        add_tracked_address,
        add_watch,
        init_db,
        set_levels,
        set_pro_interest,
    )
    from tests.test_format import assert_no_plan7_forbidden_words

    db_path = tmp_path / "seeded_stats.db"
    init_db(db_path)
    chat1 = 12345
    chat2 = 67890
    addr1 = "0x1111111111111111111111111111111111111111"
    addr2 = "0x2222222222222222222222222222222222222222"

    add_tracked_address(db_path, chat1, addr1)
    add_tracked_address(db_path, chat1, addr2)
    add_tracked_address(db_path, chat2, addr1)
    add_watch(db_path, chat1, addr1, "m1")
    add_watch(db_path, chat1, addr2, "m2")
    add_watch(db_path, chat2, addr1, "m1")
    add_depeg_subs(db_path, chat1, ["USDC", "USDT"])
    set_levels(db_path, chat1, (Decimal("1.6"), Decimal("1.2")))
    set_pro_interest(db_path, chat1, ["addresses", "faster"], 1000.0)
    set_pro_interest(db_path, chat2, [], 1000.0)

    empty_env = tmp_path / "empty_token.env"
    empty_env.write_text(f"HFWB_DB={db_path}\n")
    monkeypatch.setenv("HFWB_ENV_FILE", str(empty_env))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("RPC_URL", raising=False)

    exit_code = main(["--stats"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "Chats with tracked addresses: 2" in captured.out
    assert "Watches: 3" in captured.out
    assert "Depeg alerts: 2" in captured.out
    assert "Custom levels chats: 1" in captured.out
    assert "Pro interest:" in captured.out
    assert "total: 2" in captured.out
    assert "addresses: 1" in captured.out
    assert "faster: 1" in captured.out
    assert "levels: 0" in captured.out
    assert "email: 0" in captured.out
    assert "discord: 0" in captured.out

    # No chat IDs, no addresses in output
    assert str(chat1) not in captured.out
    assert str(chat2) not in captured.out
    assert addr1 not in captured.out
    assert addr2 not in captured.out
    assert "0x" not in captured.out

    assert_no_plan7_forbidden_words(captured.out)



def test_cli_stats_missing_db_fails_and_does_not_create_it(tmp_path: Path, monkeypatch, capsys):
    missing = tmp_path / "typo.db"
    env = tmp_path / "e.env"
    env.write_text(f"HFWB_DB={missing}\n")
    monkeypatch.setenv("HFWB_ENV_FILE", str(env))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    assert main(["--stats"]) == 1
    assert not missing.exists()  # a wrong path must not silently create an empty database
    assert "not found" in capsys.readouterr().err.lower()


def test_cli_stats_is_read_only_and_tolerates_an_older_schema(tmp_path: Path, monkeypatch, capsys):
    import sqlite3

    from hfwb.store import init_db

    old_db = tmp_path / "old.db"
    init_db(old_db)
    con = sqlite3.connect(old_db)
    con.execute("DROP TABLE pro_interest")
    con.execute("PRAGMA user_version = 3")
    con.commit()
    con.close()

    env = tmp_path / "e.env"
    env.write_text(f"HFWB_DB={old_db}\n")
    monkeypatch.setenv("HFWB_ENV_FILE", str(env))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "total: 0" in out  # the missing table counts as zero

    con = sqlite3.connect(old_db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3  # no migration was run
    assert con.execute("SELECT name FROM sqlite_master WHERE name='pro_interest'").fetchone() is None
    con.close()

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from hfwb.aave import checksum_address

logger = logging.getLogger(__name__)

DEFAULT_ENV_FILE = ".env"
DEFAULT_DB_PATH = "/data/services/healthfactor_watch_bot/bot.db"
DEFAULT_HEARTBEAT = "/data/services/healthfactor_watch_bot/heartbeat"
DEFAULT_POLL_INTERVAL = 300.0


class ConfigError(Exception):
    """Raised when configuration is invalid or missing required variables."""


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    rpc_urls: list[str]
    db_path: Path
    heartbeat_file: Path
    donate_address: str | None = None
    donate_note: str | None = None
    poll_interval: float = DEFAULT_POLL_INTERVAL


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    res: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        res[k] = v
    return res


def get_db_path(env_file: Path | str | None = None) -> Path:
    """Resolve database path from environment and env-file without requiring credentials."""
    if env_file is not None:
        target_path = Path(env_file)
    else:
        target_path = Path(os.environ.get("HFWB_ENV_FILE", DEFAULT_ENV_FILE))

    file_vars = _parse_env_file(target_path)
    merged_env = {**file_vars, **os.environ}
    db_str = merged_env.get("HFWB_DB", DEFAULT_DB_PATH).strip()
    return Path(db_str)


def load_config(env_file: Path | str | None = None) -> Config:
    """Load configuration from environment file and environment variables."""
    if env_file is not None:
        target_path = Path(env_file)
    else:
        target_path = Path(os.environ.get("HFWB_ENV_FILE", DEFAULT_ENV_FILE))

    file_vars = _parse_env_file(target_path)
    merged_env = {**file_vars, **os.environ}

    token = merged_env.get("TELEGRAM_BOT_TOKEN", "").strip()
    rpc_url = merged_env.get("RPC_URL", "").strip()
    rpc_fallback = merged_env.get("RPC_URL_FALLBACK", "").strip()
    db_str = merged_env.get("HFWB_DB", DEFAULT_DB_PATH).strip()
    heartbeat_str = merged_env.get("HFWB_HEARTBEAT", DEFAULT_HEARTBEAT).strip()

    missing: list[str] = []
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not rpc_url:
        missing.append("RPC_URL")

    if missing:
        raise ConfigError(f"Missing required configuration variables: {', '.join(missing)}")

    rpc_urls = [rpc_url]
    if rpc_fallback:
        rpc_urls.append(rpc_fallback)

    raw_donate_addr = merged_env.get("DONATE_ADDRESS", "").strip()
    donate_address: str | None = None
    if raw_donate_addr:
        try:
            donate_address = checksum_address(raw_donate_addr)
        except ValueError:
            logger.warning("Invalid DONATE_ADDRESS configured; treating as not configured")
            donate_address = None

    raw_donate_note = merged_env.get("DONATE_NOTE", "").strip()
    donate_note: str | None = None
    if raw_donate_note and len(raw_donate_note) <= 80:
        donate_note = raw_donate_note
    else:
        donate_note = None

    return Config(
        telegram_bot_token=token,
        rpc_urls=rpc_urls,
        db_path=Path(db_str),
        heartbeat_file=Path(heartbeat_str),
        donate_address=donate_address,
        donate_note=donate_note,
        poll_interval=DEFAULT_POLL_INTERVAL,
    )

# Health Factor Watch Bot (`healthfactor_watch_bot`)

**Try it:** [@healthfactor_watch_bot](https://t.me/healthfactor_watch_bot) on Telegram. Send `/watch 0x...` with any wallet address.

A Telegram bot that monitors Aave V3 (20 EVM chains) and Aave V4 (19 spokes across 4 chains) borrow positions and alerts users when their health factor drops below safe thresholds.

## Overview

- **Protocols & Markets**:
  - Aave V3 deployments across 20 EVM chains.
  - Aave V4 spokes across Ethereum, Arc, Avalanche, and Base (19 spokes).
  - Both protocols read account data via `getUserAccountData(address)` (`0xbf92857c`).
- **Read-only**: The bot does not require, store, or accept private keys. It performs no transactions and cannot sign messages.
- **Tone**: Colored status emoji and a short health-factor gauge, informational only. Every alert includes a disclaimer that messages are not financial advice. The bot never offers trading or collateral management advice.

## Commands

- `/start` - Introduction and usage instructions.
- `/watch <address>` - Scan all supported markets for an Ethereum address and monitor active positions (max 3 addresses per chat).
- `/rescan <address>` - Scan all supported markets on demand to detect new positions (rate limit: once per 5 minutes per chat).
- `/levels [levels|reset]` - View or configure custom alert levels for this chat (e.g. `/levels 1.5 1.3 1.15 1.05` or `/levels reset`).
- `/list` - Display all monitored addresses grouped with their active market positions and alert states.
- `/remove <address>` - Stop monitoring an address and all its market positions.
- `/privacy` - View privacy notice and data retention terms.
- `/delete` - Permanently remove all data and watches for this chat.
- `/help` - List available commands.

## Supported Markets

All 39 deployments listed in `hfwb/markets.json`:

| Chain | Protocol | Market | Best effort (single RPC) |
|---|---|---|---|
| Arbitrum | Aave V3 | Main market | No |
| Arc | Aave V4 | Forex Spoke | No |
| Arc | Aave V4 | Main Spoke | No |
| Avalanche | Aave V3 | Main market | No |
| Avalanche | Aave V4 | AVAX Correlated Spoke | No |
| Avalanche | Aave V4 | Forex Spoke | No |
| Avalanche | Aave V4 | Main Spoke | No |
| BNB | Aave V3 | Main market | No |
| Base | Aave V3 | Main market | No |
| Base | Aave V4 | Mag7 Spoke | No |
| Celo | Aave V3 | Main market | No |
| Ethereum | Aave V3 | Main market | No |
| Ethereum | Aave V4 | Bluechip Spoke | No |
| Ethereum | Aave V4 | Ethena Correlated Spoke | No |
| Ethereum | Aave V4 | Ethena Ecosystem Spoke | No |
| Ethereum | Aave V4 | EtherFi eSpoke | No |
| Ethereum | Aave V4 | Forex Spoke | No |
| Ethereum | Aave V4 | Gold Spoke | No |
| Ethereum | Aave V4 | Kelp eSpoke | No |
| Ethereum | Aave V4 | Lido eSpoke | No |
| Ethereum | Aave V4 | Lombard BTC Spoke | No |
| Ethereum | Aave V4 | Main Spoke | No |
| Ethereum | Aave V4 | PAXG Gold Spoke | No |
| Ethereum | Aave V4 | USDG Maple eSpoke | No |
| Ethereum | Aave V4 | USDG Pendle Spoke | No |
| Gnosis | Aave V3 | Main market | No |
| Linea | Aave V3 | Main market | No |
| Mantle | Aave V3 | Main market | No |
| MegaEth | Aave V3 | Main market | Yes |
| Metis | Aave V3 | Main market | No |
| Monad | Aave V3 | Main market | Yes |
| Optimism | Aave V3 | Main market | No |
| Plasma | Aave V3 | Main market | Yes |
| Polygon | Aave V3 | Main market | No |
| Scroll | Aave V3 | Main market | No |
| Soneium | Aave V3 | Main market | Yes |
| Sonic | Aave V3 | Main market | Yes |
| XLayer | Aave V3 | Main market | No |
| ZkSync | Aave V3 | Main market | Yes |

## What an Alert Looks Like

```text
🟠 Aave V3 · Base
Health factor 1.18  ▰▰▱▱▱▱▱▱▱▱
Fell below 1.2
About 15.3% of collateral value can fall before liquidation at 1.00 (if debt value stays the same).
Collateral $10,000 · Debt $7,000
Account: 0x794a…14ad

Informational only, not financial advice.
```

### Default Alert Levels

| Emoji | Threshold | Status | Action |
|---|---|---|---|
| 🟡 | 1.40 | L1 | Alert once on downward crossing |
| 🟠 | 1.20 | L2 | Alert once on downward crossing |
| 🔴 | 1.10 | L3 | Critical alert; repeats every 30 min |
| 🚨 | 1.05 | L4 | Severe danger alert; repeats every 30 min |
| 🟢 | 1.43 | recovery | Recovery notification when back above 1.4 (with 0.03 hysteresis) |

## Alert Levels and Hysteresis

Alert levels are configured per Telegram chat (not per wallet address). By default, every chat uses `DEFAULT_LEVELS = (1.4, 1.2, 1.1, 1.05)` with `ok` (HF >= 1.40), `L1` (< 1.40), `L2` (< 1.20), `L3` (< 1.10), and `L4` (< 1.05).

### Custom Levels (`/levels`)

Users can customize alert thresholds for their chat using `/levels <l1> <l2> ...` (or separated by commas, e.g. `/levels 1.5 1.3 1.15 1.05` or `/levels 1.6,1.4,1.2`), and restore defaults with `/levels reset`.

Validation rules for custom levels:
- **Count**: 2 to 5 levels.
- **Order**: Strictly descending (e.g. 1.8 > 1.4 > 1.2).
- **Range**: Each level between 1.01 and 5.0 inclusive (a level may be set above 1.4).
- **Minimum gap**: At least 0.04 between consecutive levels.
- **Precision**: At most 2 decimal places per value.

### Re-Arm Behavior

When alert levels are changed or reset with `/levels`, all current watch states for that chat are immediately reset to `ok` and alert timestamps are cleared in the database. During the next poll cycle, the bot re-evaluates all active positions against the new thresholds, so an alert may arrive immediately if a position is currently below a configured level.

### Transition Rules

- **Downward crossings**: An automatic alert is sent each time health factor crosses downward into a new level. A downward jump over several levels sends a single alert naming the deepest level reached (e.g. `Fell below 1.05`).
- **Dynamic hysteresis**: Hysteresis is calculated from the levels: `hyst = min(0.03, 0.6 * min_gap)` where `min_gap` is the smallest difference between consecutive levels. For the default levels this is exactly 0.03; for a minimum gap of 0.04 it is 0.024. A less severe state is re-armed only when health factor rises above that level + `hyst`:
  - With default levels, recovery from `L4` to `L3` requires HF >= 1.08; `L3` to `L2` requires HF >= 1.13; `L2` to `L1` requires HF >= 1.23; `L1` back to `ok` requires HF >= 1.43.
- **Emoji ranking**: Alert emoji are determined by distance from the lowest level (`r = n - k`):
  - `r = 0` (lowest level, `Ln`): 🚨
  - `r = 1`: 🔴
  - `r = 2`: 🟠
  - `r >= 3`: 🟡
- **Alert frequency**: While in the lowest two levels (`k >= n - 1`, e.g. `L3` and `L4` for 4 levels, or both `L1` and `L2` for 2 levels), reminders repeat every 30 minutes.
- **Recovery notification**: A recovery alert is dispatched only when health factor returns to `ok` (`HF >= levels[0] + hyst`), naming the top threshold. Upward moves between intermediate levels do not send notifications.
- **No debt**: When an account has no debt (health factor sentinel `2^256 - 1`), it is mapped to `ok` (with a recovery notification if previously non-ok).
- **RPC failures**: On RPC node failures, the previous known state is preserved. An internal failure counter increments per `(market, address)` pair; after 3 consecutive failures an error is recorded in server logs without user-facing spam.

## Polling Intervals and Backoff

Positions are polled every 1 to 5 minutes depending on how close the position is to a level:

| Interval | Condition |
|---|---|
| 60 s (near) | Health factor < 1.15, or distance to next downward level < 0.10 |
| 120 s (approaching) | Distance to next downward level < 0.30, or health factor < 1.40 |
| 300 s (safe) | All other positions, or accounts with no debt |

When multiple chats watch the same position with different alert levels, the bot polls at the shortest calculated interval.

### Failure Backoff

If an RPC read fails for a position, polling backs off progressively to avoid hammering nodes:

| Failure Streak | Retry Interval |
|---|---|
| 1 failure | 60 s |
| 2 consecutive failures | 120 s |
| 3 consecutive failures | 240 s |
| 4 or more consecutive failures | 300 s |

The failure streak resets to 0 immediately upon the next successful read.

## Limits

- **Addresses per chat**: Maximum 3 monitored addresses per Telegram chat.
- **Positions per chat**: Maximum 30 market positions per Telegram chat.
- **Command rate limiting**: Maximum 10 commands per minute per chat.
- **Rescan rate limiting**: Maximum 1 `/rescan` command every 5 minutes per chat.
- **Concurrency**: Polling concurrency is capped at 5 simultaneous requests overall and 3 simultaneous requests per RPC host.

## Daily Rescan

A background worker checks tracked addresses once every 24 hours. When an address opens a position in a new market, the bot automatically adds a watch for that market and notifies the chat (`Found a new position: ...`).

## Privacy

- The database stores only `chat_id`, monitored `address`, `market_key`, alert `state`, `last_alert_ts`, `fail_count`, and `last_scan_ts`.
- No user names, handles, or message contents are recorded.
- Retention is strictly "until `/remove` or `/delete`".
- If Telegram reports HTTP 403 ("bot was blocked by the user"), all rows for that chat are automatically deleted.

## Configuration and Secrets

Configuration is loaded from the file specified in `HFWB_ENV_FILE` (default: `.env` in the working directory; with Docker Compose the same file is passed as `env_file`) and environment variables:

| Variable | Description | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token | Yes |
| `HFWB_DB` | Path to SQLite database (default: `/data/services/healthfactor_watch_bot/bot.db`) | No |
| `HFWB_HEARTBEAT` | Heartbeat timestamp file path (default: `/data/services/healthfactor_watch_bot/heartbeat`) | No |

Secrets are never printed in exception messages, CLI outputs, or application logs.

## How to Run

### Local Environment

1. Create a Python 3.12 virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -e .
   ```

2. Validate configuration and check bot connectivity:
   ```bash
   .venv/bin/hfwb --check
   ```
   This reaches the Telegram `getMe` endpoint and prints only the bot username.

3. Live verification of all market endpoints:
   ```bash
   .venv/bin/hfwb --verify-markets
   ```
   Tests an `eth_call` on `0x000000000000000000000000000000000000dEaD` for every market and prints status without printing RPC URLs or keys.

4. Run the bot:
   ```bash
   .venv/bin/hfwb
   ```

### Docker Compose

A `Dockerfile` and `docker-compose.yml` are provided for deployment:
- Uses `python:3.12-slim` as a non-root user.
- Enforces a 256 MB memory limit.
- Runs with no exposed ports (long polling).
- Container healthcheck monitors the heartbeat timestamp file updated on each poll cycle.

## Limitations

- Positions are polled every 1 to 5 minutes depending on how close the position is to a level with public RPC endpoints. A fast market move can cross a level between two polls, so this is an early-warning tool, not a guarantee.
- Markets marked "best effort" in the table have a single public RPC endpoint.
- Only Aave V3 and Aave V4 are covered. Other lending protocols are not supported.
- USD amounts are shown for Aave V3 only. Aave V4 alerts show the health factor.
- "Distance to liquidation" assumes all collateral prices fall together and the debt value stays the same. It is an estimate.

## License

MIT. See `LICENSE`.

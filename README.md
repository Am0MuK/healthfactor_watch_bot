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
- `/whatif <asset> <±%> [...]` - Health factor of your watched Aave V3 positions after a price move (e.g. `/whatif ETH -20`, `/whatif BTC -10 ETH -15`, `/whatif market -30`).
- `/levels [levels|reset]` - View or configure custom alert levels for this chat (e.g. `/levels 1.5 1.3 1.15 1.05` or `/levels reset`).
- `/depeg [on|off [symbols...]]` - View status or configure opt-in stablecoin depeg alerts (e.g. `/depeg on` for all 12 tokens, `/depeg on USDC USDT`, or `/depeg off`).
- `/donate` - View voluntary donation address and network info.
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
Liquidation if, with other prices unchanged:
• WETH −25.3% (to $1,867)
• cbBTC −38.4% (to $36,951)
All crypto together −15.3%
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

## Stablecoin Depeg Alerts

The bot provides optional, opt-in alerts for 12 major stablecoins when their price deviates from 1.00 USD (either above or below peg). This feature is disabled by default.

### Supported Stablecoins (Ethereum)

- **USDC**: `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48`
- **USDT**: `0xdAC17F958D2ee523a2206206994597C13D831ec7`
- **DAI**: `0x6B175474E89094C44Da98b954EedeAC495271d0F`
- **USDS**: `0xdC035D45d973E3EC169d2276DDab16f1e407384F`
- **GHO**: `0x40D16FC0246aD3160Ccc09B8D0D3A2cD28aE6C2f`
- **USDe**: `0x4c9EDD5852cd905f086C759E8383e09bff1E68B3`
- **PYUSD**: `0x6c3ea9036406852006290770BEdFcAbA0e23A0e8`
- **RLUSD**: `0x8292Bb45bf1Ee4d140127049757C2E0fF06317eD`
- **USDG**: `0xe343167631d89B6Ffc58B88d6b7fB0228795491D`
- **frxUSD**: `0xCAcd6fd266aF91b8AeD52aCCc382b4e165586E29`
- **crvUSD**: `0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E`
- **FDUSD**: `0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409`

### Alert Thresholds and Rules

Alert levels are defined by absolute price deviation from $1.00:

| Emoji | Level | Deviation Threshold | Action |
|---|---|---|---|
| 🟡 | D1 | 0.5% ($0.9950 or $1.0050) | Alert once on crossing |
| 🟠 | D2 | 1.0% ($0.9900 or $1.0100) | Alert once on crossing |
| 🔴 | D3 | 2.0% ($0.9800 or $1.0200) | Critical alert; repeats every 60 min |
| 🚨 | D4 | 5.0% ($0.9500 or $1.0500) | Severe danger alert; repeats every 60 min |
| 🟢 | recovery | < 0.3% ($0.9970 to $1.0030) | Recovery notification when back within 0.3% |

- **Escalation confirmation**: Moving to a higher severity level requires two different price data points in a row (not the same data point polled twice) at or above that level. The confirmed deviation is the lower of the two, so a single glitchy print never alerts and a fast collapse (for example 1% then 3% then 7%) still alerts at every step, one data point behind the price.
- **Hysteresis and step-down**: Moving down from level $D_k$ to $D_{k-1}$ requires the deviation to drop below $0.8 \times \text{threshold}_k$ (e.g. leaving D4 for D3 requires deviation < 4.0%; D3 to D2 < 1.6%; D2 to D1 < 0.8%). Step-downs occur immediately and do not generate alert messages.
- **Recovery**: Returning to normal (`ok`) occurs immediately once the price deviation drops below 0.3%.
- **Price above peg**: Deviations above peg are tracked identically to deviations below peg.

### Commands

- `/depeg` - View current subscription status and tracked token states.
- `/depeg on` - Subscribe to alerts for all 12 supported stablecoins.
- `/depeg on <symbols>` - Subscribe to specific stablecoins (case-insensitive, separated by spaces or commas, e.g. `/depeg on USDC USDT` or `/depeg on usds,pyusd`).
- `/depeg off` - Turn off all stablecoin depeg alerts for this chat.
- `/depeg off <symbols>` - Unsubscribe from specific stablecoins.

### Data Source and Limitations

- **Aggregated Market Price**: Prices come from DefiLlama (`coins.llama.fi`), which is polled every 60 seconds. It is an aggregated market price and can differ across exchanges, liquidity pools, and chains.
- **Delay**: DefiLlama refreshes each token about every 5 minutes, and some tokens are normally 11 to 15 minutes old. With the two-data-point confirmation, expect an alert roughly 5 to 15 minutes after a move shows up in the data. This is a slow-moving depeg warning, not a real-time trading signal.
- **Canonical Ethereum Addresses**: Only canonical Ethereum contract addresses are polled; bridged or wrapped representations on L2s and alt-L1s may trade at slightly different values.
- **Confidence and Staleness**: A price reading is only considered usable if DefiLlama reports confidence $\ge 0.9$ and the timestamp is not older than 20 minutes. If a token's data is stale or unavailable, no state change or alert happens for it that cycle, and an entirely empty response from the source is treated as an error (logged), never as "no depeg".
- **Aave Oracle Price**: When an alert is dispatched, the bot queries the Ethereum Aave Oracle contract (`0x54586bE62E3c3580375aE3723C145253060Ca0C2`) on a best-effort basis for informational context.
- **Oracle Blind Spots**: The Ethereum Aave Oracle hardcodes GHO to 1.0 USD and proxies USDe to USDT's price feed; alerts for GHO and USDe display `Aave oracle: not independent (fixed or proxy price)` instead of a numeric price.
- **Yield-bearing Tokens Excluded**: Yield-bearing assets (e.g. sUSDe, sDAI) are not pegged to $1.00 USD and are out of scope.

## Price Moves and `/whatif` (Aave V3)

For Aave V3 positions the bot also reads the position asset by asset: balances, liquidation thresholds (including the eMode category threshold, Aave V3.2+ layout) and Aave's own oracle prices. It reads only the reserves set in the account's `getUserConfiguration` bitmap, so a typical position needs about 8 calls.

- **Tie-out guard.** The bot recomputes collateral, debt and health factor from these numbers and compares them with `getUserAccountData`. If any of the three differs by more than 0.5 %, it shows no per-asset numbers and falls back to the plain alert. Wrong numbers are worse than none.
- **Liquidation lines** (in `/watch` and in alerts): how far each asset can move on its own before the health factor reaches 1.00, at most 3 assets, smallest move first. A borrowed volatile asset shows a rise (`WETH +40%`). Collateral that covers the debt by itself shows "safe even at $0". "All crypto together" moves every non-stablecoin asset by the same percentage (stablecoins, euro tokens and gold stay fixed); a correlated loop such as wstETH collateral against WETH debt shows "no liquidation". Stablecoins and positions under $1 are left out.
- **`/whatif`**: up to 5 pairs. `ETH` and `BTC` include their wrapped and staked versions (WETH, wstETH, weETH, rETH, ...; WBTC, cbBTC, tBTC, LBTC, ...); any other symbol matches exactly; `market` moves all crypto. One `/whatif` per chat every 10 seconds.
- **Limits.** Prices are Aave's oracle prices, not exchange prices. Debt amounts stay fixed (interest keeps accruing in reality). A same-asset loop (WETH supplied and borrowed) cannot be liquidated by the WETH price, only by interest. Aave V4 positions are not covered yet. Every read has a 10-second budget, so a slow RPC cannot hold up alerts.

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

## Donations

The bot is free and open source. If it is useful to you, you can support it with a voluntary donation via the `/donate` command. Donations grant no extra features or guarantees.

Donation settings are optional and configured by the operator via environment variables:
- `DONATE_ADDRESS`: Ethereum address (0x-prefixed, 40 hex characters) displayed to users.
- `DONATE_NOTE`: Short optional note (max 80 characters, e.g. "USDC on Base").

The public repository contains no donation address, and the bot stores nothing when `/donate` is invoked.

### Usage Statistics (`--stats`)

Aggregate statistics can be printed directly from the database without starting the bot, reaching the network, or needing a bot token:
```bash
.venv/bin/hfwb --stats
```
This prints the number of chats with tracked addresses, watches, depeg alerts, and custom levels chats. No chat IDs or wallet addresses are output.

## Privacy

- The database stores only `chat_id`, monitored `address`, `market_key`, alert `state`, `last_alert_ts`, `fail_count`, `last_scan_ts`, and subscribed stablecoin symbols.
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
| `DONATE_ADDRESS` | Optional Ethereum address for voluntary donations | No |
| `DONATE_NOTE` | Optional note displayed with donation address (max 80 chars) | No |

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
   This reaches the Telegram `getMe` endpoint and prints only the bot username and donation configuration status.

3. Live verification of all market endpoints:
   ```bash
   .venv/bin/hfwb --verify-markets
   ```
   Tests an `eth_call` on `0x000000000000000000000000000000000000dEaD` for every market and prints status without printing RPC URLs or keys.

4. Inspect database statistics:
   ```bash
   .venv/bin/hfwb --stats
   ```
   Reads the local database and prints summary counts without network or token requirements.

5. Run the bot:
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
- When per-asset data is not available (Aave V4, or the tie-out guard fails), alerts fall back to "About X% of collateral value can fall", which assumes all collateral prices fall together and the debt value stays the same. It is an estimate.

## License

MIT. See `LICENSE`.

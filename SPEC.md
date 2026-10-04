# DeFi Health Alert Bot — MVP spec

Public Telegram bot. A user sends a wallet address; the bot watches its Aave V3 health factor and sends an alert when it gets risky. Read-only: no private keys, no signing, no transactions. English only.

## Scope (MVP)
- Markets every Aave V3 deployment (20 EVM chains) and every Aave V4 Spoke (Ethereum, Arc, Avalanche, Base; 19 spokes), listed in `hfwb/markets.json` (live-verified). Both protocols use the selector `0xbf92857c` (`getUserAccountData(address)`): V3 returns 6 words (collateral, debt in USD 1e8; HF word 5, WAD), V4 returns 7 words (HF is word 2, WAD; `borrowCount` word 6; no-debt = HF 2^256-1). A user's position is per market, so alerts are per market and name it (e.g. `Aave V3 · Base`, `Aave V4 · Ethereum · Main Spoke`). V4 alerts show HF only (USD value units not confirmed). Other lending protocols are out of scope.
- `/watch <address>` scans all markets once and watches only those where the address has a position; `/rescan <address>` and an automatic daily rescan pick up new positions (the user is told when one is found). Limit 3 addresses per chat, 30 market positions per chat.
- Commands: `/start`, `/watch <address>`, `/list`, `/rescan <address>`, `/remove <address>`, `/privacy`, `/delete` (wipe everything about the chat), `/help`.
- Alert levels an automatic alert each time HF falls to 1.4, 1.2, 1.1 and 1.05. States: `ok` (HF >= 1.4) / `L1` (< 1.4) / `L2` (< 1.2) / `L3` (< 1.1) / `L4` (< 1.05). Each downward crossing alerts once; a jump over several levels sends one alert for the deepest level reached. Hysteresis 0.03 (so 1.05 and 1.1 stay distinct): a level re-arms only above level + 0.03. While in L3 or L4 repeat every 30 min (liquidation is at 1.0). Recovery message when back to `ok` (HF >= 1.43). No separate 1.6 warning.
- No debt (HF = 2^256-1) is "no debt", not an alert and not an error.
- Text is informational only. Never "buy", "sell" or "add collateral" advice. Every alert carries a short "not financial advice" footer.

## Out of scope (phase 2)
Stablecoin depeg alerts, Uniswap LP out-of-range alerts, other chains, per-user thresholds.

## Design rules
- Long-polling (getUpdates). No inbound port, nothing behind Caddy.
- SQLite at `/data/services/healthfactor_watch_bot/bot.db` (chat_id, address, state, last_alert_ts). No names, usernames or message text stored.
- Limits: 3 addresses per chat, 10 commands/min per chat, address checksum-validated, EOA/contract both allowed.
- Poll loop every 5 min, one RPC call per distinct address (shared across chats), small concurrency cap.
- RPC: public endpoints, ordered primary then fallback (see `hfwb/markets.json`).
- Fail loudly: an RPC error, empty or reverted response never evaluates to ok/0. The address keeps its previous state, and after 3 consecutive failures the bot logs an error (no per-user spam).
- Secrets only in `.env` next to the code (git-ignored, mode 600): `TELEGRAM_BOT_TOKEN`, `RPC_URL`. Logs redact both.

## Privacy
Stores chat_id + address + alert state only. `/delete` removes all rows for the chat. `/privacy` says exactly this. Wallet addresses are public on chain but still personal data under GDPR, so retention is "until /remove or /delete", plus auto-removal of chats where Telegram returns "bot was blocked".

## Tests (written first)
State machine transitions incl. hysteresis and repeat; no-debt sentinel; RPC failure keeps state; address validation; per-chat limits; `/delete` wipes; message formatting has no advice wording; secrets never appear in logs.


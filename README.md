# Jafar Engine

The JAFAR Intelligence Pipeline — end-to-end market intelligence, signals, execution rails (human-in-the-loop), and observability for trading workflows.

## Overview

Jafar Engine ingests multi-source market data, computes factor-based signals, applies a Kronos forecast layer as a gate/size modifier, surfaces candidates via discovery, tracks momentum fades, and turns actionable crypto signals into human-approved trade proposals. Runs as a long-lived service (hourly full runs, 15-min light refreshes), exposes a Flask dashboard, and integrates with Telegram for alerts/commands.

## Architecture

- **Data layer** (`build_snapshot.py`): builds per-asset snapshots from yfinance/market data, writes `snapshot_<ASSET>.json`, `snapshot_summary.json`, `signals.json`.
- **Signals** (`signals.py`): composite scoring, action (BUY/ACCUMULATE/SELL/REDUCE/HOLD), entry/SL/TP levels, sizing. Kronos is gate-only by default (`KRONOS_FACTOR_WEIGHT=0`) — used as a gate/size modifier (never forces auto-trades).
- **Kronos** (`kronos_layer.py`, `kronos_src/`): subprocess forecast layer in isolated venv; auto-runs on full builds and light refreshes; logs to `kronos_track.json`, outputs `forecasts.json` and per-snapshot `kronos_forecast`.
- **Trader** (`trader.py`): human-in-the-loop execution for crypto only. Creates pending proposals; approvals via dashboard (`/api/proposals`, approve/reject) or Telegram inline buttons. Modes: `paper` (default), `testnet`, `live` (requires confirmation). Whitelist enforced (BTCUSDT/ETHUSDT/SOLUSDT/ZECUSDT).
- **Service** (`run_service.py`): long-lived loop — full pipeline hourly, light price refresh every 15 min, alerts, Telegram bot thread. Persists `service_state.json`, `pending_orders.json`, `trade_log.json` (gitignored).
- **Discovery** (`discover.py`): MEXC USDT-M top gainers/losers (24h), cached in `discovery.json`.
- **Fade Tracker** (`fade_tracker.py`): tracks when top gainers lose momentum (peak >= 20% with >=15pt giveback/rollover), persists `fade_track.json`, emits per-symbol Telegram alerts with cooldowns.
- **Dashboard** (`dashboard/app.py`): Flask UI on `:5000` (signals, proposals, state).
- **Telegram** (`telegram_bot.py`, `alerts.py`): commands (`/status`, `/signals`, `/signal <A>`, `/price <A>`, `/news <A>`, `/discover`, `/fades`, `/positions`, `/run`, `/calendar`) and alerts with per-key cooldowns.
- **Alerts** (`alerts.py`): Telegram delivery with recipient adoption (`/start`) and cooldown state in `alert_state.json`.

## Quick Start

### Prereqs
- Python 3.14 (system). Do not use the repo’s `.venv/` (Windows venv).
- Packages: flask, pandas, numpy, yfinance, torch (preinstalled in environment). Kronos uses its own isolated venv at `~/.venvs/trading-kronos/` (pinned pandas 2.2.2).

### Commands
```bash
cd /mnt/jafar-data/CODE/TradingPipeline

# Full data layer run (auto-runs Kronos at end)
python3 build_snapshot.py

# Intelligence brief (HTML)
python3 "generate brief.py"

# Print entry/exit signals from existing snapshots
python3 signals.py

# Start Flask dashboard (:5000)
bash start_dashboard.sh

# Expose public tunnel (Cloudflare quick tunnel)
bash share_dashboard.sh  # prints https://*.trycloudflare.com

# Long-lived service (Railway/Docker style)
python3 run_service.py

# Trader CLI
python3 trader.py status
python3 trader.py plan
python3 trader.py list
python3 trader.py approve <id>
python3 trader.py reject <id>

# Discovery/Fades
python3 discover.py
python3 fade_tracker.py scan
python3 fade_tracker.py update
```

## Configuration

Primary config via `.env` (see `.env.example`). Key knobs:
- `KRONOS_ENABLED` (default 1), `KRONOS_FACTOR_WEIGHT` (default 0 = gate-only), `KRONOS_PRED_LEN`, `KRONOS_GATE`, `KRONOS_GATE_THRESH`, `KRONOS_SIZE_FLOOR`, `KRONOS_PRED_LEN_BY_ASSET`
- `TRADING_MODE` (`paper`|`testnet`|`live`|`off`), `TRADE_SYMBOLS`, `TRADE_MAX_NOTIONAL`, `TRADE_MAX_POSITIONS`, `TRADE_DAILY_LOSS_LIMIT`, `TRADE_APPROVAL_TTL_S`, `TRADE_LIVE_CONFIRM`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional; adopt via `/start`)
- `FADE_PEAK_THRESHOLD` (20.0), `FADE_GIVEBACK_MIN` (15.0), `FADE_MAX_AGE_H` (48.0)
- `KILL_SWITCH` path to halt execution

## Data & State

Runtime JSON files (gitignored): `snapshot_*.json`, `snapshot_summary.json`, `signals.json`, `discovery.json`, `fade_track.json`, `forecasts.json`, `kronos_track.json`, `pending_orders.json`, `trade_log.json`, `service_state.json`, `alert_state.json`. Regenerated at runtime — do not commit.

Outputs: `intelligence_brief_*.html`, logs in `service.log`.

## Safety & Notes

- **Windows venv**: `.venv/` is Windows-only — never use on Linux. Use system `python3`.
- **Human-in-the-loop**: Engine never auto-fires trades; crypto proposals require explicit approval (dashboard/Telegram).
- **Whitelist**: Only BTCUSDT/ETHUSDT/SOLUSDT/ZECUSDT tradable. Equities/commodities (GC=F/BZ=F/DX-Y.NYB/META/NVDA/AMD) remain advisory.
- **Kill switch**: Presence of `KILL_SWITCH` file halts everything.
- **Ephemeral analysis**: `candidate.py` writes to `candidate_runs/` (scratch) and does not touch live snapshots.
- **Kronos isolation**: Runs as subprocess in its own venv (pandas version split) — do not import `model` from main interpreter.

## Links

- Repo: https://github.com/JAFAR/Jafar-Engine
- Service: long-polling Telegram bot; dashboard on `0.0.0.0:5000`.

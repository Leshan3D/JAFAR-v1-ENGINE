# AGENTS.md — JAFAR Intelligence Pipeline

Always read `PIPELINE.md` in this repo for full pipeline context. Key rules below.

## Output format for analysis requests
When asked to analyze market intelligence / rank possible trades, always present
the result as **clean, tabulated markdown** — one table for the ranked actionable
setups (rank, asset, side, action, score, entry/SL/TP1/TP2, conviction, key
drivers), a second table for HOLD/watch-list names, and a short caveats list.
No prose dumps; reasons go in compact table cells or tight bullets.

## Critical gotcha
The `.venv/` in this directory is a stale local venv. It is not used by the
engine and it may contain binaries from another OS. NEVER use it. Always use system
`python3` (mise Python 3.14; flask, pandas, numpy, yfinance, torch preinstalled).

## Commands
```bash
cd ~/TradingPipeline
python3 build_snapshot.py          # full data layer run (auto-runs Kronos layer at the end)
python3 "generate brief.py"        # HTML intelligence brief
python3 signals.py                 # print entry/exit signals from existing snapshots
bash start_dashboard.sh            # start Flask dashboard on :5000 + open Brave
```

## Kronos forecast layer (auto)
`build_snapshot.py` calls `kronos_layer.py` at the end of every full run
(via `run_kronos_layer()`), and `run_service.do_light()` re-runs it on the
15-min refresh so it never goes stale against price. `signals.py` uses it as a
**gate / size modifier, not a vote**: a strong opposing forecast steps the
action toward HOLD and cuts size; it only enters the composite if
`KRONOS_FACTOR_WEIGHT>0`. It also logs every forecast to `kronos_track.json`
and prints a running hit-rate. Kronos pins `pandas==2.2.2` while the engine runs
pandas 3.x, so it lives in its own venv and runs as a subprocess — never import
`model` from the main interpreter.

- venv: `~/.venvs/trading-kronos/bin/python` (override `KRONOS_PYTHON`)
- repo: `kronos_src/` (git clone of shiyu-coder/Kronos)
- horizon: `KRONOS_PRED_LEN` (24 default = ~1 day on 1h bars); daily assets
  (GC=F, BZ=F, DX-Y.NYB) use 3 bars via `KRONOS_PRED_LEN_BY_ASSET` in build_snapshot.py
- gate knobs: `KRONOS_GATE`, `KRONOS_GATE_THRESH`, `KRONOS_SIZE_FLOOR`,
  `KRONOS_GATE_MIN_Z` (default 2.0)
- the gate score is `100*tanh(z)`, so a score of 50 is only |z|=0.55. The
  forecast must ALSO clear `KRONOS_GATE_MIN_Z` to veto, because the model's
  own hit rate is 52.9% (391 forecasts) — a sub-1-sigma forecast from a
  coin-flip model is noise, and it was holding ETH/SOL/ZEC at HOLD.
- disable: `KRONOS_ENABLED=0`
- output: `forecasts.json`, `kronos_track.json` + a `kronos_forecast` block in each snapshot
- CPU-only (the GT 1030 is sm_61, unsupported by the installed torch build);
  all 10 assets forecast in ~12s. See `.env.example` for all knobs.

## Trader (human-in-the-loop execution)
`trader.py` turns fresh actionable **crypto** signals into pending proposals and
executes them only after a human approves — the engine never auto-fires.
`run_service.plan_trades()` calls `trader.plan()` after every full and light run.
Approvals come from the dashboard "Trade Proposals" panel
(`/api/proposals`, `POST /api/orders/<id>/approve|reject`) or Telegram inline
buttons (`telegram_bot.notify_proposals` + `handle_callback`).

- modes (`TRADING_MODE`): `paper` (default, simulates fills, touches no exchange),
  `testnet` (Binance USDT-M futures testnet), `live` (needs `TRADE_LIVE_CONFIRM=I_UNDERSTAND`), `off`
- only crypto is tradable (`TRADE_SYMBOLS`); gold/Brent/DXY/equities stay advisory
- rails: `KILL_SWITCH` file halts everything, per-order `TRADE_MAX_NOTIONAL`,
  `TRADE_MAX_POSITIONS`, `TRADE_DAILY_LOSS_LIMIT`, proposals expire in
  `TRADE_APPROVAL_TTL_S`, one pending proposal per (symbol, side)
- evidence gate (`TRADE_QUALITY_STRICT`, default on): a proposal is skipped
  when the evidence behind it is missing or unusable — no/thin news, no
  sentiment, sentiment confidence below `TRADE_MIN_SENTIMENT_CONF`, stale
  news (`TRADE_MAX_NEWS_AGE_S`). Several dead feeds (`TRADE_FEED_DEGRADED_COUNT`)
  veto everything except crypto: crypto has four other evidence legs (price,
  funding + OI, Fear & Greed, crowd) and a duplicated RSS layer, so a partial
  outage scales it to 60% size instead of blocking it (`TRADE_CRYPTO_FEED_BLOCK=1`
  restores the veto). Usable-but-weak evidence (market-wide headlines, a thin
  crowd read) is not blocked; it scales the order down instead. Size also
  scales with sentiment confidence and with how specific the headlines are
  (asset_specific 1.0, market_beta 0.7, market_wide 0.4).
- `run_service.py` reloads `trader.py` on every plan cycle, so gate edits take
  effect on the next 15-min refresh without restarting the service (and without
  re-running the pipeline).
- state: `pending_orders.json`, `trade_log.json` (gitignored)
- CLI: `python3 trader.py [status|plan|list|approve <id>|reject <id>]`


`build_snapshot.py` writes `snapshot_<ASSET>.json`, `snapshot_summary.json`,
`signals.json`. The dashboard serves these from disk — no pipeline run needed to
view live data.

## Scoring layers (current)
- `signals.py` factors: fear_greed (0.30, banded — the neutral middle of the
  index votes 0 instead of a constant drag), news (0.25), momentum (0.30),
  macro (0.05, macro-linked assets), polymarket (0.10, strike-relative),
  derivatives (0.10, crypto + MEXC sleeve). The composite normalises by the
  weight of the factors actually present.
- `fear_greed` is banded on the index's own classification (extreme fear /
  fear / neutral / greed / extreme greed). The old linear map (100 - 2*value)
  gave a mildly greedy 63 the same −26 as extreme greed, a −7.8 drag on
  every crypto signal.
- `polymarket` is scored as the crowd's SURPRISE against the probability the
  strike's distance from spot implies (lognormal baseline, with a separate
  barrier model for "reach/hit" contracts), discounted by sample-size
  confidence. The raw YES average is not a sentiment reading and produced a
  −99 "STRONGLY_BEARISH" off three far-OTM contracts.
  - the baseline uses a regime-aware vol (half the last 60 sessions, half the
    full history; 252 bars/yr for stocks and commodities, 365 for crypto)
  - a touch contract whose outcome our price has already decided carries no
    information and is dropped
  - if the spot implied by the crowd's own prices is more than 8% away from
    ours (a Brent series against "WTI" contracts), the read is measuring the
    basis, not the opinion: it is damped and flagged as a basis mismatch
- `derivatives` comes from the perp tape: Binance USDT-M funding/basis/OI for
  crypto, MEXC funding/OI for gold, Brent and tokenised equities. Positive
  funding = crowded longs = contrarian bearish tilt.
- `data_quality` travels with every signal (headline count, scope, average
  sentiment confidence, news age, dead feeds, crowd confidence).

Note: these JSON files are **gitignored runtime state** (regenerated at boot by
the service). Don't commit drift in them.

## Deployment / service mode
`run_service.py` runs the engine as one long-lived process (Railway/Docker):
full pipeline hourly, light price refresh every 15 min, entry-zone + action-flip
Telegram alerts, and a long-poll Telegram bot (`/status /signals /signal
/price /news /run`). See `DEPLOY.md` for Railway + BotFather steps. Run it
locally the same way:

```bash
python3 run_service.py             # cwd = data dir, .env read from repo root
```

The container sets `DATA_DIR=/data` (volume); unset locally = files land next to
the scripts, exactly like manual runs. `alerts.py` / `telegram_bot.py` degrade
safely with no Telegram token.

## Public shareable link (trading partner tunnel)
The dashboard always listens on `0.0.0.0:5000`. To expose it publicly via a free
Cloudflare quick tunnel:

```bash
cd ~/TradingPipeline
bash share_dashboard.sh            # prints the public https://*.trycloudflare.com URL
```

`share_dashboard.sh` (in this dir) starts cloudflared in the background, waits,
prints the public URL, and opens Brave with it. The tunnel URL changes every run;
re-run the script to emit a fresh link. Requires `cloudflared` on PATH
(installed at `~/.local/bin/cloudflared`).

- Kill/renew tunnel: `pkill -f 'cloudflared tunnel'`
- The dashboard must be running first (`bash start_dashboard.sh`).
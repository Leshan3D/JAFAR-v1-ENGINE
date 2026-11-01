# JAFAR Intelligence — Trading Pipeline Context

> Reference file for running the pipeline and dashboard. Read this first whenever
> asked to "run the pipeline" / "launch the dashboard".

## Environment gotcha (IMPORTANT)
The `.venv/` in this repo is a **Windows** venv — `pyvenv.cfg` points at
`C:\Users\user\...` and it contains Windows-native `Scripts/*.exe`. It will
**never run on Linux**. Ignore it entirely.

Always use the system `python3` (mise, Python 3.14). Everything needed is already
installed there: flask, pandas, numpy, yfinance, transformers/torch, etc.

## How to run (Linux)
```bash
cd ~/TradingPipeline

# 1. Full data pipeline (fetches price + news + sentiment + macro + polymarket,
#    writes snapshot_*.json + snapshot_summary.json + signals.json)
python3 build_snapshot.py

# 2. Optional: readable HTML brief (newspaper style)
python3 "generate brief.py"

# 3. Signal engine scan only (reads existing snapshots, prints entry/exit levels)
python3 signals.py

# 4. Dashboard server -> http://localhost:5000  (launch in Brave)
bash start_dashboard.sh
```

## What the pipeline does

```
Layer 1: Data Collection  (build_snapshot.py orchestrates)
  fetch_price.py      Binance OHLCV (crypto) + Yahoo Finance (stocks/commodities)
  fetch_news.py       RSS (CoinTelegraph/Decrypt/Reuters/YF/MarketWatch), GDELT,
                      SEC EDGAR; per-source fallback so one failure never kills run
  fetch_sentiment.py  Fear&Greed from alternative.me + local FinBERT (pos/neg/neu + conf)
  fetch_macro.py      FRED: 10Y/2Y yields, real yield, breakeven, Fed Funds, CPI, DXY;
                      derives yield-curve status + gold macro signal
  fetch_polymarket.py Prediction markets filtered to financial/geo (sports blocked),
                      crowd sentiment per asset

Layer 2: Signal Engine  (signals.py)
  6 weighted factors, each -100..+100:
    Fear & Greed  30% (contrarian: greed=bad, fear=good)  [crypto]
    News sentiment 25% (directional)
    Momentum     30% (trend + RSI over/underbought)
    Macro         5% (inverted curve = headwind)          [macro-linked assets]
    Polymarket   10% (crowd consensus)
    Kronos       0% by default — a GATE/size modifier, not a vote (env
                 KRONOS_FACTOR_WEIGHT>0 to blend). Opposing forecasts step the
                 action toward HOLD and cut size; agreement = no change.
  Composite -> STRONG_BUY / BUY / ACCUMULATE / HOLD / REDUCE / SELL / STRONG_SELL
  Actionable signals get entry, stop-loss (1.5x ATR), TP (2:1 & 3:1 R/R),
  position size = **full margin (100% of allocated capital)**.
  Polymarket factor only scores markets with a $ price anchor — see
  fetch_polymarket.py reject_reason().

Layer 1.5: Kronos Forecast Layer  (kronos_layer.py — auto-run by build_snapshot.py
           hourly and by run_service.do_light() every 15 min)
  Foundation model (Kronos-small) over the snapshot's OHLCV price_data,
  forecasting the next N bars (24 for 1h assets ≈ 1 day; 3 for daily assets).
  Writes forecasts.json + kronos_track.json (accuracy scoreboard) and injects a
  `kronos_forecast` block into each snapshot. Runs in an isolated venv
  (/home/jafar/.venvs/trading-kronos) because Kronos pins pandas 2.2.2
  while the engine uses pandas 3.x. CPU-only; ~12s for all 10 assets.


Layer 3: Output
  signals.json            machine-readable actionable signals
  snapshot_<ASSET>.json   raw data per asset
  intelligence_brief.html newspaper-style brief (auto-generate via "generate brief.py")
  dashboard.py            live Flask dashboard @ :5000 (sparklines, sentiment bars,
                          polymarket arcs, auto-refresh 5 min)
```

## Watchlist
| Asset       | Type      | Source      | Notes              |
|-------------|-----------|-------------|--------------------|
| BTCUSDT     | crypto    | Binance     | 1h / 400 candles   |
| ETHUSDT     | crypto    | Binance     | 1h / 400 candles   |
| SOLUSDT     | crypto    | Binance     | 1h / 400 candles   |
| GC=F        | commodity | yfinance    | Gold futures 1d    |
| ZECUSDT     | crypto    | Binance     | 1h / 400 candles   |
| BZ=F        | commodity | yfinance    | Brent crude 1d     |
| NVDA        | stock     | yfinance    | 1h / 400 candles   |
| META        | stock     | yfinance    | 1h / 400 candles   |
| AMD         | stock     | yfinance    | 1h / 400 candles   |
| DX-Y.NYB    | stock     | yfinance    | DXY index 1d       |

## Snapshot filename mapping
Symbol chars that are invalid for filenames get replaced: `=` `-` `.` `/` → `_`.
e.g. `GC=F` → `snapshot_GC_F.json`, `DX-Y.NYB` → `snapshot_DX_Y_NYB.json`.
`dashboard.py` ASSET_META keys match these sanitized names.

## Notes / Known weak spots
- `build_snapshot.py:run_with_brief()` imports `generate_brief` but the real module
  filename is `generate brief.py` (space), so that import path is broken — call the
  generator script directly instead.
- Dashboard is Flask dev server binding `0.0.0.0:5000`. If port 5000 is taken,
  start_dashboard.sh kills the existing process first.
- snapshot_*.json files already present can be served by the dashboard even without
  re-running the fetch layer (data is read from disk).
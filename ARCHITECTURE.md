# ARCHITECTURE.md — How the JAFAR engine actually works

> Code-level walkthrough of this directory. Verified against the source on
> **2026-11-14**. This file adds the *mechanism* that `README.md` and
> `PIPELINE.md` describe only at a high level. When those two disagree with the
> code, the code wins — see [Doc drift](#doc-drift-whats-stale-elsewhere).
>
> **Adding this file is operationally inert.** The engine's directory scans
> target `snapshot_*.json` by name; no code path globs or reads `*.md`. Nothing
> reads `ARCHITECTURE.md`.

---

## 1. Thirty-second version

JAFAR is a **human-in-the-loop market intelligence loop**, not an auto-trader.

```
scheduler (run_service.py)
   │
   ├─ hourly  → build_snapshot.py  → fetch price/news/sentiment/macro/polymarket
   │                                 → snapshot_<ASSET>.json
   │                                 → kronos_layer.py (subprocess, own venv)
   │                                 → signals.py evaluate → signals.json
   │                                 → generate brief.py  → HTML brief
   │                                 → trader.py plan     → pending_orders.json
   │
   ├─ 15 min  → price + derivatives refresh → regenerate signals (no news refetch)
   ├─ 60 s    → zone watcher (entry ±0.3 %, action flips)
   ├─ 60 s    → macro calendar (pre-print warnings, post-print re-run)
   ├─ always  → Flask dashboard :5000 + Telegram bot thread
   │
   └─ approval → dashboard / Telegram → trader.approve() → paper | testnet | live
```

**The engine never fires a trade on its own.** It produces a proposal; a human
clicks approve. That is the single most important design property.

---

## 2. Process map

| Process | Started by | Lifetime | Role |
|---|---|---|---|
| `run_service.py` | `systemd --user` via `run_service_eat.sh` | long-lived | Conductor: schedules runs, hosts threads |
| `build_snapshot.py` | `run_service.run_script()` | 5–15 min, subprocess | Data layer + signal pass |
| `kronos_layer.py` | `build_snapshot.run_kronos_layer()` | ~10–60 s, subprocess | Forecast layer in isolated venv |
| `generate brief.py` | `run_service.do_full()` | seconds | HTML newspaper brief |
| `dashboard.py` | `run_service._ensure_dashboard()` | long-lived child | Flask UI/API on `:5000` |
| `telegram_bot.py` | thread inside `run_service` | long-lived | Long-poll bot + LLM chat |
| `zone_loop` / `macro_loop` | threads inside `run_service` | long-lived | Entry-zone + macro alarms |
| `watch_btc_levels.py` | its own systemd unit | long-lived | Price-level pings |
| `jafar-msci-watch` | systemd timer | oneshot | Index-review watcher + BTC scan |

Only one writer should touch `snapshot_*.json` / `signals.json` at a time.
`run_service` is that writer. Any manual run must stop the service first (see
[Focused runs](#12-focused-runs--jafar_only)).

---

## 3. Data flow in detail

```
                 ┌─────────────── run_service.do_full() ───────────────┐
                 │                                                     │
 fetch_price ────┤ Binance / yfinance / MEXC OHLCV                     │
 fetch_news ─────┤ RSS per asset class + GDELT + SEC EDGAR             │
 fetch_sentiment ┤ Fear&Greed (alternative.me) + local FinBERT         │
 fetch_macro ────┤ FRED + BLS CPI → gold/oil/equity macro signals      │
 fetch_polymarket┤ price-anchored prediction markets → crowd score     │
 fetch_price ────┤ Binance USD-M funding / OI / basis (crypto only)    │
                 │                                                     │
                 ▼                                                     │
        snapshot_<ASSET>.json  (raw, per asset)                        │
                 │                                                     │
                 ▼                                                     │
          kronos_layer.py  → injects kronos_forecast, writes forecasts │
                 │                                                     │
                 ▼                                                     │
        signals.evaluate_snapshot() → signals.json + snapshot_summary  │
                 │                                                     │
        ┌────────┴─────────┬───────────────┬──────────────┐            │
        ▼                  ▼               ▼              ▼            │
   dashboard.py      telegram_bot     trader.plan()   generate brief   │
   (:5000 UI)        (alerts/cmds)    (proposals)     (HTML)           │
                                                                       │
                 └─────────────────────────────────────────────────────┘
```

### 3.1 Watchlist (`build_snapshot.WATCHLIST`)

| Asset | Label | Type | Bars | Notes |
|---|---|---|---|---|
| `BTCUSDT` | BITCOIN | crypto | 1h × 400 | Binance spot + USD-M perp context |
| `ETHUSDT` | ETHEREUM | crypto | 1h × 400 | |
| `SOLUSDT` | SOLANA | crypto | 1h × 400 | |
| `ZECUSDT` | ZCASH | crypto | 1h × 400 | |
| `GC=F` | GOLD FUTURES | commodity | 1d × 200 | MEXC `XAU_USDT` reference |
| `BZ=F` | BRENT CRUDE | commodity | 1d × 200 | |
| `NVDA` | NVDA | stock | 1h × 400 | yfinance |
| `META` | META PLATFORMS | stock | 1h × 400 | |
| `AMD` | AMD | stock | 1h × 400 | |
| `INTC` | INTEL | stock | 1h × 400 | Added for the Oct-2026 CPI trade |
| `DX-Y.NYB` | DXY INDEX | stock | 1d × 200 | |

Filename sanitisation: `=` `-` `.` `/` → `_`.
`GC=F → snapshot_GC_F.json`, `DX-Y.NYB → snapshot_DX_Y_NYB.json`.
`dashboard.py` keys and `run_service` pruning depend on this exact mapping.

`build_snapshot.validate_watchlist()` **fails loudly** on mis-wiring (e.g. a
`polymarket_key` that doesn't exist). This exists because SOL once pointed at
Bitcoin's markets and produced a fabricated crowd score.

### 3.2 Price (`fetch_price.py`)

- `fetch_binance_ohlcv(symbol, interval, limit)` — spot klines.
- `fetch_stock_ohlcv(ticker, period, interval, retries=3)` — yfinance, retried
  because Yahoo intermittently times out.
- `fetch_mexc_ohlcv` / `fetch_mexc_ticker` — MEXC contract venue (gold).
- `fetch_binance_futures_context(symbol)` — the **derivatives** factor for
  crypto: `premiumIndex` (mark/index/last funding) + `openInterest` + `24hr`
  change. Basis is computed as `(mark − index) / index × 100`. Gold/Brent get
  the same block from their MEXC ticker (`mexc_reference`: funding, OI, volume),
  which is why their composite weight sum is 0.80 rather than 0.70.
- `estimate_annualized_vol(closes)` — used as the strike-distance baseline for
  Polymarket's crowd model.

### 3.3 News (`fetch_news.py`)

RSS registry by asset class:

| Class | Sources |
|---|---|
| Crypto | CoinTelegraph, CoinDesk, Decrypt, BeInCrypto, The Block, Investing.com, Reddit |
| Oil | EIA Today in Energy, OilPrice, CNBC, WSJ, GDELT |
| Stocks | Yahoo per-ticker, MarketWatch, Seeking Alpha, CNBC, WSJ, SEC EDGAR |
| Macro | CNBC, WSJ World, NPR, Federal Reserve, FT |

Every source is independent: one dead feed cannot kill the run. Each outcome is
recorded in `SOURCE_HEALTH`, so a quietly dead feed shows up in `signals.json`
as `feed_degradation` instead of being silently ignored.

GDELT is special-cased: minimum 45 s between calls, 6 h disk cache
(`gdelt_cache.json`), and a 30 min circuit breaker (`gdelt_breaker.json`) after
HTTP 429.

**Entity split.** `split_news_by_entity()` scores how many headlines actually
name the asset. If fewer than `MIN_ASSET_SPECIFIC_HEADLINES = 5` are
asset-specific, the engine falls back to market-wide headlines and labels the
scope:

| Scope | Meaning | Size multiplier |
|---|---|---|
| `asset_specific` | ≥ 5 headlines name the asset | 1.00 |
| `market_beta` | crypto-wide, but asset's measured BTC corr ≥ 0.50 | 0.70 |
| `market_wide` | generic tape, no justified link | 0.40 |

`NEWS_ENTITY_PATTERNS` is the per-asset name list. `INTC` was missing and was
added; before that, Intel news was scored as generic market news.

### 3.4 Sentiment (`fetch_sentiment.py`)

- `fetch_fear_greed_index(limit)` — alternative.me, crypto only.
- `FinBERTSentiment` — local transformer; returns positive/negative/neutral plus
  per-headline confidence. Loaded **once** per full run (expensive), not per
  asset.

### 3.5 Macro + CPI (`fetch_macro.py`)

FRED series: `DGS10`, `DGS2`, `DFII10`, `T10YIE`, `CPIAUCSL`, `FEDFUNDS`,
`DTWEXBGS` (broad dollar).

**CPI is the one place BLS beats FRED.** BLS v2 is queried first because FRED
lags the release by a day or more:

| Series | Use |
|---|---|
| `CUSR0000SA0` | headline MoM (SA) — what the market trades |
| `CUSR0000SA0L1E` | core MoM (SA) |
| `CUUR0000SA0` | headline NSA (YoY quote) |
| `CUUR0000SA0L1E` | core NSA |

Cached 6 h (`CPI_CACHE_TTL_S`) because BLS v2 unregistered allows ~25 queries/day
and the service calls hourly. FRED is the fallback.

`classify_inflation()` annualises MoM:

```
core annualised ≥ 3.5 %  OR  headline annualised ≥ 4.0 %  → HOT
core ≤ 1.5 % AND headline ≤ 2.0 %                          → COOL
otherwise                                                  → NEUTRAL
```

`fetch_all_macro()` then derives three **separate** asset signals:

- `gold_macro_signal` — real yields, safe-haven demand
- `oil_macro_signal` — dollar, growth, inflation
- `equity_macro_signal` — duration logic: HOT CPI / strong dollar / inverted
  curve are headwinds; COOL CPI / weak dollar are tailwinds

`signals.MACRO_SIGNAL_KEYS` decides which asset inherits which signal:

```python
GC=F → gold_macro_signal
BZ=F → oil_macro_signal
NVDA, META, AMD, INTC → equity_macro_signal
```

**There is deliberately no crypto macro key.** Crypto has no macro factor by
design. The `HOT` tag is a regime label, not a surprise metric, and it reaches
INTC only through `equity_macro_signal`.

### 3.6 Polymarket (`fetch_polymarket.py`)

Filters the top ~100 markets down to financial/geopolitical ones (sports
blocked), then per asset:

- `MIN_LIQUIDITY_USD = 500`, `MIN_VOLUME_24H_USD = 1000`
- only markets with a **$ price anchor** are scored — a raw YES probability on
  "BTC above $90k" is a statement about the strike, not the crowd
- `implied_spot()` inverts the crowd probability through Black-Scholes-style
  touch/direction logic, then compares it to spot (`basis_ratio`)
- `BASIS_MISMATCH_TOL` rejects markets whose implied spot is too far from the
  real one
- the output `crowd_score` is the strike-relative reading, **not** an average of
  raw probabilities (that old method once printed STRONGLY_BEARISH off three
  far-OTM contracts)

### 3.7 Snapshot schema (abridged)

Each `snapshot_<ASSET>.json` carries:

```text
snapshot_time, asset, label, asset_type, candle_interval, candle_limit
current_price, price_data[], daily_closes[], candle_count
btc_beta{beta,corr}, btc_context
news_headlines[], news_sources_hit[], news_source_health
news_sentiment[], sentiment_counts{}, avg_confidence
news_scope, news_scope_note, news_fetched_at
fear_greed{}            (crypto only)
macro_context{}         (all assets, but consumed selectively)
polymarket_signals{}
derivatives{}           (crypto via Binance; gold/Brent via MEXC)
mexc_reference{}        (gold)
volatility, data_as_of, age_seconds, is_stale
kronos_forecast{}       (injected by the Kronos subprocess)
errors[]
```

`data_as_of` is the **newest bar timestamp**, not the write time. Staleness is
recomputed at read time, so a snapshot that was fresh when written still reports
stale later.

Staleness thresholds scale with bar size:

| Interval | Stale after |
|---|---|
| 5m | 30 min |
| 15m | 90 min |
| 1h | 150 min |
| 4h | 10 h |
| 1d | 36 h |

---

## 4. Kronos forecast layer (`kronos_layer.py`)

A foundation model (`NeoQuasar/Kronos-small`) forecasts the next N bars from the
snapshot's own OHLCV. It runs as a **subprocess in an isolated venv**
(`~/.venvs/trading-kronos`) because Kronos pins `pandas 2.2.2` while the engine
uses pandas 3.x. Never import `kronos_src.model` from the main interpreter.

Defaults: `KRONOS_PRED_LEN=24` bars (≈1 day of 1h), `KRONOS_LOOKBACK=400`,
`KRONOS_SAMPLES=1`, CPU, seed 42 (reproducible forecasts). Daily assets use a
short horizon via `KRONOS_PRED_LEN_BY_ASSET`:

```python
GC=F → 3,  BZ=F → 3,  DX-Y.NYB → 3
```

Without that, a daily asset would be forecast a month out, which is useless for
a 1–3 day tactical hold.

Outputs: `forecasts.json`, `kronos_track.json` (rolling hit-rate scoreboard), and
a `kronos_forecast` block injected back into each snapshot. `build_snapshot`
re-reads those blocks (`_reload_kronos_block`) so the same run's signals see the
new forecast.

**Kronos does not vote by default.** `KRONOS_FACTOR_WEIGHT=0`. It is a gate/size
modifier, described next. Also runs on the 15-min light refresh
(`KRONOS_ON_LIGHT=1`) so it does not go stale between hourly runs.

---

## 5. Signal engine (`signals.py`)

### 5.1 Weights

| Factor | Weight | Direction | Applies to |
|---|---:|---|---|
| `fear_greed` | 0.30 | **contrarian** | crypto only |
| `news` | 0.25 | directional | all |
| `momentum` | 0.30 | trend + RSI | all |
| `macro` | 0.05 | asset-specific tilt | gold / oil / equities |
| `polymarket` | 0.10 | crowd vs strike | all with a price-anchored market |
| `derivatives` | 0.10 | funding + basis + OI | crypto (Binance tape) + gold/Brent (MEXC tape) |
| `kronos` | 0.00 | gate only | all |

The composite **normalises by the weights actually present**:

```
composite = Σ(wᵢ·sᵢ) / Σ(wᵢ)      for i present in the snapshot
```

So a crypto snapshot has weight sum `1.05`; gold/Brent `.80` (they carry the
MEXC funding/OI tape as a derivatives factor); an equity snapshot `.70`.
Missing factors do not drag the score toward zero. `weights_used` in
`signals.json` records exactly what was present, missing, and the divisor used.

### 5.2 How each factor scores (−100…+100)

- **Fear & Greed** — piecewise-linear bands aligned to Alternative.me's own
  classification (Extreme Fear 0–25, Fear 25–45, Neutral 45–65, Greed 65–80,
  Extreme Greed 80–100). Contrarian: fear is positive, greed is negative. There
  are no cliffs at band boundaries.
- **News** — direction of FinBERT sentiment (`pos − neg`), scaled. The
  `news_scope` multiplier from §3.3 is applied later by the trader, not inside
  the factor score.
- **Momentum** — trend + RSI. Oversold adds, overbought subtracts; RSI source is
  the daily series when available (`rsi_source: "1d"`), otherwise the trading
  interval. `ret5` is 5 bars, not 5 days.
- **Macro** — inverted curve −50, flattening −15, steepening +10; then ±20 for
  the asset-specific macro signal. Clipped to ±100.
- **Polymarket** — strike-relative `crowd_score`, confidence-discounted (×0.3,
  capped ±30 internally).
- **Derivatives** — funding-rate bands (crowded longs = negative), plus basis
  and OI-change context.

### 5.3 Data-quality gate

A clean-looking score built on three headlines is not tradable evidence. The
engine separates flags into:

**Blocking** — `no_news`, `thin_news`, `no_sentiment`,
`sentiment_confidence_unknown`, `low_sentiment_confidence`, `stale_news`,
`feed_degradation`, `no_factors`.

**Advisory** — `news_age_unknown`, `thin_polymarket`, `weak_proxy_news`.

Defaults: ≥ 5 headlines, sentiment confidence ≥ 0.55, news age ≤ 90 min, ≤ 3
failed feeds.

One deliberate exception: for **crypto**, `feed_degradation` is demoted from
blocking to advisory (`CRYPTO_NON_BLOCKING_FLAGS`). Crypto carries four other
evidence legs and the RSS layer is duplicated across assets, so a partial feed
outage scales the order down instead of vetoing it. Set
`TRADE_CRYPTO_FEED_BLOCK=1` to restore the veto.

### 5.4 Kronos gate

```python
score  = 100 · tanh(z)          # z = forecast move / historical sigma
veto   = opposite direction AND |z| ≥ KRONOS_GATE_MIN_Z (2.0)
effect = action −1 step toward HOLD, size × KRONOS_SIZE_FLOOR (0.5)
```

Why the z-condition: the old rule fired at `score ≥ 50`, which is only `|z| ≈
0.55` — inside one standard deviation. With a 52.9 % hit rate, that was not a
gate, it was an outage generator (it held ETH, SOL and ZEC at HOLD on noise).
Now the forecast must be statistically significant before it can veto.

Agreement and neutrality change nothing. The full decision is preserved in
`signals.json → kronos_gate` (`action_before`, `action_after`, `size_mult`,
`z_significant`, `note`).

### 5.5 Action bands

| Composite | Action |
|---:|---|
| ≥ +50 | `STRONG_BUY` |
| ≥ +20 | `BUY` |
| ≥ +8 | `ACCUMULATE` |
| > −8 | `HOLD` |
| ≤ −8 | `REDUCE` |
| ≤ −20 | `SELL` |
| ≤ −50 | `STRONG_SELL` |

Anything above `HOLD` is "actionable": it gets levels and a size. `HOLD` emits
none.

### 5.6 Levels and sizing

```python
stop_width = 1.5 × ATR(interval)
LONG:  entry = close,  SL = close − width,  TP1 = close + 2·width,  TP2 = close + 3·width
SHORT: mirror
```

So TP1 is exactly 2R and TP2 is 3R **by construction**. This is the engine's
central trade-off: the stop is tight (≈0.8 % for BTC 1h), which makes 2R
reachable but places the stop inside normal noise. Expect a high stop-out rate.
Widening the stop without moving the targets breaks the 2:1 payoff — stop and
payoff are a package.

`position_size()` returns `MAX_POSITION_FRACTION = 1.00`: **100 % of the
allocated capital is committed as margin**. Risk is expressed by the stop, not
by a reduced fraction. The trader then multiplies that fraction by evidence
quality and leverage, and caps the notional.

For gold/Brent, `mexc_reference` gives a basis shift and `signals.py` also emits
`mexc_levels` — the same levels translated into the venue where the order
actually fills.

---

## 6. Execution rails (`trader.py`)

Human-in-the-loop, crypto only.

```
signals.json ──► plan() ──► pending_orders.json ──► approve() ──► paper/testnet/live
                                  │
                                  └─ expires after TRADE_APPROVAL_TTL_S (900 s)
```

### 6.1 Gates before a proposal exists

| Gate | Rule |
|---|---|
| Mode | `TRADING_MODE=off` disables planning |
| Kill switch | presence of `KILL_SWITCH` file aborts |
| Whitelist | `TRADABLE` = `TRADE_SYMBOLS` (default BTC/ETH/SOL/ZEC USDT) |
| Action | must be in `ACTIONABLE` (anything stronger than HOLD) |
| Freshness | `is_stale` signals are skipped |
| Evidence | strict mode blocks proposals with blocking quality flags |
| Position cap | `open + new < TRADE_MAX_POSITIONS` (3) |
| Duplicate | one pending proposal per `(symbol, side)` |
| Notional | `min(equity × size_frac × leverage, TRADE_MAX_NOTIONAL)` |

`size_frac` is the signal's 1.00 × evidence multiplier:

```
confidence 0.55 → 0.25×,  0.70 → 0.50×,  0.90 → 1.00×
scope: asset_specific 1.0, market_beta 0.7, market_wide 0.4
feed_degradation: × 0.60
```

### 6.2 Approval

`approve()` re-checks the kill switch, live confirmation and TTL, then:

- `paper` → simulated fill at signal entry, no exchange call
- `testnet` → Binance futures testnet
- `live` → `fapi.binance.com`, **only** if `TRADE_LIVE_CONFIRM=I_UNDERSTAND`

Executions are appended to `trade_log.json`. Approvals arrive from the dashboard
(`POST /api/orders/<id>/approve`) or Telegram inline buttons.

### 6.3 Honest caveat: declared vs enforced

`TRADE_DAILY_LOSS_LIMIT` and `_realized_today()` are **defined but never
enforced** anywhere in the current code. Do not rely on the daily loss limit as
a safety rail. The real rails are the whitelist, the notional cap, the position
cap, the TTL, the kill switch and human approval.

### 6.4 MEXC executor (`mexc_exec.py`)

A separate manual CLI for MEXC contract trades (gold/BTC), not wired into
`trader.py`. It has its own rails: dry-run default, `MEXC_LEVERAGE`,
`MEXC_MAX_NOTIONAL`, `MEXC_MAX_POSITIONS`, kill switch, signed requests and
secret redaction. Commands: `status`, `quote`, `order`, `positions`, `close`,
`leverage`.

---

## 7. Service conductor (`run_service.py`)

### 7.1 Schedule

| Loop | Default | Env | What it does |
|---|---:|---|---|
| Full pipeline | 3600 s | `PIPELINE_INTERVAL_S` | data layer + Kronos + signals + brief + `trader.plan()` |
| Light refresh | 900 s | `PRICE_REFRESH_INTERVAL_S` | price + daily closes + derivatives + Kronos + regenerate signals |
| Zone watch | 60 s | `ZONE_POLL_INTERVAL_S` | entry ±0.3 % crossings, action flips |
| Macro poll | 60 s | `MACRO_POLL_INTERVAL_S` | lead warnings + post-print wake |
| Stocks in zone loop | 300 s | hard-coded | only during US regular hours (13:30–20:00 UTC) |
| Dashboard ensure | every outer tick | — | restarts Flask if it died |

On boot, `next_full = 0`, so a full run starts immediately.

`do_light()` deliberately does **not** refetch news, sentiment, macro or
Polymarket. It only refreshes price/derivatives and recomputes signals from the
stored snapshots. That is why a 15-min signal can move on price alone.

`_prune_orphan_snapshots()` deletes `snapshot_*.json` files whose asset is no
longer on the watchlist. This is how a removed ticker stops haunting the
dashboard. `snapshot_summary.json` is explicitly protected.

### 7.2 Threads

`main()` starts three daemon threads and then runs the scheduling loop:

- `zone_loop` — writes `last_actions.json`
- `macro_loop` — reads `macro_calendar.json`, sends Telegram/desktop alerts
- `telegram_bot.run` — long-poll `getUpdates`; `/run` sets `run_event`

`run_event` is the only path from the bot into the scheduler: it forces
`do_full(manual=True)` on the next tick.

### 7.3 Macro calendar (`macro_calendar.py`)

Builds a forward calendar from FRED release dates plus a bundled FOMC schedule,
with an optional hand-maintained `macro_calendar_manual.json`. Cached 12 h.
Lead warnings default to 120/30/10 minutes; a post-print re-run fires 45 s after
the event (grace 600 s). Optional loud desktop alarm at 15/5 min and at the
print (`MACRO_PC_ALARM_*`).

This loop is how a CPI print becomes an engine action within a minute rather
than waiting for the next hourly sweep.

---

## 8. Interfaces

### 8.1 Dashboard (`dashboard.py`, Flask, `0.0.0.0:5000`)

| Route | Purpose |
|---|---|
| `GET /` | full HTML dashboard (server-rendered template, dark UI, sparklines) |
| `GET /api/summary` | all signals |
| `GET /api/assets` | asset cards |
| `GET /api/asset/<key>` | one snapshot |
| `GET /api/fng` | Fear & Greed history |
| `GET /api/proposals` | pending trade proposals |
| `POST /api/orders/<id>/approve` | approve → `trader.approve()` |
| `POST /api/orders/<id>/reject` | reject |
| `GET /api/trader/status` | mode, equity, rails |

The dashboard is restarted automatically by `run_service._ensure_dashboard()`
if port 5000 stops answering. `start_dashboard.sh` is the manual equivalent and
kills an existing listener first.

### 8.2 Telegram (`telegram_bot.py` + `alerts.py`)

Commands: `/start` `/help` `/status` `/calendar` `/signals` `/signal <asset>`
`/price <asset>` `/news <asset>` `/positions` `/run`. Free text goes to an
OpenAI-compatible LLM with the full Jafar context.

`alerts.py` handles delivery and **per-key cooldowns** (`alert_state.json`) so a
flapping condition cannot spam. Chat adoption is automatic on `/start`, or pin
`TELEGRAM_CHAT_ID` in `.env`. Tokens are redacted before logging.

There is **no** `/discover` or `/fades` command in the current code — those
modules were deleted (see doc drift).

---

## 9. Outputs and state files

### 9.1 Primary outputs

| File | Writer | Consumer |
|---|---|---|
| `snapshot_<ASSET>.json` | `build_snapshot` / `do_light` | signals, dashboard, Kronos |
| `snapshot_summary.json` | `build_snapshot` | dashboard summary |
| `signals.json` | `build_snapshot`, `do_light` | dashboard, bot, trader |
| `forecasts.json` | `kronos_layer` | signals, bot |
| `kronos_track.json` | `kronos_layer` | scoreboard |
| `intelligence_brief_*.html` | `generate brief.py` | human reading |
| `service.log` | `run_service` (stdout) | debugging |
| `pipeline_runs_eat.log` | `run_service_eat.sh` | start/exit stamps |

### 9.2 Runtime state (all gitignored, all regenerable)

```text
service_state.json        last run times, errors, start stamp
pending_orders.json       proposals awaiting approval
trade_log.json            executed/rejected trades
last_actions.json         previous action per asset (flip detection)
alert_state.json          Telegram recipient + cooldown state
macro_calendar.json       cached calendar
oi_track.json             open-interest deltas
gdelt_cache.json          GDELT answer cache
gdelt_breaker.json        GDELT 429 circuit breaker
btc_levels_state.json     fired levels + arm reference price
candidate_runs/           ephemeral candidate analyses
msci_watch/<stamp>/       MSCI watcher run artifacts
```

Deleting `service_state.json` or `last_actions.json` is harmless — it only
re-baselines alerts. Deleting `snapshot_*.json` is not: the next light refresh
will skip assets whose snapshot is missing, and only a full run rebuilds them.

---

## 10. Auxiliary tools

| Script | What it does | Alert channel |
|---|---|---|
| `watch_btc_levels.py` | direction-aware price-level pings; one fire per level, state persisted | desktop + sound + Telegram |
| `watch_btc_position.py` | milestone manager for an existing BTC long (TP1/BE/TP2/run/SL) | desktop + Telegram |
| `tp_alert.py` | generic Binance TP1/TP2/SL watcher with live P&L for margin × leverage | desktop |
| `alert_btc_limit.py` | detects a resting MEXC limit BUY filling (or price running away) | desktop + Telegram |
| `macro_calendar.py` | FRED/BLS/FOMC forward calendar | consumed by `run_service` |
| `msci_watch.py` | polls MSCI review docs, greps for DAT names, runs a BTC-only scan, restores the book | Telegram |
| `candidate.py` | runs any symbol through the full engine in a scratch dir, never touching live snapshots | CLI / JSON |

### 10.1 `watch_btc_levels.py` — the direction trap

This watcher was originally written for **upward-only** round numbers
(`last >= level`). Armed with a stop *below* the market it fired immediately on
the wrong side. It now stores the price at arm time (`ref`) and decides:

```python
downward = level < ref
hit      = last <= level if downward else last >= level
```

It also refuses to crash when the first quote fails (`last=None` cold boot) and
quotes **Bybit** by default. MEXC's public ticker returns 403 from this
machine; OKX also 403s. Bybit `BTCUSDT` linear is the working feed.

Operational truth: a local watcher cannot fire while the host is powered down.
The exchange-side stop is the only authoritative one. Keep it on the exchange.

### 10.2 `msci_watch.py`

Built for the Nov-2026 MSCI index review: polls deterministic document URLs,
converts PDFs with `pdftotext`, greps for digital-asset-treasury names, waits for
the news tape to settle, runs `JAFAR_ONLY=BTCUSDT`, restores the 11-asset book,
writes `RESULT.md`, and sends Telegram. Uses `flock` and a `.done` sentinel so a
timer re-fire cannot double-run. Everything lands under `msci_watch/<stamp>/`.

---

## 11. Environment and configuration

Config is loaded from `.env` (mode 600) by the modules that need it. `.env` is
**not** committed and holds the live bot token, FRED key and LLM key.

### 11.1 Core knobs

| Group | Variables |
|---|---|
| Cadence | `PIPELINE_INTERVAL_S`, `PRICE_REFRESH_INTERVAL_S`, `ZONE_POLL_INTERVAL_S` |
| Macro | `MACRO_ALERTS_ENABLED`, `MACRO_POLL_INTERVAL_S`, `MACRO_LEAD_MIN`, `MACRO_POST_DELAY_S`, `MACRO_PC_ALARM_*` |
| Kronos | `KRONOS_ENABLED`, `KRONOS_ON_LIGHT`, `KRONOS_PYTHON`, `KRONOS_MODEL`, `KRONOS_PRED_LEN`, `KRONOS_LOOKBACK`, `KRONOS_SEED`, `KRONOS_FACTOR_WEIGHT`, `KRONOS_GATE`, `KRONOS_GATE_THRESH`, `KRONOS_GATE_MIN_Z`, `KRONOS_SIZE_FLOOR` |
| Trader | `TRADING_MODE`, `TRADE_SYMBOLS`, `TRADE_LEVERAGE`, `TRADE_MAX_NOTIONAL`, `TRADE_MAX_POSITIONS`, `TRADE_DAILY_LOSS_LIMIT`*, `TRADE_APPROVAL_TTL_S`, `TRADE_QUALITY_STRICT`, `TRADE_CRYPTO_FEED_BLOCK`, `TRADE_LIVE_CONFIRM`, `KILL_SWITCH` |
| MEXC | `MEXC_LEVERAGE`, `MEXC_MAX_NOTIONAL`, `MEXC_MAX_POSITIONS`, `MEXC_DRY_RUN` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| LLM | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_STYLE` |
| Data | `FRED_API_KEY`, `DATA_DIR`, `JAFAR_ONLY` |

\* declared but not enforced (see §6.3).

Current `.env` has `TRADING_MODE=paper`. The engine's trader therefore cannot
place a real order even if approved. `TRADE_LEVERAGE` defaults to **1** — the
engine's notional math is not the same as a manual 10× position. Do not confuse
the two: engine leverage is a config value, not a property of your exchange
account.

### 11.2 Interpreter pin (important)

`mise`'s `latest` Python moved to **3.15.2**, which does **not** contain the
engine's dependencies. All packages live in **3.14.7**. The systemd wrapper now
pins:

```bash
PY_HOME=~/.local/share/mise/installs/python/3.14.7
```

Without this the service crash-loops with:

```
ModuleNotFoundError: No module named 'requests'
```

Any new script or unit must use the same interpreter. `btc-levels-watch` works
on `latest` only because it is stdlib-only today; pin it too if it grows deps.

---

## 12. Focused runs — `JAFAR_ONLY`

`build_snapshot.py` accepts a run-scoped universe filter:

```bash
JAFAR_ONLY=BTCUSDT,INTC python3 build_snapshot.py
```

Unknown names are ignored with a warning. This is how you scan one asset for a
CPI print without editing `WATCHLIST`.

**The trap:** a focused run writes only the selected snapshots, but it also
**overwrites `signals.json` and `snapshot_summary.json` with the subset.** The
dashboard and `trader.plan()` read those files. If you leave the subset in place,
the other nine assets vanish from the UI and from proposal planning until the
next full run.

Safe recipe:

```bash
cd "$ENGINE"
cp signals.json "${TMPDIR:-/tmp}/jafar-sig.bak"
cp forecasts.json "${TMPDIR:-/tmp}/jafar-fc.bak"
systemctl --user stop trading-pipeline

set -a; . ./.env; set +a
JAFAR_ONLY=BTCUSDT,INTC python3 -u build_snapshot.py

cp "${TMPDIR:-/tmp}/jafar-sig.bak" signals.json
cp "${TMPDIR:-/tmp}/jafar-fc.bak" forecasts.json
systemctl --user start trading-pipeline
```

Back up **both** files. Restoring only `signals.json` leaves the subset
forecasts in place.

---

## 13. Operations runbook

```bash
ENGINE="$(cd "$(dirname "$0")" && pwd)"
cd "$ENGINE"

# status
systemctl --user status trading-pipeline.service --no-pager
systemctl --user status btc-levels-watch.service --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/

# logs
tail -f service.log
tail -f pipeline_runs_eat.log
journalctl --user -u trading-pipeline.service -f

# restart
systemctl --user restart trading-pipeline.service

# one-off full run (service stopped first)
systemctl --user stop trading-pipeline
python3 build_snapshot.py
systemctl --user start trading-pipeline

# signals only, from existing snapshots
python3 signals.py

# trader inspection
python3 trader.py status
python3 trader.py list
```

### Health checks

- `service.log` ends with `DATA LAYER COMPLETE` and an ACTIONABLE SIGNALS table.
- `signals.json` `data_as_of` is inside the staleness window for the interval.
- `kronos_gate` is present and `z_significant` is meaningful.
- dashboard returns 200 and shows all 11 assets (if it shows fewer, a focused
  run was not restored).
- `btc_levels_state.json` has `"fired": []` after arming unless a level truly
  printed.

### If it crash-loops

1. `journalctl --user -u trading-pipeline.service -n 50`
2. `tail -50 service.log`
3. Most common causes: interpreter moved (missing module), NTFS mount not ready,
   port 5000 occupied, bad `.env` value, unreadable snapshot JSON.

---

## 14. Known traps and code facts

1. **Never use `.venv/`.** It is a stale local venv and may contain binaries
   from another OS. Use the Python pinned by the wrapper scripts.
2. **Only `snapshot_*.json` is globbed.** `.md`, `.log`, state JSON and scratch
   dirs are not read by the engine. Adding docs is safe.
3. **`snapshot_summary.json` matches the glob** and is filtered by name in every
   reader. Do not rename it.
4. **`is_stale` is computed at read time**, so a signal can be fresh in
   `signals.json` and stale when the trader reads it minutes later.
5. **`do_light` never refetches news.** A signal that moves on the 15-min cycle
   is moving on price/derivatives, not new information.
6. **Kronos is a gate, not a voter.** Raising `KRONOS_FACTOR_WEIGHT` above 0
   changes that and lets an unvalidated model originate signals — do it
   deliberately, not by accident.
7. **ATR stops are tight.** ~1.5× ATR(1h) ≈ 0.8 % on BTC, roughly 0.4× daily
   sigma. High stop-out rate is expected. Widen the stop only if you also move
   the targets, or the 2:1 payoff disappears.
8. **Macro weight is 0.05.** It is ~7 % of an equity composite. It informs, it
   does not drive. Crypto has no macro factor at all.
9. **CPI is BLS-first, FRED-fallback**, cached 6 h. A second CPI print inside
   the cache window will not be seen until the cache expires.
10. **`TRADE_DAILY_LOSS_LIMIT` is dead config.** Not enforced. Verify before
    trusting it.
11. **`_prune_orphan_snapshots()` deletes files.** If you temporarily remove an
    asset, its snapshot is gone after the next successful full run.
12. **`run_with_brief()` imports `generate_brief`, but the file is
    `generate brief.py`.** Call the script directly; the function path is
    broken by design of the filename.
13. **One writer.** Manual `build_snapshot.py` while the service is running can
    interleave writes with `do_light`. Stop the service for focused runs.
14. **Level watchers are advisory only.** They cannot see a powered-down host.
    The exchange-side stop is the only guarantee.

---

## 15. File map

```text
ENGINE/
├── run_service.py            service conductor (scheduler + threads)
├── build_snapshot.py         data layer orchestrator + watchlist + JAFAR_ONLY
├── signals.py               factor scoring, gates, levels, sizing
├── kronos_layer.py           forecast subprocess (isolated venv)
├── kronos_src/               vendored Kronos model code
├── fetch_price.py            Binance / yfinance / MEXC OHLCV + perp context
├── fetch_news.py             RSS registry + GDELT + EDGAR + entity split
├── fetch_sentiment.py        Fear&Greed + FinBERT
├── fetch_macro.py            FRED + BLS CPI → gold/oil/equity signals
├── fetch_polymarket.py       price-anchored crowd model
├── macro_calendar.py         FRED/FOMC forward calendar
├── trader.py                 proposals, approval, paper/testnet/live
├── mexc_exec.py              manual MEXC contract CLI
├── dashboard.py              Flask dashboard + approval API
├── alerts.py                 Telegram delivery + cooldowns
├── telegram_bot.py           command bot + LLM chat
├── generate brief.py         HTML brief generator
├── candidate.py              ephemeral out-of-watchlist analysis
├── msci_watch.py             MSCI review watcher + BTC scan
├── watch_btc_levels.py       direction-aware level pings
├── watch_btc_position.py     BTC position milestone manager
├── tp_alert.py               generic TP/SL watcher
├── alert_btc_limit.py        MEXC limit-fill watcher
├── run_service_eat.sh        systemd wrapper (interpreter pin)
├── run_pipeline_eat.sh       one-shot wrapper (interpreter pin)
├── start_dashboard.sh        manual Flask launcher
├── Dockerfile / DEPLOY.md    Railway container
├── AGENTS.md / PIPELINE.md / README.md / ARCHITECTURE.md
└── snapshot_*.json, signals.json, forecasts.json, *.log, state JSON
```

---

## 16. Doc drift — what's stale elsewhere

The older docs are useful but out of date in these places:

| Claim | Reality |
|---|---|
| `discover.py` and `fade_tracker.py` exist | Both deleted; no `/discover` or `/fades` in the bot |
| `dashboard/app.py` | It is `dashboard.py` (single file, inline template) |
| Repo at `~/TradingPipeline` or `.../CODE/TradingPipeline` | Live engine is this directory on the NTFS mount |
| `TRADE_DAILY_LOSS_LIMIT` is a rail | Declared, never enforced |
| Working dir / launch instructions | Use `run_service_eat.sh`; the systemd unit pins TZ and Python |

`README.md` is still the right place for the Telegram/Railway setup flow.
`PIPELINE.md` is still a good conceptual overview. This file is the accurate
mechanism reference.

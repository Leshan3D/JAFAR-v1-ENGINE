"""
DATA LAYER ORCHESTRATOR — JAFAR Intelligence
Combines price + news + macro + sentiment + polymarket into one JSON snapshot.

Snapshot feeds:
  price_data           -> Kronos (Forecast Layer)
  news_headlines       -> Claude Haiku (Reasoning Layer)
  news_sentiment       -> Claude Haiku (Reasoning Layer)
  macro_context        -> Claude Haiku (Reasoning Layer)
  polymarket_signals   -> Claude Haiku (Reasoning Layer)
  fear_greed           -> Claude Haiku (Reasoning Layer)

Watchlist:
  BTCUSDT, ETHUSDT, SOLUSDT, ZECUSDT  - Crypto (Binance)
  GC=F                          - Gold Futures (yfinance)
  BZ=F                          - Brent Crude Oil (yfinance)
  NVDA                        - Equity (yfinance)
  DX-Y.NYB                    - DXY Dollar Index (yfinance)

Dead sources removed: Reuters, AP Business, Reddit
"""

import json
import os
import re
import time
import subprocess
import pandas as pd
from datetime import datetime, timezone, timedelta

from fetch_price import (fetch_binance_ohlcv, fetch_stock_ohlcv,
                         fetch_mexc_ohlcv, fetch_mexc_ticker,
                         fetch_binance_futures_context, estimate_annualized_vol)
from fetch_news import fetch_news_for_asset, source_health_summary
from fetch_sentiment import fetch_fear_greed_index, FinBERTSentiment
from fetch_macro import fetch_all_macro
from fetch_polymarket import fetch_polymarket_snapshot, ASSET_QUERIES, derive_signal
from signals import evaluate_snapshot, compute_freshness


# Staleness thresholds live in signals.STABLE_AFTER_BY_INTERVAL (per bar size).
STALE_AFTER_SECONDS = 150 * 60  # default only; see compute_freshness()

# Below this many asset-specific headlines, the market-wide feed is a poor
# proxy and the news factor must say so rather than quietly voting.
MIN_ASSET_SPECIFIC_HEADLINES = 5

# Word-boundary entity matchers so a headline counts as asset-specific only
# when it actually names the asset.
NEWS_ENTITY_PATTERNS = {
    "BTCUSDT":  [r"\bbitcoin\b", r"\bbtc\b"],
    "ETHUSDT":  [r"\bethereum\b", r"\beth\b"],
    "SOLUSDT":  [r"\bsolana\b", r"\bsol\b"],
    "ZECUSDT":  [r"\bzcash\b", r"\bzec\b"],
    "GC=F":     [r"\bgold\b", r"\bcomex\b", r"\b bullion\b"],
    "BZ=F":     [r"\bcrude\b", r"\bbrent\b", r"\bopec\b", r"\boil price\b"],
    "NVDA":     [r"\bnvidia\b", r"\bnvda\b"],
    "META":     [r"\bmeta platforms\b", r"\bmeta\b", r"\bfacebook\b"],
    "AMD":      [r"\badvanced micro devices\b", r"\bamd\b"],
    "INTC":     [r"\bintel\b", r"\bintc\b", r"\bfoundry\b"],
    "DX-Y.NYB": [r"\bdollar\b", r"\bdxy\b"],
}
NEWS_ENTITY_PATTERNS = {
    k: [re.compile(p, re.I) for p in v] for k, v in NEWS_ENTITY_PATTERNS.items()
}


OI_TRACK_FILE = "oi_track.json"


def update_oi_track(symbol, oi_usd):
    """
    Keep a short history of open-interest samples so the 24h change is
    available.

    An absolute open-interest number says very little on its own; what
    matters is whether leverage is being added or pulled. The first runs
    only establish the baseline, so the change is None until there is
    history.
    """
    if not oi_usd:
        return None
    try:
        data = {}
        if os.path.exists(OI_TRACK_FILE):
            with open(OI_TRACK_FILE) as f:
                data = json.load(f)
        now = datetime.now(timezone.utc)
        series = data.get(symbol) or []
        series.append({"ts": now.isoformat(), "oi": float(oi_usd)})
        cutoff = (now - timedelta(days=7)).isoformat()
        series = [p for p in series if p.get("ts", "") >= cutoff]
        data[symbol] = series
        with open(OI_TRACK_FILE, "w") as f:
            json.dump(data, f, indent=1)

        target = (now - timedelta(hours=24)).isoformat()
        prior = [p for p in series if p.get("ts", "") <= target]
        if prior:
            base = float(prior[-1]["oi"])
            if base:
                return round((float(oi_usd) / base - 1) * 100, 2)
    except Exception as e:  # noqa: BLE001
        print(f"  WARN — OI track failed: {e}")
    return None


def split_news_by_entity(headlines, asset):
    """Split headlines into (asset_specific, market_wide)."""
    pats = NEWS_ENTITY_PATTERNS.get(asset)
    if not pats:
        return list(headlines), []
    specific, market_wide = [], []
    for h in headlines:
        (specific if any(p.search(h) for p in pats) else market_wide).append(h)
    return specific, market_wide


# Correlation at or above this makes a crypto-wide feed genuinely informative
# for the asset rather than a weak substitute. Below it, market-wide news is
# still a downgrade and should keep saying so.
BETA_JUSTIFIED_CORR = 0.50

_BTC_DAILY_CACHE = {"closes": None, "polymarket": None}


def _btc_reference_daily_closes():
    """Daily closes for BTCUSDT — the reference asset for beta.

    BTCUSDT is the first WATCHLIST entry, so by the time any alt is processed
    its daily_closes are already in the cache. Falls back to the on-disk
    snapshot, then to None so beta context is simply omitted.
    """
    if _BTC_DAILY_CACHE["closes"]:
        return _BTC_DAILY_CACHE["closes"]
    try:
        with open("snapshot_BTCUSDT.json") as fh:
            dc = json.load(fh).get("daily_closes")
        if dc:
            _BTC_DAILY_CACHE["closes"] = dc
            return dc
    except Exception:
        pass
    return None


def compute_beta(asset_daily, ref_daily):
    """OLS beta and correlation of asset daily returns vs the reference.

    Log returns, so the beta is scale-free and comparable across assets.
    """
    try:
        import numpy as np
        if not asset_daily or not ref_daily:
            return None
        n = min(len(asset_daily), len(ref_daily))
        if n < 30:
            return None
        a = np.diff(np.log(np.asarray(asset_daily[-n:], dtype=float)))
        r = np.diff(np.log(np.asarray(ref_daily[-n:], dtype=float)))
        if a.std() == 0 or r.std() == 0:
            return None
        corr = float(np.corrcoef(a, r)[0, 1])
        beta = float(np.polyfit(r, a, 1)[0])
        return {"corr": round(corr, 3), "beta": round(beta, 2),
                "bars": n - 1, "reference": "BTCUSDT"}
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# WATCHLIST
# ─────────────────────────────────────────────────────────────
# Kronos forecasts over `kronos_pred_len` bars. Crypto/equities carry 1h
# candles, so the default horizon (KRONOS_PRED_LEN, 24) is ~1 day. The daily
# assets (gold, Brent, DXY) need a SHORT pred_len or Kronos forecasts a month
# out — mismatched with Jafar's 1-3 day tactical holds. Assets absent from this
# map fall back to the layer default.
KRONOS_PRED_LEN_BY_ASSET = {
    "GC=F":     3,
    "BZ=F":     3,
    "DX-Y.NYB": 3,
}

WATCHLIST = [
    {
        "asset":              "BTCUSDT",
        "label":              "BITCOIN",
        "asset_type":         "crypto",
        "news_asset_type":    "crypto",
        "polymarket_key":     "bitcoin",
        "geopolitical_query": "Bitcoin cryptocurrency regulation",
        "candle_interval":    "1h",
        "candle_limit":       400,
        "ticker":             None,
    },
    {
        "asset":              "ETHUSDT",
        "label":              "ETHEREUM",
        "asset_type":         "crypto",
        "news_asset_type":    "crypto",
        "polymarket_key":     "ethereum",
        "geopolitical_query": "Ethereum DeFi blockchain",
        "candle_interval":    "1h",
        "candle_limit":       400,
        "ticker":             None,
    },
    {
        "asset":              "SOLUSDT",
        "label":              "SOLANA",
        "asset_type":         "crypto",
        "news_asset_type":    "crypto",
        "polymarket_key":     "solana",
        "geopolitical_query": "Solana crypto market",
        "candle_interval":    "1h",
        "candle_limit":       400,
        "ticker":             None,
    },
    {
        "asset":              "GC=F",
        "label":              "GOLD FUTURES",
        "asset_type":         "commodity",
        "news_asset_type":    "gold",
        "polymarket_key":     "gold",
        "geopolitical_query": "central bank gold Federal Reserve inflation",
        "candle_interval":    "1d",
        "candle_limit":       200,
        "ticker":             None,
        # MEXC is the execution venue; capture its gold price alongside the
        # COMEX futures feed so the basis can be reported and applied.
        "mexc_reference_symbol": "XAU_USDT",
    },
    {
        "asset":              "ZECUSDT",
        "label":              "ZCASH",
        "asset_type":         "crypto",
        "news_asset_type":    "crypto",
        "polymarket_key":     "zcash",
        "geopolitical_query": "Zcash cryptocurrency privacy coin market",
        "candle_interval":    "1h",
        "candle_limit":       400,
        "ticker":             None,
    },
    {
        "asset":              "BZ=F",
        "label":              "BRENT CRUDE",
        "asset_type":         "commodity",
        "news_asset_type":    "commodity",
        "polymarket_key":     "oil",
        "geopolitical_query": "OPEC oil Iran ceasefire Brent supply",
        "candle_interval":    "1d",
        "candle_limit":       200,
        "ticker":             None,
        # MEXC is the execution venue; capture its Brent price alongside the
        # ICE futures feed so the basis can be reported and applied.
        "mexc_reference_symbol": "UKOIL_USDT",
    },
    {
        "asset":              "NVDA",
        "label":              "NVDA",
        "asset_type":         "stock",
        "news_asset_type":    "stock",
        "polymarket_key":     "tech",
        "geopolitical_query": "AI chips semiconductor export controls",
        "candle_interval":    "1h",
        "candle_limit":       400,
        "ticker":             "NVDA",
        # MEXC tokenized-stock perp (the execution venue) captured alongside the
        # yfinance feed so the basis can be reported and applied to the levels.
        "mexc_reference_symbol": "NVIDIA_USDT",
    },
    {
        "asset":              "META",
        "label":              "META PLATFORMS",
        "asset_type":         "stock",
        "news_asset_type":    "stock",
        "polymarket_key":     "meta",
        "geopolitical_query": "Meta Platforms advertising AI antitrust",
        "candle_interval":    "1h",
        "candle_limit":       400,
        "ticker":             "META",
        # MEXC tokenized-stock perp (the execution venue) captured alongside the
        # yfinance feed so the basis can be reported and applied to the levels.
        "mexc_reference_symbol": "METASTOCK_USDT",
    },
    {
        "asset":              "AMD",
        "label":              "AMD",
        "asset_type":         "stock",
        "news_asset_type":    "stock",
        "polymarket_key":     "amd",
        "geopolitical_query": "AMD semiconductor chips GPU export controls",
        "candle_interval":    "1h",
        "candle_limit":       400,
        "ticker":             "AMD",
        # MEXC tokenized-stock perp (the execution venue) captured alongside the
        # yfinance feed so the basis can be reported and applied to the levels.
        "mexc_reference_symbol": "AMDSTOCK_USDT",
    },
    {
        "asset":              "INTC",
        "label":              "INTEL",
        "asset_type":         "stock",
        "news_asset_type":    "stock",
        "polymarket_key":     "intel",
        "geopolitical_query": "Intel foundry semiconductor AI chips",
        "candle_interval":    "1h",
        "candle_limit":       400,
        "ticker":             "INTC",
        # MEXC tokenized-stock perp (the execution venue) captured alongside the
        # yfinance feed so the basis can be reported and applied to the levels.
        "mexc_reference_symbol": "INTCSTOCK_USDT",
    },
    {
        "asset":              "DX-Y.NYB",
        "label":              "DXY INDEX",
        "asset_type":         "stock",
        "news_asset_type":    "macro",
        "polymarket_key":     "macro",
        "geopolitical_query": "US dollar Federal Reserve monetary policy",
        "candle_interval":    "1d",
        "candle_limit":       90,
        "ticker":             None,
    },
]


# Optional run-scoped universe filter. `JAFAR_ONLY=BTCUSDT,INTC` limits a run
# to the named assets without editing the watchlist, so a focused scan can be
# done against a specific CPI/FOMC event and the full sweep resumed afterwards.
_only = [a.strip().upper() for a in
         (os.environ.get("JAFAR_ONLY") or "").split(",") if a.strip()]
if _only:
    _known = {w["asset"].upper() for w in WATCHLIST}
    _unknown = [a for a in _only if a not in _known]
    if _unknown:
        print(f"  [watchlist] JAFAR_ONLY — unknown asset(s) ignored: {_unknown}")
    WATCHLIST = [w for w in WATCHLIST if w["asset"].upper() in set(_only)]
    print(f"  [watchlist] JAFAR_ONLY active → {[w['asset'] for w in WATCHLIST]}")


def validate_watchlist(watchlist):
    """
    Fail loudly on mis-wiring instead of silently substituting another asset's
    data. SOLUSDT once pointed at polymarket_key="bitcoin" (with no "solana"
    key in ASSET_SEARCHES at all), so Solana's crowd factor was computed from
    Bitcoin markets and produced a fabricated STRONGLY_BEARISH reading.
    """
    known = set(ASSET_QUERIES)
    errors = []
    for entry in watchlist:
        key = entry.get("polymarket_key")
        if key not in known:
            errors.append(
                f"{entry['asset']}: polymarket_key={key!r} not in "
                f"ASSET_SEARCHES {sorted(known)}"
            )
    dupes = {}
    for entry in watchlist:
        dupes.setdefault(entry.get("polymarket_key"), []).append(entry["asset"])
    for key, assets in dupes.items():
        if len(assets) > 1:
            errors.append(
                f"polymarket_key={key!r} shared by {assets} — each asset needs "
                f"its own markets or the factors are cross-contaminated"
            )
    if errors:
        raise ValueError(
            "WATCHLIST mis-wiring detected:\n  - " + "\n  - ".join(errors)
        )


validate_watchlist(WATCHLIST)


def build_snapshot(asset_config, finbert=None, fear_greed=None,
                   macro_context=None, polymarket_data=None):
    """
    Build one complete data snapshot for a single asset.
    """
    asset          = asset_config["asset"]
    label          = asset_config.get("label", asset)
    asset_type     = asset_config["asset_type"]
    news_type      = asset_config["news_asset_type"]
    geo_query      = asset_config.get("geopolitical_query")
    ticker         = asset_config.get("ticker")
    polymarket_key = asset_config.get("polymarket_key", "macro")

    print(f"\n{'='*60}")
    print(f"  SNAPSHOT: {label} ({asset})")
    print(f"{'='*60}")

    snapshot = {
        "snapshot_time":     datetime.now(timezone.utc).isoformat(),
        "asset":             asset,
        "label":             label,
        "asset_type":        asset_type,
        "current_price":     None,
        "price_data":        [],
        "daily_closes":      None,
        "btc_beta":          None,
        "btc_context":       None,
        "candle_interval":   asset_config.get("candle_interval"),
        "candle_limit":      asset_config.get("candle_limit"),
        # Per-asset Kronos horizon; None lets the layer use KRONOS_PRED_LEN.
        "kronos_pred_len":   KRONOS_PRED_LEN_BY_ASSET.get(asset),
        "news_headlines":    [],
        "news_sources_hit":  [],
        "news_sentiment":    [],
        "news_scope":        None,
        "news_scope_note":   None,
        "overall_sentiment": "UNKNOWN",
        "sentiment_counts":  {},
        "avg_confidence":    None,
        "fear_greed":        fear_greed if asset_type == "crypto" else None,
        "macro_context":     None,
        "polymarket_signals": None,
        "errors":            [],
        # MEXC reference ticker (execution venue) + basis vs the futures feed.
        "mexc_reference":    None,
        # Derivatives positioning context (funding, basis, open interest).
        "derivatives":       None,
        # Annualised volatility, used as the strike-distance baseline for the
        # strike-relative prediction-market score.
        "annualized_vol":    None,
        # Provenance of the news layer: when it was pulled and which sources
        # actually answered.
        "news_fetched_at":   None,
        "news_sources_failed": [],
        "news_source_health": {},
        # Freshness provenance. Consumers (dashboard, notes, agents) previously
        # had no way to tell a 5-minute-old snapshot from a 3-hour-old one —
        # a run at 07:14 UTC was still being quoted as current at 10:40 UTC.
        "data_as_of":        None,
        "age_seconds":       None,
        "is_stale":          None,
        "stale_after_seconds": STALE_AFTER_SECONDS,
    }

    # Attach macro context to relevant asset types. Stocks are included so the
    # equity entry can inherit the CPI/rates tilt (equity_macro_signal); before
    # this they had no macro backdrop at all and were blind to CPI.
    if macro_context and news_type in ("gold", "macro", "commodity", "stock"):
        snapshot["macro_context"] = {
            "gold_macro_signal":    macro_context.get("gold_macro_signal"),
            "gold_macro_reasoning": macro_context.get("gold_macro_reasoning"),
            "oil_macro_signal":     macro_context.get("oil_macro_signal"),
            "oil_macro_reasoning":  macro_context.get("oil_macro_reasoning"),
            "equity_macro_signal":  macro_context.get("equity_macro_signal"),
            "equity_macro_reasoning": macro_context.get("equity_macro_reasoning"),
            "inflation_signal":     macro_context.get("inflation_signal"),
            "cpi":                  macro_context.get("cpi"),
            "yield_curve_status":   macro_context.get("yield_curve_status"),
            "yield_curve_spread":   macro_context.get("yield_curve_spread"),
            "DGS10":  macro_context.get("DGS10",  {}).get("value"),
            "DGS2":   macro_context.get("DGS2",   {}).get("value"),
            "DFII10": macro_context.get("DFII10", {}).get("value"),
            "T10YIE": macro_context.get("T10YIE", {}).get("value"),
            "FEDFUNDS": macro_context.get("FEDFUNDS", {}).get("value"),
            "DTWEXBGS": macro_context.get("DTWEXBGS", {}).get("value"),
        }

    # Polymarket signals are attached after the price fetch (see below) so
    # the crowd factor can be weighted by moneyness around spot.

    # ── 1. Price Data ──────────────────────────────────────────
    print(f"\n[1/3] Price data...")
    try:
        _mexc = asset_config.get("source") == "mexc"
        _mexc_sym = asset_config.get("mexc_symbol")
        _interval_map = {"5m": "Min5", "15m": "Min15", "30m": "Min30",
                         "1h": "Min60", "4h": "Hour4", "1d": "Day1"}
        _mexc_interval = _interval_map.get(asset_config["candle_interval"], "Min60")
        if _mexc:
            df = fetch_mexc_ohlcv(_mexc_sym, interval=_mexc_interval,
                                  limit=asset_config["candle_limit"])
        elif asset_type == "crypto":
            df = fetch_binance_ohlcv(
                asset,
                interval=asset_config["candle_interval"],
                limit=asset_config["candle_limit"]
            )
        else:
            period = "200d" if asset_config["candle_limit"] > 100 else "60d"
            df = fetch_stock_ohlcv(asset, period=period,
                                   interval=asset_config["candle_interval"])

        snapshot["price_data"]    = df[["timestamps","open","high",
                                        "low","close","volume","amount"]
                                      ].to_dict(orient="records")
        snapshot["current_price"] = float(df["close"].iloc[-1])
        snapshot["candle_interval"] = asset_config["candle_interval"]
        snapshot["candle_limit"]    = asset_config["candle_limit"]
        print(f"  OK — {len(snapshot['price_data'])} {asset_config['candle_interval']} candles | "
              f"Current: {snapshot['current_price']:.4f}")

        # MEXC reference price — the venue the operator actually trades on.
        # Captured alongside the futures feed so the basis between MEXC and
        # COMEX/ICE can be reported and applied to the levels. Candle
        # history/momentum stay on the futures feed.
        mexc_ref_sym = asset_config.get("mexc_reference_symbol")
        if mexc_ref_sym:
            try:
                ref = fetch_mexc_ticker(mexc_ref_sym)
                if ref.get("last"):
                    basis = ref["last"] - snapshot["current_price"]
                    basis_pct = (ref["last"] / snapshot["current_price"] - 1) * 100
                    ref["reference_futures_price"] = snapshot["current_price"]
                    ref["basis_vs_futures"] = round(basis, 4)
                    ref["basis_pct"] = round(basis_pct, 4)
                    ref["note"] = (
                        "MEXC is the execution venue; candle history/momentum "
                        "still come from the yfinance futures feed. Add "
                        "basis_vs_futures to shift futures levels into MEXC terms.")
                    snapshot["mexc_reference"] = ref
                    print(f"  MEXC ref {mexc_ref_sym}: last {ref['last']:.4f} "
                          f"(basis {basis:+.4f}, {basis_pct:+.2f}% vs futures)")
            except Exception as e:
                msg = f"MEXC reference fetch failed ({mexc_ref_sym}): {e}"
                print(f"  WARN — {msg}")
                snapshot["errors"].append(msg)

        # Daily candles for the RSI leg. RSI_OVERBOUGHT/OVERSOLD (70/30) are
        # daily conventions; scoring them off 1h bars produced a fake 68.7
        # overbought reading on a chart whose true daily RSI was 62.1.
        if asset_config["candle_interval"] != "1d":
            try:
                if _mexc:
                    ddf = fetch_mexc_ohlcv(_mexc_sym, interval="Day1", limit=120)
                elif asset_type == "crypto":
                    ddf = fetch_binance_ohlcv(asset, interval="1d", limit=120)
                else:
                    ddf = fetch_stock_ohlcv(asset, period="1y", interval="1d")
                snapshot["daily_closes"] = [float(x) for x in ddf["close"].tolist()]
                print(f"  OK — {len(snapshot['daily_closes'])} daily closes for RSI")
                if asset == "BTCUSDT":
                    # Seed the reference series every other asset's beta uses.
                    _BTC_DAILY_CACHE["closes"] = snapshot["daily_closes"]
            except Exception as e:
                print(f"  WARN — daily candles unavailable ({e}); "
                      f"RSI falls back to {asset_config['candle_interval']}")

        # Annualised volatility from daily closes where available (daily bars
        # are the right unit: an hourly stdev understates the real move
        # distribution and would make every strike look further out of the
        # money than it is).
        #
        # Two corrections that matter:
        #  - bars per year: crypto trades 365 days, stocks/commodities/FX
        #    trade ~252. Scaling a stock's daily moves by sqrt(365) inflates
        #    its vol by 20% for no reason.
        #  - regime weight: the strike-relative baseline needs the vol the
        #    market is trading at now, not the vol of a year that included a
        #    crisis. A blend of the last 60 sessions and the full history is
        #    the usual compromise between a noisy short window and a stale
        #    long one.
        closes = snapshot.get("daily_closes")
        if closes:
            series = closes
        elif asset_config["candle_interval"] == "1d":
            series = [c["close"] for c in snapshot["price_data"]]
        else:
            series = [c["close"] for c in snapshot["price_data"]]
        bars_per_year = 365 if asset_config.get("asset_type") == "crypto" else 252
        ann_vol = estimate_annualized_vol(series, bars_per_year=bars_per_year)
        vol_recent = estimate_annualized_vol(series[-61:], bars_per_year=bars_per_year)
        baseline_vol = None
        if ann_vol and vol_recent:
            baseline_vol = 0.5 * ann_vol + 0.5 * vol_recent
        else:
            baseline_vol = ann_vol or vol_recent
        snapshot["annualized_vol"] = ann_vol
        snapshot["annualized_vol_recent"] = vol_recent
        snapshot["annualized_vol_baseline"] = baseline_vol
        if ann_vol:
            print(f"  Annualised vol: {ann_vol:.1%} "
                  f"(recent {vol_recent:.1%}, baseline {baseline_vol:.1%})")

        # Derivatives positioning context.
        #  - Crypto trades on Binance USDT-M futures, so that is the tape.
        #  - Gold / Brent / tokenised equities are quoted on MEXC, whose
        #    ticker already carries funding and open interest.
        try:
            if asset_type == "crypto":
                deriv = fetch_binance_futures_context(asset)
                if deriv:
                    deriv["basis_pct"] = (
                        (deriv["mark_price"] - deriv["index_price"])
                        / deriv["index_price"] * 100
                        if deriv.get("mark_price") and deriv.get("index_price")
                        else None)
                    deriv["open_interest_change_pct_24h"] = update_oi_track(
                        asset, deriv.get("open_interest_usd"))
                    snapshot["derivatives"] = deriv
                    print(f"  Derivatives: funding "
                          f"{deriv.get('funding_rate_annualized_pct', 0):+.1f}%/yr, "
                          f"OI ${(deriv.get('open_interest_usd') or 0)/1e6:,.0f}M")
            else:
                ref = snapshot.get("mexc_reference") or {}
                if ref.get("funding_rate_annualized_pct") is not None:
                    snapshot["derivatives"] = {
                        "venue": "MEXC",
                        "symbol": ref.get("symbol"),
                        "mark_price": ref.get("last"),
                        "index_price": ref.get("index"),
                        "last_funding_rate": ref.get("last_funding_rate"),
                        "funding_rate_annualized_pct":
                            ref.get("funding_rate_annualized_pct"),
                        "basis_pct": ref.get("basis_pct"),
                        "open_interest_contracts": ref.get("open_interest_contracts"),
                        "open_interest_usd": ref.get("open_interest_usd"),
                        "quote_volume_24h": ref.get("quote_volume_24h"),
                        "price_change_pct_24h": ref.get("price_change_pct_24h"),
                        "as_of": ref.get("as_of"),
                    }
        except Exception as e:  # noqa: BLE001
            msg = f"Derivatives context failed: {e}"
            print(f"  WARN — {msg}")
            snapshot["errors"].append(msg)

        # Beta to BTC, measured rather than assumed. BTCUSDT is first in the
        # watchlist so its series is already cached by the time alts run.
        # Crypto only: "how much does this track BTC" is a real question for
        # SOL/ETH/ZEC and a meaningless one for gold, crude or NVDA.
        if asset != "BTCUSDT" and asset_type == "crypto":
            ref = _btc_reference_daily_closes()
            b = compute_beta(snapshot.get("daily_closes"), ref)
            if b:
                snapshot["btc_beta"] = b
                print(f"  Beta vs BTC — corr {b['corr']}, beta {b['beta']} "
                      f"({b['bars']} daily bars)")
            else:
                print("  Beta vs BTC — unavailable (need >=30 aligned daily bars)")

    except Exception as e:
        msg = f"Price fetch failed: {e}"
        print(f"  ERROR — {msg}")
        snapshot["errors"].append(msg)

    # Polymarket is attached AFTER price so the crowd factor can be weighted
    # by moneyness around the asset's own spot.
    if polymarket_data:
        markets = (polymarket_data.get("by_asset") or {}).get(polymarket_key)
        signals = polymarket_data.get("signals", {}).get(polymarket_key)
        if signals is not None:
            signals = derive_signal(
                markets or [], spot=snapshot.get("current_price"),
                ann_vol=snapshot.get("annualized_vol_baseline"))
        snapshot["polymarket_signals"] = signals
        # Seed the reference crowd read for every other asset's context.
        if polymarket_key == "bitcoin" and signals:
            _BTC_DAILY_CACHE["polymarket"] = signals

    # Every non-BTC asset carries BTC's crowd read as context. Confidence-gated
    # downstream in signals.py — a thin BTC sample must not leak into an alt.
    # Crypto only, for the same reason as beta: BTC's crowd says nothing
    # useful about crude, the dollar, or a single stock.
    if (asset != "BTCUSDT" and asset_type == "crypto"
            and snapshot.get("btc_beta") and _BTC_DAILY_CACHE["polymarket"]):
        btcsig = _BTC_DAILY_CACHE["polymarket"]
        snapshot["btc_context"] = {
            "polymarket": {
                "signal": btcsig.get("signal"),
                "confidence": btcsig.get("confidence"),
                "markets_used": btcsig.get("markets_used"),
                "avg_prob_yes": btcsig.get("avg_prob_yes"),
            },
            "note": "BTCUSDT crowd read, context only — never added to this asset's composite",
        }

    # ── 2. News ────────────────────────────────────────────────
    print(f"\n[2/3] News ({news_type})...")
    try:
        news_df = fetch_news_for_asset(
            asset_type=news_type,
            ticker=ticker,
            geopolitical_query=geo_query,
            max_per_source=8
        )
        if not news_df.empty and "title" in news_df.columns:
            headlines = news_df["title"].dropna().tolist()
            sources   = news_df["source"].unique().tolist() \
                        if "source" in news_df.columns else []

            # Split asset-specific headlines from market-wide ones. The RSS
            # feeds are crypto-wide, so Solana's factor was being decided by
            # items like "South Korea crypto exchange profits fall 78%" while
            # the record $188M ETF week never surfaced.
            specific, market_wide = split_news_by_entity(headlines, asset)
            snapshot["news_specific_count"]   = len(specific)
            snapshot["news_market_wide_count"] = len(market_wide)
            snapshot["news_market_wide_sample"] = market_wide[:5]

            if len(specific) >= MIN_ASSET_SPECIFIC_HEADLINES:
                snapshot["news_headlines"]  = specific
                snapshot["news_scope"]      = "asset_specific"
                snapshot["news_scope_note"] = (
                    f"{len(specific)} of {len(headlines)} headlines name this asset")
                print(f"  OK — {len(headlines)} headlines, "
                      f"{len(specific)} asset-specific (USING)")
            else:
                # Whether a crypto-wide fallback is a *downgrade* depends on
                # whether the asset actually tracks the crypto market. SOL
                # correlates ~0.82 with BTC at beta ~1.2, so crypto-wide news
                # is its primary driver, not a consolation prize — labelling
                # it a weak proxy was backwards. Judge it from measured beta.
                b = snapshot.get("btc_beta") or {}
                justified = (b.get("corr") or 0) >= BETA_JUSTIFIED_CORR
                snapshot["news_headlines"]  = headlines
                snapshot["news_scope"]      = "market_beta" if justified else "market_wide"
                if justified:
                    snapshot["news_scope_note"] = (
                        f"only {len(specific)} asset-specific (min "
                        f"{MIN_ASSET_SPECIFIC_HEADLINES}); using {len(headlines)} "
                        f"crypto-wide headlines — but {asset} tracks BTC at "
                        f"corr {b.get('corr')} / beta {b.get('beta')}, so these "
                        f"carry real signal, not a weak proxy")
                else:
                    corr = b.get("corr")
                    snapshot["news_scope_note"] = (
                        f"only {len(specific)} asset-specific (min "
                        f"{MIN_ASSET_SPECIFIC_HEADLINES}); scored market-wide "
                        f"news"
                        + (f" with no BTC relationship (corr {corr})"
                           if corr is not None else "")
                        + f", so this factor is a WEAK proxy for {asset}")
                tag = "BTC-BETA (JUSTIFIED)" if justified else "WEAK PROXY"
                print(f"  WARN — {len(specific)} asset-specific "
                      f"(min {MIN_ASSET_SPECIFIC_HEADLINES}); using all "
                      f"{len(headlines)} market-wide headlines [{tag}]")
            snapshot["news_sources_hit"] = sources
            print(f"  Sources: {', '.join(sources)}")
        snapshot["news_fetched_at"] = datetime.now(timezone.utc).isoformat()
        health = source_health_summary()
        snapshot["news_sources_failed"] = health["sources_failed"]
        snapshot["news_sources_empty"] = health.get("sources_empty", [])
        snapshot["news_source_health"] = health["detail"]
        if health["sources_failed"]:
            print(f"  WARN — dead sources this run: "
                  f"{', '.join(health['sources_failed'])}")
        if health.get("sources_empty"):
            print(f"  NOTE — sources with nothing to publish: "
                  f"{', '.join(health['sources_empty'])}")
        if not snapshot.get("news_headlines"):
            print("  WARNING — No headlines fetched")
    except Exception as e:
        msg = f"News fetch failed: {e}"
        print(f"  ERROR — {msg}")
        snapshot["errors"].append(msg)

    # ── 3. FinBERT Sentiment ───────────────────────────────────
    print(f"\n[3/3] Sentiment scoring...")
    if finbert and snapshot["news_headlines"]:
        try:
            scores_df = finbert.score(snapshot["news_headlines"])
            snapshot["news_sentiment"] = scores_df.to_dict(orient="records")

            pos = int((scores_df["sentiment"] == "positive").sum())
            neg = int((scores_df["sentiment"] == "negative").sum())
            neu = int((scores_df["sentiment"] == "neutral").sum())
            avg = float(scores_df["confidence"].mean())

            snapshot["sentiment_counts"]  = {"positive": pos,
                                             "negative": neg,
                                             "neutral":  neu}
            snapshot["avg_confidence"]    = round(avg, 4)
            snapshot["overall_sentiment"] = (
                "BULLISH" if pos > neg else
                "BEARISH" if neg > pos else
                "NEUTRAL"
            )
            print(f"  Pos:{pos} Neg:{neg} Neu:{neu} | "
                  f"Conf:{avg:.1%} | Signal:{snapshot['overall_sentiment']}")

        except Exception as e:
            msg = f"FinBERT failed: {e}"
            print(f"  ERROR — {msg}")
            snapshot["errors"].append(msg)
    else:
        print("  Skipped (no headlines or FinBERT not loaded)")

    # ── Freshness provenance ───────────────────────────────────
    # The newest bar we actually hold, not the wall-clock write time.
    # Staleness is interval-aware (a daily bar is current for a whole day) and
    # is recomputed again at read time by signals.compute_freshness().
    if snapshot["price_data"]:
        fresh = compute_freshness(
            snapshot["price_data"][-1].get("timestamps"),
            interval=snapshot.get("candle_interval") or "1h",
        )
        snapshot.update(fresh)
        if fresh.get("age_seconds") is not None:
            flag = "STALE" if fresh["is_stale"] else "fresh"
            print(f"  Data as-of {str(fresh['data_as_of'])[:16]} "
                  f"({fresh['age_seconds']/60:.0f} min old, {flag}, "
                  f"threshold {fresh['stale_after_seconds']/60:.0f}m)")

    return snapshot


def run_kronos_layer():
    """
    Kronos Forecast Layer (auto). Kronos pins pandas==2.2.2 while this engine
    runs pandas 3.x, so it lives in its own venv and is invoked as a subprocess.
    Best-effort: a failure here never kills the data layer / signal engine.

    Controls (env):
      KRONOS_ENABLED   "0" disables (default "1")
      KRONOS_PYTHON    interpreter with Kronos deps
                       (default ~/.venvs/trading-kronos/bin/python)
      KRONOS_TIMEOUT_S subprocess timeout (default 1800)
    """
    if os.environ.get("KRONOS_ENABLED", "1").lower() in ("0", "false", "no"):
        print("\n  [kronos] disabled (KRONOS_ENABLED=0)")
        return
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "kronos_layer.py")
    py = os.environ.get("KRONOS_PYTHON") or os.path.expanduser(
        "~/.venvs/trading-kronos/bin/python")
    if not os.path.exists(script):
        print("  [kronos] kronos_layer.py not found — skipping")
        return
    if not os.path.exists(py):
        print(f"  [kronos] venv python not found at {py} — skipping "
              f"(set KRONOS_PYTHON or KRONOS_ENABLED=0 to silence)")
        return
    env = dict(os.environ)
    # Snapshots are written relative to the CWD (or DATA_DIR in service mode);
    # point the layer at the same place so it reads what we just wrote.
    data_dir = env.get("DATA_DIR") or os.getcwd()
    env["DATA_DIR"] = data_dir
    try:
        p = subprocess.run(
            [py, script], cwd=data_dir, env=env,
            timeout=int(os.environ.get("KRONOS_TIMEOUT_S", "1800")),
        )
        print(f"  [kronos] forecast layer "
              f"{'OK' if p.returncode == 0 else f'FAILED (rc {p.returncode})'}")
    except subprocess.TimeoutExpired:
        print("  [kronos] WARN — forecast layer timed out (non-fatal)")
    except Exception as e:
        print(f"  [kronos] WARN — forecast layer failed: {e} (non-fatal)")


def _reload_kronos_block(all_snapshots):
    """Re-read the kronos_forecast block the layer injected into each snapshot
    file so the in-memory signal engine sees it this same run."""
    for s in all_snapshots:
        safe = (s["asset"].replace("=", "_").replace("/", "_")
                           .replace("-", "_").replace(".", "_"))
        try:
            with open(f"snapshot_{safe}.json") as fh:
                block = json.load(fh).get("kronos_forecast")
            if block:
                s["kronos_forecast"] = block
        except (FileNotFoundError, json.JSONDecodeError):
            pass


def run_full_data_layer(use_finbert=True):
    """
    Full pipeline run across all assets in WATCHLIST.
    """
    print("\n" + "="*60)
    print("  JAFAR INTELLIGENCE — DATA LAYER")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("="*60)

    # ── Load FinBERT once ──────────────────────────────────────
    finbert = None
    if use_finbert:
        try:
            finbert = FinBERTSentiment()
        except Exception as e:
            print(f"\nWARNING: FinBERT failed ({e}) — no NLP scoring")

    # ── Fear & Greed (crypto) ──────────────────────────────────
    fear_greed = None
    try:
        fng = fetch_fear_greed_index(limit=1)
        fear_greed = {
            "value":          int(fng["value"].iloc[0]),
            "classification": fng["classification"].iloc[0],
            "date":           str(fng["date"].iloc[0])
        }
        print(f"\nFear & Greed: {fear_greed['value']} — "
              f"{fear_greed['classification']}")
    except Exception as e:
        print(f"\nWARNING: Fear & Greed failed ({e})")

    # ── FRED Macro ────────────────────────────────────────────
    macro_context = None
    try:
        print("\nFetching FRED macro context...")
        macro_context = fetch_all_macro()
        print(f"  Gold signal: {macro_context.get('gold_macro_signal')}")
        print(f"  Yield curve: {macro_context.get('yield_curve_status')} "
              f"({macro_context.get('yield_curve_spread')})")
    except Exception as e:
        print(f"\nWARNING: FRED macro failed ({e})")

    # ── Polymarket ────────────────────────────────────────────
    polymarket_data = None
    try:
        print("\nFetching Polymarket prediction markets...")
        polymarket_data = fetch_polymarket_snapshot(
            asset_searches=ASSET_QUERIES,
            top_limit=100
        )
        total    = polymarket_data.get("total_markets_fetched", 0)
        filtered = polymarket_data.get("total_after_filter",    0)
        print(f"  {total} raw → {filtered} financial markets")
        for asset_key, sig in polymarket_data.get("signals", {}).items():
            if sig["signal"] != "UNKNOWN":
                prob = sig.get("avg_prob_yes", 0) or 0
                print(f"  {asset_key:<14} {sig['signal']:<22} "
                      f"(avg YES: {prob:.0%})")
        print("  (strike-relative crowd scores are computed per asset in the "
              "snapshot pass, where spot and vol are known)")
    except Exception as e:
        print(f"\nWARNING: Polymarket failed ({e})")

    # ── Build snapshots ────────────────────────────────────────
    all_snapshots = []
    for cfg in WATCHLIST:
        snap = build_snapshot(
            cfg,
            finbert=finbert,
            fear_greed=fear_greed,
            macro_context=macro_context,
            polymarket_data=polymarket_data
        )
        all_snapshots.append(snap)

        safe  = (snap["asset"].replace("=","_").replace("/","_")
                              .replace("-","_").replace(".","_"))
        fname = f"snapshot_{safe}.json"
        with open(fname, "w") as f:
            json.dump(snap, f, indent=2, default=str)
        print(f"\n  Saved: {fname}")
        time.sleep(2)

    # ── Kronos forecast layer (auto) ──────────────────────────
    # Runs after snapshots are on disk, then the injected forecast block is
    # pulled back in so the signal engine can weight it this same run.
    run_kronos_layer()
    _reload_kronos_block(all_snapshots)

    # ── Summary file ──────────────────────────────────────────
    summary = []
    signals = []
    for s in all_snapshots:
        sc = {k: v for k, v in s.items() if k != "price_data"}
        sc["candle_count"]   = len(s.get("price_data", []))
        sc["headline_count"] = len(s.get("news_headlines", []))
        sc["news_headlines"] = s.get("news_headlines", [])[:5]
        # Compute actionable entry/exit signal
        try:
            sig = evaluate_snapshot(s)
            sc["signal"] = sig
            signals.append(sig)
        except Exception as e:
            sc["signal"] = {
                "asset": s.get("asset"),
                "label": s.get("label", s.get("asset")),
                "action": "ERROR",
                "composite_score": None,
                "current_price": s.get("current_price"),
                "levels": {},
                "position_size_frac": None,
                "error": str(e),
            }
            signals.append(sc["signal"])
        summary.append(sc)

    with open("snapshot_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)

    # ── Signals file (actionable trades only) ─────────────────
    with open("signals.json", "w") as f:
        json.dump(signals, f, indent=2, default=str)

    # ── Final table ───────────────────────────────────────────
    print("\n" + "="*70)
    print("  DATA LAYER COMPLETE")
    print("="*70)
    print(f"{'Asset':<12} {'Label':<14} {'Price':>12} "
          f"{'Candles':>8} {'News':>6} {'Sentiment':<10} {'Polymarket':<20} Status")
    print("-"*90)
    for s in summary:
        poly = s.get("polymarket_signals") or {}
        poly_sig = poly.get("signal", "N/A")
        err_str  = f"{len(s.get('errors',[]))} err" if s.get("errors") else "OK"
        price    = s.get("current_price")
        price_str = f"{price:.2f}" if price else "N/A"
        print(
            f"{s['asset']:<12} "
            f"{s.get('label',''):<14} "
            f"{price_str:>12} "
            f"{s.get('candle_count',0):>8} "
            f"{s.get('headline_count',0):>6} "
            f"{s.get('overall_sentiment','?'):<10} "
            f"{poly_sig:<20} "
            f"{err_str}"
        )
    print("="*70)

    # ── Actionable signals ─────────────────────────────────
    print("\n" + "="*70)
    print("  ACTIONABLE SIGNALS")
    print("="*70)
    active = [sg for sg in signals
              if sg.get("action") not in ("HOLD", "NO_DATA", "ERROR")]
    if not active:
        print("  No strong setups right now — all assets HOLD/NEUTRAL.")
    for sg in active:
        lv = sg.get("levels") or {}
        size = sg.get("position_size_frac")
        size_str = f"{size*100:.1f}% acct" if size else "n/a"
        print(f"  {sg.get('label', sg.get('asset', '?')):<14} {sg['action']:<14} "
              f"score {sg.get('composite_score'):+.1f} @ {sg.get('current_price'):,.2f}")
        if lv.get("bias") in ("LONG", "SHORT"):
            print(f"      {lv['bias']} @ {lv['entry']:,.4f} | "
                  f"SL {lv['stop_loss']:,.4f} | "
                  f"TP {lv['take_profit_1']:,.4f} / {lv['take_profit_2']:,.4f} | "
                  f"size {size_str}")
    skipped = [sg for sg in signals
               if sg.get("action") in ("NO_DATA", "ERROR")]
    for sg in skipped:
        errs = sg.get("error") or "; ".join(sg.get("errors") or []) or "unknown"
        print(f"  {sg.get('label', sg.get('asset', '?')):<14} "
              f"{sg.get('action'):<8} — {errs}")
    print("="*70)
    print("\nSnapshots saved. Signals in signals.json")
    print("Kronos Forecast Layer: included above (forecasts.json).")
    print("NEXT: Feed forecasts into the Reasoning Layer.")

    return all_snapshots


if __name__ == "__main__":
    run_full_data_layer(use_finbert=True)


def run_with_brief():
    """Run full data layer then auto-generate human-readable brief."""
    run_full_data_layer(use_finbert=True)
    try:
        from generate_brief import generate_brief
        print("\nGenerating intelligence brief...")
        generate_brief()
    except Exception as e:
        print(f"WARNING: Brief generation failed ({e})")
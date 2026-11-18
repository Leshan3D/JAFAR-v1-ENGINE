"""
MACRO DATA MODULE
Source: FRED API (Federal Reserve Bank of St. Louis)
Free, official US government economic data.
Get your free key at: fred.stlouisfed.org/docs/api/api_key.html

Tracks macro indicators that move GOLD and broader markets:
  US 10Y Treasury Yield   (DGS10)    — primary gold driver
  US 2Y Treasury Yield    (DGS2)     — yield curve signal
  10Y Real Yield          (DFII10)   — gold strongest inverse
  TIPS Breakeven Rate     (T10YIE)   — inflation expectations
  CPI Inflation           (CPIAUCSL) — inflation hedge signal
  Fed Funds Rate          (FEDFUNDS) — monetary policy stance
  USD Broad Index / DXY   (DTWEXBGS) — dollar strength
"""

import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

# Read from env only — never hardcode secrets in source.
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
FRED_BASE    = "https://api.stlouisfed.org/fred/series/observations"

FRED_SERIES = {
    "DGS10":    {"label": "US 10Y Yield",         "unit": "%",  "category": "rates"},
    "DGS2":     {"label": "US 2Y Yield",           "unit": "%",  "category": "rates"},
    "DFII10":   {"label": "10Y Real Yield",         "unit": "%",  "category": "rates"},
    "T10YIE":   {"label": "10Y Breakeven Infl.",    "unit": "%",  "category": "inflation"},
    "CPIAUCSL": {"label": "CPI Index",              "unit": "idx","category": "inflation"},
    "FEDFUNDS": {"label": "Fed Funds Rate",          "unit": "%",  "category": "rates"},
    "DTWEXBGS": {"label": "USD Broad Index",        "unit": "idx","category": "dollar"},
}

# Dollar regime is judged against the broad index's OWN 1-year baseline, not a
# fixed level. DTWEXBGS (~120) is on a completely different scale from DXY
# (~100), so a DXY-calibrated threshold (e.g. 105) labels it "strong" forever.
# Deviation-from-trend is scale-free and self-adjusting as the index drifts.
DOLLAR_TREND_DAYS = 400     # ~1 trading year of DTWEXBGS
DOLLAR_STRONG_PCT = 2.0     # > +2% vs 1y average => strong dollar
DOLLAR_WEAK_PCT   = -2.0    # < -2% vs 1y average => weak dollar

# ── CPI / inflation regime ────────────────────────────────────
# FRED carries the CPI index levels (CPIAUCSL/CPILFESL) but lags the BLS
# release by a day or more, so the engine would miss the very print that just
# moved markets. BLS is the primary source (same-day); FRED is the fallback.
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_CPI_SERIES = {
    "headline_nsa": "CUUR0000SA0",      # headline YoY is quoted off NSA
    "core_nsa":     "CUUR0000SA0L1E",
    "headline_sa":  "CUSR0000SA0",      # the MoM the market trades is SA
    "core_sa":      "CUSR0000SA0L1E",
}
# BLS v2 unregistered allows ~25 queries/day and the print changes monthly, so
# cache aggressively — the service calls this hourly.
CPI_CACHE_TTL_S = int(os.environ.get("CPI_CACHE_TTL_S", 6 * 3600))
_cpi_cache = {"ts": 0.0, "data": None}


def fetch_fred_series(series_id, lookback_days=60, limit=10):
    """
    Fetch recent observations for a single FRED series.
    Returns DataFrame with columns: date, value, series_id
    """
    if not FRED_API_KEY:
        print(f"    [FRED] FRED_API_KEY not set — skipping {series_id}")
        return pd.DataFrame()

    start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end   = datetime.now().strftime("%Y-%m-%d")

    params = {
        "series_id":         series_id,
        "api_key":           FRED_API_KEY,
        "file_type":         "json",
        "observation_start": start,
        "observation_end":   end,
        "sort_order":        "desc",
        "limit":             limit,
    }

    try:
        r = requests.get(FRED_BASE, params=params, timeout=15)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        if not obs:
            return pd.DataFrame()
        df = pd.DataFrame(obs)[["date", "value"]]
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value"])
        df["series_id"] = series_id
        df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception as e:
        print(f"    [FRED {series_id}] {type(e).__name__}: {e}")
        return pd.DataFrame()


def _pct_change(cur, prev):
    if cur is None or not prev:
        return None
    return round((cur / prev - 1) * 100, 2)


def _prev_period(year, period):
    m = int(period[1:])
    if m == 1:
        return (str(int(year) - 1), "M12")
    return (str(year), f"M{m - 1:02d}")


def _bls_cpi_series(lookback_years=2):
    end = datetime.now().year
    try:
        r = requests.post(
            BLS_URL,
            json={"seriesid": list(BLS_CPI_SERIES.values()),
                  "startyear": str(end - lookback_years), "endyear": str(end)},
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        print(f"    [BLS CPI] {type(e).__name__}: {e}")
        return None
    if payload.get("status") != "REQUEST_SUCCEEDED":
        print(f"    [BLS CPI] {payload.get('status')}: {payload.get('message')}")
        return None
    return {s["seriesID"]: s.get("data", [])
            for s in payload.get("Results", {}).get("series", [])}


def _bls_points(series, sid):
    pts = {}
    for o in series.get(sid, []):
        try:
            pts[(str(o["year"]), o["period"])] = float(o["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return pts


def _cpi_from_bls():
    series = _bls_cpi_series()
    if not series:
        return None
    pts = {k: _bls_points(series, sid) for k, sid in BLS_CPI_SERIES.items()}
    if not all(pts.values()):
        return None
    lk = max(pts["headline_nsa"])            # latest headline month
    if lk not in pts["headline_sa"]:         # SA legs share the month
        return None
    prev = _prev_period(*lk)
    yago = (str(int(lk[0]) - 1), lk[1])

    def mom(sa):
        return _pct_change(sa.get(lk), sa.get(prev))

    def yoy(nsa):
        return _pct_change(nsa.get(lk), nsa.get(yago))

    return {
        "as_of":            f"{lk[0]}-{lk[1][1:]}",
        "source":           "BLS",
        "headline_mom":     mom(pts["headline_sa"]),
        "headline_yoy":     yoy(pts["headline_nsa"]),
        "core_mom":         mom(pts["core_sa"]),
        "core_yoy":         yoy(pts["core_nsa"]),
        "headline_index_sa": pts["headline_sa"].get(lk),
        "core_index_sa":    pts["core_sa"].get(lk),
    }


def _cpi_from_fred():
    h = fetch_fred_series("CPIAUCSL", lookback_days=800, limit=30)
    c = fetch_fred_series("CPILFESL", lookback_days=800, limit=30)
    if h.empty or c.empty:
        return None
    h = h.sort_values("date").reset_index(drop=True)
    c = c.sort_values("date").reset_index(drop=True)
    if len(h) < 13 or len(c) < 13:
        return None
    return {
        "as_of":            h["date"].iloc[-1].strftime("%Y-%m"),
        "source":           "FRED (lagged)",
        "headline_mom":     _pct_change(h["value"].iloc[-1], h["value"].iloc[-2]),
        "headline_yoy":     _pct_change(h["value"].iloc[-1], h["value"].iloc[-13]),
        "core_mom":         _pct_change(c["value"].iloc[-1], c["value"].iloc[-2]),
        "core_yoy":         _pct_change(c["value"].iloc[-1], c["value"].iloc[-13]),
        "headline_index_sa": float(h["value"].iloc[-1]),
        "core_index_sa":    float(c["value"].iloc[-1]),
    }


def classify_inflation(cpi):
    """HOT / COOL / NEUTRAL from annualised MoM. The Fed's 2% target is the
    yardstick: sustained core run-rate well above it is a rates-up regime."""
    if not cpi:
        return None

    def ann(mom):
        return None if mom is None else (((1 + mom / 100) ** 12) - 1) * 100

    h_ann, c_ann = ann(cpi.get("headline_mom")), ann(cpi.get("core_mom"))
    if h_ann is None and c_ann is None:
        return None
    if (c_ann is not None and c_ann >= 3.5) or (h_ann is not None and h_ann >= 4.0):
        return "HOT"
    if (c_ann is None or c_ann <= 1.5) and (h_ann is None or h_ann <= 2.0):
        return "COOL"
    return "NEUTRAL"


def _cpi_num(x, nd=2):
    """Format a CPI stat, or 'n/a' when BLS cannot publish it (e.g. missing YoY)."""
    return f"{x:.{nd}f}%" if isinstance(x, (int, float)) else "n/a"


def _cpi_brief(cpi):
    """Compact 'core X% MoM / Y% YoY' fragment for reasoning strings."""
    if not cpi:
        return "n/a"

    def f(x):
        return f"{x:.2f}%" if isinstance(x, (int, float)) else "?"

    return (f"core {f(cpi.get('core_mom'))} MoM / {f(cpi.get('core_yoy'))} YoY, "
            f"headline {f(cpi.get('headline_yoy'))} YoY")


def fetch_cpi():
    """Latest CPI print (headline + core MoM/YoY). BLS primary, FRED fallback.
    Cached for CPI_CACHE_TTL_S to stay well inside BLS's unregistered quota."""
    now = time.time()
    if _cpi_cache["data"] and now - _cpi_cache["ts"] < CPI_CACHE_TTL_S:
        return _cpi_cache["data"]
    data = _cpi_from_bls() or _cpi_from_fred()
    if data:
        data["inflation_signal"] = classify_inflation(data)
        _cpi_cache.update(ts=now, data=data)
    return data


def fetch_all_macro():
    """
    Fetch all macro indicators and derive gold signal.

    Returns dict:
    {
      "DGS10": {"label":..., "value":4.42, "unit":"%", "as_of":"2026-07-07"},
      ...
      "yield_curve_spread":  -0.45,
      "yield_curve_status":  "INVERTED" | "NORMAL",
      "gold_macro_signal":   "BULLISH_TAILWIND" | "NEUTRAL" | "BEARISH_HEADWIND",
      "gold_macro_reasoning": "...",
      "oil_macro_signal":    "BULLISH_TAILWIND" | "NEUTRAL" | "BEARISH_HEADWIND",
      "oil_macro_reasoning":  "...",
    }
    """
    print("  Fetching FRED macro indicators...")
    result = {}

    for sid, meta in FRED_SERIES.items():
        print(f"    [{sid}] {meta['label']}...", end=" ")
        df = fetch_fred_series(sid)
        if not df.empty:
            val  = float(df["value"].iloc[0])
            date = str(df["date"].iloc[0].date())
            result[sid] = {**meta, "value": val, "as_of": date}
            print(f"{val:.3f}{meta['unit']}")
        else:
            result[sid] = {**meta, "value": None, "as_of": None}
            print("N/A")

    # ── Derived: yield curve ──────────────────────────────────
    y2  = result.get("DGS2",  {}).get("value")
    y10 = result.get("DGS10", {}).get("value")
    if y2 and y10:
        spread = round(y2 - y10, 3)
        result["yield_curve_spread"] = spread
        result["yield_curve_status"] = "INVERTED" if spread > 0 else "NORMAL"
    else:
        result["yield_curve_spread"] = None
        result["yield_curve_status"] = "UNKNOWN"

    # ── Derived: gold macro signal ────────────────────────────
    real_yield = result.get("DFII10",   {}).get("value")
    dxy        = result.get("DTWEXBGS", {}).get("value")
    breakeven  = result.get("T10YIE",   {}).get("value")
    yc_status  = result.get("yield_curve_status", "UNKNOWN")

    # ── Derived: dollar regime (scale-free) ───────────────────
    dxy_hist = fetch_fred_series("DTWEXBGS", lookback_days=DOLLAR_TREND_DAYS,
                                 limit=DOLLAR_TREND_DAYS)
    dxy_baseline = float(dxy_hist["value"].mean()) if not dxy_hist.empty else None
    dxy_dev = ((dxy - dxy_baseline) / dxy_baseline * 100) \
        if (dxy is not None and dxy_baseline) else None
    result["dollar_baseline_1y"] = round(dxy_baseline, 3) if dxy_baseline else None
    result["dollar_dev_pct"]     = round(dxy_dev, 2) if dxy_dev is not None else None
    if dxy_dev is None:
        dollar_regime = None
    elif dxy_dev > DOLLAR_STRONG_PCT:
        dollar_regime = "strong"
    elif dxy_dev < DOLLAR_WEAK_PCT:
        dollar_regime = "weak"
    else:
        dollar_regime = "neutral"

    # ── Derived: CPI / inflation regime ───────────────────────
    cpi = fetch_cpi()
    result["cpi"] = cpi
    inflation_signal = (cpi or {}).get("inflation_signal")
    result["inflation_signal"] = inflation_signal
    if cpi:
        print(f"  CPI [{cpi.get('source')}] {cpi.get('as_of')}: "
              f"headline {_cpi_num(cpi.get('headline_mom'))} MoM / {_cpi_num(cpi.get('headline_yoy'))} YoY, "
              f"core {_cpi_num(cpi.get('core_mom'))} MoM / {_cpi_num(cpi.get('core_yoy'))} YoY "
              f"→ {inflation_signal}")

    tailwinds, headwinds = [], []

    if real_yield is not None:
        if real_yield > 1.5:
            headwinds.append(f"High real yield {real_yield:.2f}% — opportunity cost elevated")
        elif real_yield < 0:
            tailwinds.append(f"Negative real yield {real_yield:.2f}% — gold outperforms bonds")
        else:
            headwinds.append(f"Moderate real yield {real_yield:.2f}% — mild headwind")

    if dollar_regime == "strong":
        headwinds.append(f"Strong dollar {dxy:.1f} ({dxy_dev:+.1f}% vs 1y avg) "
                         "— gold expensive for foreign buyers")
    elif dollar_regime == "weak":
        tailwinds.append(f"Weak dollar {dxy:.1f} ({dxy_dev:+.1f}% vs 1y avg) "
                         "— tailwind for global gold buyers")

    if breakeven is not None:
        if breakeven > 2.5:
            tailwinds.append(f"High inflation expectations {breakeven:.2f}% — hedge demand elevated")
        elif breakeven < 2.0:
            headwinds.append(f"Low inflation expectations {breakeven:.2f}% — reduced hedge demand")

    # CPI print — the market trades the rates reaction, which dominates gold's
    # hedge bid on the day. Hot print => higher-for-longer => headwind.
    if inflation_signal == "HOT":
        headwinds.append(f"Hot CPI ({_cpi_brief(cpi)}) — higher-for-longer rates "
                         "weigh on bullion")
    elif inflation_signal == "COOL":
        tailwinds.append(f"Cool CPI ({_cpi_brief(cpi)}) — easing rate pressure "
                         "supports gold")

    if yc_status == "INVERTED":
        tailwinds.append("Inverted yield curve — recession fear supports safe haven demand")

    if len(tailwinds) > len(headwinds):
        signal = "BULLISH_TAILWIND"
    elif len(headwinds) > len(tailwinds):
        signal = "BEARISH_HEADWIND"
    else:
        signal = "NEUTRAL"

    parts = []
    if tailwinds: parts.append("Tailwinds: " + "; ".join(tailwinds))
    if headwinds: parts.append("Headwinds: " + "; ".join(headwinds))

    result["gold_macro_signal"]    = signal
    result["gold_macro_reasoning"] = " | ".join(parts) if parts else (
        "No decisive macro driver (real yields, dollar, inflation, curve neutral)")

    # ── Derived: oil macro signal ─────────────────────────────
    # Oil is a pro-growth, USD-priced commodity: a strong dollar and recession
    # fear are headwinds, while a weak dollar and firm inflation expectations
    # are tailwinds. Deliberately separate from gold_macro_signal — gold scores
    # on real yields/safe-haven demand, which oil does not track.
    oil_tailwinds, oil_headwinds = [], []

    if dollar_regime == "strong":
        oil_headwinds.append(f"Strong dollar {dxy:.1f} ({dxy_dev:+.1f}% vs 1y avg) "
                             "— headwind for USD-priced crude")
    elif dollar_regime == "weak":
        oil_tailwinds.append(f"Weak dollar {dxy:.1f} ({dxy_dev:+.1f}% vs 1y avg) "
                             "— tailwind for crude demand")

    if breakeven is not None:
        if breakeven > 2.5:
            oil_tailwinds.append(f"High inflation expectations {breakeven:.2f}% — firm demand backdrop")
        elif breakeven < 2.0:
            oil_headwinds.append(f"Low inflation expectations {breakeven:.2f}% — soft demand backdrop")

    if yc_status == "INVERTED":
        oil_headwinds.append("Inverted yield curve — recession risk weighs on oil demand")

    if len(oil_tailwinds) > len(oil_headwinds):
        oil_signal = "BULLISH_TAILWIND"
    elif len(oil_headwinds) > len(oil_tailwinds):
        oil_signal = "BEARISH_HEADWIND"
    else:
        oil_signal = "NEUTRAL"

    oil_parts = []
    if oil_tailwinds: oil_parts.append("Tailwinds: " + "; ".join(oil_tailwinds))
    if oil_headwinds: oil_parts.append("Headwinds: " + "; ".join(oil_headwinds))

    result["oil_macro_signal"]    = oil_signal
    result["oil_macro_reasoning"] = " | ".join(oil_parts) if oil_parts else (
        "No decisive macro driver (dollar, inflation, curve neutral)")

    # ── Derived: equity macro signal ──────────────────────────
    # Equities are a duration asset: hot CPI => higher-for-longer rates =>
    # multiple compression, a strong dollar trims multinational earnings, and
    # an inverted curve flags recession risk. Deliberately its own signal so
    # the stock entries in the watchlist can inherit a rates-aware tilt instead
    # of borrowing gold's real-yield read.
    eq_tailwinds, eq_headwinds = [], []

    if inflation_signal == "HOT":
        eq_headwinds.append(f"Hot CPI ({_cpi_brief(cpi)}) — higher-for-longer "
                            "rates compress equity multiples")
    elif inflation_signal == "COOL":
        eq_tailwinds.append(f"Cool CPI ({_cpi_brief(cpi)}) — easing rate "
                            "pressure supports equities")

    if dollar_regime == "strong":
        eq_headwinds.append("Strong dollar — headwind for multinational earnings")
    elif dollar_regime == "weak":
        eq_tailwinds.append("Weak dollar — tailwind for multinational earnings")

    if yc_status == "INVERTED":
        eq_headwinds.append("Inverted yield curve — recession risk")

    if len(eq_tailwinds) > len(eq_headwinds):
        equity_signal = "BULLISH_TAILWIND"
    elif len(eq_headwinds) > len(eq_tailwinds):
        equity_signal = "BEARISH_HEADWIND"
    else:
        equity_signal = "NEUTRAL"

    eq_parts = []
    if eq_tailwinds: eq_parts.append("Tailwinds: " + "; ".join(eq_tailwinds))
    if eq_headwinds: eq_parts.append("Headwinds: " + "; ".join(eq_headwinds))

    result["equity_macro_signal"]    = equity_signal
    result["equity_macro_reasoning"] = " | ".join(eq_parts) if eq_parts else (
        "No decisive equity macro driver (rates, dollar, curve neutral)")

    return result


if __name__ == "__main__":
    print("=" * 60)
    print("MACRO DATA MODULE — FRED API TEST")
    print("=" * 60 + "\n")

    macro = fetch_all_macro()

    print("\n--- MACRO SUMMARY ---")
    for sid, meta in FRED_SERIES.items():
        entry = macro.get(sid, {})
        val   = entry.get("value")
        as_of = entry.get("as_of", "N/A")
        if val is not None:
            print(f"  {meta['label']:<28} {val:>8.3f}{meta['unit']}   as of {as_of}")
        else:
            print(f"  {meta['label']:<28} {'N/A':>8}")

    print(f"\n  Yield Curve Spread : {macro.get('yield_curve_spread')} ({macro.get('yield_curve_status')})")
    print(f"  Dollar (DTWEXBGS)  : {macro.get('DTWEXBGS', {}).get('value')} "
          f"| 1y avg {macro.get('dollar_baseline_1y')} "
          f"| dev {macro.get('dollar_dev_pct')}%")
    cpi = macro.get("cpi") or {}
    print(f"\n  CPI ({cpi.get('source')}, {cpi.get('as_of')}) : "
          f"headline {_cpi_num(cpi.get('headline_mom'))} MoM / {_cpi_num(cpi.get('headline_yoy'))} YoY"
          f" | core {_cpi_num(cpi.get('core_mom'))} MoM / {_cpi_num(cpi.get('core_yoy'))} YoY"
          f" → {macro.get('inflation_signal')}")
    print(f"  Gold Macro Signal  : {macro.get('gold_macro_signal')}")
    print(f"  Reasoning          : {macro.get('gold_macro_reasoning')}")
    print(f"  Oil Macro Signal   : {macro.get('oil_macro_signal')}")
    print(f"  Reasoning          : {macro.get('oil_macro_reasoning')}")
    print(f"  Equity Macro Signal: {macro.get('equity_macro_signal')}")
    print(f"  Reasoning          : {macro.get('equity_macro_reasoning')}")
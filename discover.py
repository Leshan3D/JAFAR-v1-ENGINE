#!/usr/bin/env python3
"""
CRYPTO MOVERS SCANNER — JAFAR Intelligence

Pulls the 24h top gainers / top losers from MEXC USDT-M perpetuals and writes
them to a SEPARATE file, discovery.json. Candidates deliberately stay out of
WATCHLIST, snapshot_*.json and signals.json — nothing is analyzed until you
pick one (Telegram /discover, or `python3 candidate.py <SYMBOL>`).

MEXC is the execution venue, so every row here is already tradable.

Ported from the old Windows/n8n top_gainers_losers.py: the CoinGecko source is
kept only as an optional name/market-cap enrichment, and the n8n webhook is
gone — results stay local.

Env:
  DISCOVER_TOP_N   rows per side                    (default 10)
  DISCOVER_ENRICH  "0" disables the CoinGecko names (default 1)
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or HERE
DISCOVERY_FILE = os.path.join(DATA_DIR, "discovery.json")

MEXC_TICKERS = "https://contract.mexc.com/api/v1/contract/ticker"
MEXC_DETAIL = "https://contract.mexc.com/api/v1/contract/detail"
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"

TOP_N = int(os.environ.get("DISCOVER_TOP_N", "10"))
ENRICH = os.environ.get("DISCOVER_ENRICH", "1").lower() not in ("0", "false", "no")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_json(url, params=None, retries=3, timeout=25):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def _base_symbol(symbol):
    return symbol[:-5] if symbol.endswith("_USDT") else symbol.split("_")[0]


# ── MEXC-native movers (primary) ────────────────────────────

def fetch_mexc_movers(top_n=TOP_N):
    """Top gainers + losers across MEXC USDT-M perpetuals by 24h change.

    No liquidity or category filters — raw top_n each side, as requested.
    """
    tickers = _get_json(MEXC_TICKERS).get("data") or []
    try:
        details = _get_json(MEXC_DETAIL).get("data") or []
    except RuntimeError:
        details = []
    meta = {d.get("symbol"): d for d in details}

    rows = []
    for t in tickers:
        sym = t.get("symbol") or ""
        if not sym.endswith("_USDT"):
            continue
        change = _num(t.get("riseFallRate"))
        if change is None:
            continue
        m = meta.get(sym, {})
        rows.append({
            "symbol":         sym,
            "base":           _base_symbol(sym),
            "name":           None,
            "last":           _num(t.get("lastPrice")),
            "index_price":    _num(t.get("indexPrice")),
            "fair_price":     _num(t.get("fairPrice")),
            "change_24h_pct": round(change * 100, 2),
            "high_24h":       _num(t.get("high24Price")),
            "low_24h":        _num(t.get("lower24Price")),
            "amount_24h":     _num(t.get("amount24")),   # quote turnover (USDT)
            "volume_24h":     _num(t.get("volume24")),   # base volume
            "open_interest":  _num(t.get("holdVol")),
            "funding_rate":   _num(t.get("fundingRate")),
            "contract_size":  _num(m.get("contractSize")),
            "max_leverage":   m.get("maxLeverage"),
            "mexc_symbol":    sym,
        })

    gainers = sorted(rows, key=lambda x: x["change_24h_pct"], reverse=True)[:top_n]
    losers = sorted(rows, key=lambda x: x["change_24h_pct"])[:top_n]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source":       "mexc_usdt_m",
        "universe":     len(rows),
        "top_n":        top_n,
        "gainers":      gainers,
        "losers":       losers,
    }


# ── optional CoinGecko name enrichment ──────────────────────

def enrich_names(payload, retries=2):
    """Best-effort: map base symbol -> friendly name + market-cap rank.

    MEXC gives cryptic bases (DARK, MASKSOL); CoinGecko's free /coins/markets
    covers the top coins. Obscure memecoins simply keep name=None. Any failure
    is swallowed so the scanner never depends on CoinGecko.
    """
    try:
        markets = _get_json(COINGECKO_MARKETS, params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
        }, retries=retries)
    except RuntimeError:
        return payload
    by_symbol = {}
    for c in markets or []:
        sym = str(c.get("symbol") or "").upper()
        if sym and sym not in by_symbol:
            by_symbol[sym] = {
                "name": c.get("name"),
                "market_cap": c.get("market_cap"),
                "market_cap_rank": c.get("market_cap_rank"),
            }
    for side in ("gainers", "losers"):
        for row in payload.get(side, []):
            hit = by_symbol.get(str(row.get("base") or "").upper())
            if hit:
                row["name"] = hit.get("name")
                row["market_cap"] = hit.get("market_cap")
                row["market_cap_rank"] = hit.get("market_cap_rank")
    payload["enriched"] = True
    return payload


# ── persistence ─────────────────────────────────────────────

def _atomic_write(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, path)


def save_discovery(payload, path=DISCOVERY_FILE):
    _atomic_write(path, payload)
    return path


def load_discovery(path=DISCOVERY_FILE):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def discovery_age_seconds(payload):
    ts = (payload or {}).get("generated_at")
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).total_seconds()


def refresh(enrich=ENRICH, top_n=TOP_N, path=DISCOVERY_FILE):
    payload = fetch_mexc_movers(top_n=top_n)
    if enrich:
        payload = enrich_names(payload)
    save_discovery(payload, path=path)
    return payload


def get_movers(max_age_s=900, enrich=ENRICH, top_n=TOP_N, path=DISCOVERY_FILE):
    payload = load_discovery(path=path)
    age = discovery_age_seconds(payload)
    if payload and age is not None and age <= max_age_s:
        return payload
    return refresh(enrich=enrich, top_n=top_n, path=path)


# ── CLI ─────────────────────────────────────────────────────

def _fmt_pct(x):
    return f"{x:+.2f}%" if isinstance(x, (int, float)) else "  n/a "


def _fmt_money(x):
    if not isinstance(x, (int, float)):
        return "n/a"
    if x >= 1_000_000_000:
        return f"${x/1e9:,.2f}B"
    if x >= 1_000_000:
        return f"${x/1e6:,.2f}M"
    if x >= 1_000:
        return f"${x/1e3:,.1f}K"
    return f"${x:,.2f}"


def format_table(payload):
    lines = []
    for side, title in (("gainers", "TOP GAINERS"), ("losers", "TOP LOSERS")):
        lines.append(f"\n{title}  (MEXC USDT-M, 24h)")
        lines.append(f"  {'symbol':<16}{'name':<18}{'chg':>9}{'last':>14}{'turnover':>12}")
        for r in payload.get(side, []):
            lines.append(
                f"  {r['symbol']:<16}{(r.get('name') or r['base']):<18}"
                f"{_fmt_pct(r['change_24h_pct']):>9}"
                f"{(r['last'] if r['last'] is not None else 0):>14,.6g}"
                f"{_fmt_money(r.get('amount_24h')):>12}")
    lines.append(f"\n  {payload.get('universe', 0)} USDT perps scanned · "
                 f"generated {payload.get('generated_at', '')[:19]}Z")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Top crypto gainers/losers from MEXC.")
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="ignore discovery.json and always refetch")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    args = ap.parse_args()

    if args.force:
        payload = refresh(enrich=not args.no_enrich, top_n=args.top_n)
    else:
        payload = get_movers(enrich=not args.no_enrich, top_n=args.top_n)

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(format_table(payload))
        print(f"\n  saved -> {DISCOVERY_FILE}")


if __name__ == "__main__":
    main()

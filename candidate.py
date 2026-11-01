#!/usr/bin/env python3
"""
CANDIDATE ANALYZER — JAFAR Intelligence

Runs ANY discovered symbol through the full engine on demand — price + news +
FinBERT sentiment + Fear&Greed + Kronos + signal scoring — WITHOUT adding it to
the watchlist.

Ephemeral by design: the snapshot and the Kronos subprocess run in a scratch
directory under candidate_runs/, never in the live DATA_DIR, so nothing leaks
into snapshot_*.json, snapshot_summary.json or signals.json. The live service
never sees candidates until you promote one yourself.

Usage:
    python3 candidate.py DARK_USDT
    python3 candidate.py DARK --json

Used by the Telegram /discover zoom-in.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or HERE
SCRATCH_ROOT = os.path.join(DATA_DIR, "candidate_runs")

if HERE not in sys.path:
    sys.path.insert(0, HERE)

import build_snapshot as bs          # noqa: E402
import signals                       # noqa: E402
from fetch_sentiment import FinBERTSentiment, fetch_fear_greed_index  # noqa: E402


def normalize_symbol(symbol):
    """'dark', 'DARK', 'DARKUSDT', 'DARK_USDT' -> ('DARK', 'DARK_USDT')."""
    s = str(symbol or "").strip().upper()
    if s.endswith("_USDT"):
        return s[:-5], s
    if s.endswith("USDT") and len(s) > 4:
        return s[:-4], s[:-4] + "_USDT"
    return s, s + "_USDT"


def _candidate_config(symbol):
    base, mexc_symbol = normalize_symbol(symbol)
    return {
        "asset":              mexc_symbol,
        "label":              base,
        "asset_type":         "crypto",
        "news_asset_type":    "crypto",
        "polymarket_key":     "macro",
        "geopolitical_query": None,
        "candle_interval":    "1h",
        "candle_limit":       400,
        "ticker":             None,
        "source":             "mexc",
        "mexc_symbol":        mexc_symbol,
    }


def _ensure_news_entity(asset_key, base):
    """Teach the news splitter this candidate's symbol so asset-specific
    headlines aren't silently collapsed into the market-wide bucket (which is
    what the None fallback in split_news_by_entity otherwise does). Keyed by
    the exact asset string build_snapshot looks up."""
    if base and len(base) >= 3 and base.isalnum() \
            and asset_key not in bs.NEWS_ENTITY_PATTERNS:
        bs.NEWS_ENTITY_PATTERNS[asset_key] = [
            re.compile(rf"\b{re.escape(base)}\b", re.I)]


def _run_kronos(scratch_dir, timeout_s=1800):
    script = os.path.join(HERE, "kronos_layer.py")
    py = os.environ.get("KRONOS_PYTHON") or os.path.expanduser(
        "~/.venvs/trading-kronos/bin/python")
    if not (os.path.exists(script) and os.path.exists(py)):
        return False
    env = dict(os.environ)
    env["DATA_DIR"] = scratch_dir
    try:
        p = subprocess.run([py, script], cwd=scratch_dir, env=env,
                           timeout=timeout_s)
        return p.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def analyze_candidate(symbol, with_macro=False, with_polymarket=False,
                      keep=False, verbose=True):
    """Run one symbol through the engine; return {'symbol','snapshot','signal'}."""
    base, mexc_symbol = normalize_symbol(symbol)
    cfg = _candidate_config(mexc_symbol)
    _ensure_news_entity(mexc_symbol, base)

    if verbose:
        print(f"\n{'='*60}\n  CANDIDATE: {base} ({mexc_symbol})\n{'='*60}")

    finbert = None
    try:
        finbert = FinBERTSentiment()
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"  FinBERT unavailable: {e}")

    fear_greed = None
    try:
        fng = fetch_fear_greed_index(limit=1)
        fear_greed = {"value": int(fng["value"].iloc[0]),
                      "classification": fng["classification"].iloc[0],
                      "date": str(fng["date"].iloc[0])}
    except Exception:  # noqa: BLE001
        pass

    macro_context = None
    if with_macro:
        try:
            from fetch_macro import fetch_all_macro
            macro_context = fetch_all_macro()
        except Exception:  # noqa: BLE001
            pass

    polymarket_data = None
    if with_polymarket:
        try:
            from fetch_polymarket import fetch_polymarket_snapshot, ASSET_QUERIES
            polymarket_data = fetch_polymarket_snapshot(asset_searches=ASSET_QUERIES,
                                                        top_limit=100)
        except Exception:  # noqa: BLE001
            pass

    snap = bs.build_snapshot(cfg, finbert=finbert, fear_greed=fear_greed,
                             macro_context=macro_context,
                             polymarket_data=polymarket_data)

    os.makedirs(SCRATCH_ROOT, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix=f"{base}_", dir=SCRATCH_ROOT)
    snap_path = os.path.join(scratch, f"snapshot_{mexc_symbol}.json")
    with open(snap_path, "w") as f:
        json.dump(snap, f, indent=2, default=str)

    _run_kronos(scratch)
    try:
        with open(snap_path) as f:
            kronos = json.load(f).get("kronos_forecast")
        if kronos:
            snap["kronos_forecast"] = kronos
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    sig = signals.evaluate_snapshot(snap)
    sig["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    sig["is_candidate"] = True

    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)

    return {"symbol": mexc_symbol, "base": base, "snapshot": snap, "signal": sig}


# ── CLI ─────────────────────────────────────────────────────

def _fmt(x, nd=6):
    if x is None:
        return "n/a"
    try:
        return f"{float(x):,.{nd}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(x)


def print_dossier(result):
    sig, snap = result["signal"], result["snapshot"]
    print(f"\n{'='*66}")
    print(f"  {sig.get('label')} ({sig.get('asset')})  ->  {sig.get('action')}"
          f"   composite {sig.get('composite_score')}")
    print(f"{'='*66}")
    print(f"  price {_fmt(sig.get('current_price'))}   "
          f"news_scope {snap.get('news_scope')}   "
          f"sentiment {snap.get('overall_sentiment')} "
          f"{snap.get('sentiment_counts')}")
    print("\n  FACTORS")
    for f_ in sig.get("factors", []):
        print(f"    {f_['factor']:<12} w={f_.get('weight')}  score={f_.get('score')}")
    lv = sig.get("levels") or {}
    if lv:
        print("\n  LEVELS")
        for k in ("entry", "stop_loss", "take_profit_1", "take_profit_2"):
            if lv.get(k) is not None:
                print(f"    {k:<16}{_fmt(lv[k])}")
    if sig.get("position_size_frac") is not None:
        print(f"\n  size_frac {sig['position_size_frac']}")
    gate = sig.get("kronos_gate")
    if gate:
        print(f"  kronos_gate {gate}")
    print("\n  RATIONALE\n" + str(sig.get("rationale", ""))[:1500])


def main():
    ap = argparse.ArgumentParser(description="Analyze one candidate through the engine.")
    ap.add_argument("symbol", help="e.g. DARK_USDT, DARK, darkusdt")
    ap.add_argument("--json", action="store_true", help="print raw result JSON")
    ap.add_argument("--keep", action="store_true", help="keep the scratch dir")
    ap.add_argument("--with-macro", action="store_true")
    ap.add_argument("--with-polymarket", action="store_true")
    args = ap.parse_args()

    result = analyze_candidate(args.symbol, with_macro=args.with_macro,
                               with_polymarket=args.with_polymarket,
                               keep=args.keep)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_dossier(result)


if __name__ == "__main__":
    main()

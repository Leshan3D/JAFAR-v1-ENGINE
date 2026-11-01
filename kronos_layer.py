"""
KRONOS FORECAST LAYER — JAFAR Intelligence

Layer 1.5: after build_snapshot.py writes snapshot_<ASSET>.json (whose
`price_data` is exactly Kronos' OHLCV input), this layer forecasts the next
N bars per asset and injects a `kronos_forecast` block back into each snapshot.
It also writes a combined forecasts.json.

Runs in its own venv (Kronos pins pandas==2.2.2 while the main pipeline uses
pandas 3.x). Invoke with that interpreter, e.g.:

    /home/jafar/.venvs/trading-kronos/bin/python kronos_layer.py

Env overrides:
    KRONOS_SRC        path to cloned Kronos repo (default ./kronos_src)
    KRONOS_MODEL      NeoQuasar/Kronos-small | -base | -mini   (default -small)
    KRONOS_TOKENIZER  NeoQuasar/Kronos-Tokenizer-base | -2k     (default -base)
    KRONOS_PRED_LEN   bars to forecast                         (default 24)
    KRONOS_LOOKBACK   context bars, <= max_context             (default 400)
    KRONOS_SAMPLES    parallel samples (averaged internally)   (default 1)
    KRONOS_OUTREP     extra independent runs for p(up)         (default 0)
    KRONOS_DEVICE     cpu | cuda                               (default cpu)
    DATA_DIR          dir holding snapshots                    (default script dir)
"""

import os
import sys
import json
import glob
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or HERE
KRONOS_SRC = os.environ.get("KRONOS_SRC") or os.path.join(HERE, "kronos_src")

sys.path.insert(0, KRONOS_SRC)
from model import Kronos, KronosTokenizer, KronosPredictor  # noqa: E402

MODEL_ID     = os.environ.get("KRONOS_MODEL", "NeoQuasar/Kronos-small")
TOKENIZER_ID = os.environ.get("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
PRED_LEN     = int(os.environ.get("KRONOS_PRED_LEN", 24))
LOOKBACK     = int(os.environ.get("KRONOS_LOOKBACK", 400))
SAMPLES      = int(os.environ.get("KRONOS_SAMPLES", 1))
OUTREP       = int(os.environ.get("KRONOS_OUTREP", 0))
DEVICE       = os.environ.get("KRONOS_DEVICE", "cpu")
MAX_CONTEXT  = 512
MIN_BARS     = int(os.environ.get("KRONOS_MIN_BARS", 48))

OHLCV = ["open", "high", "low", "close", "volume", "amount"]


def get_predictor():
    # Seed so a given snapshot yields the same forecast each run (sampling is
    # otherwise stochastic) — keeps signals.json reproducible.
    torch.manual_seed(int(os.environ.get("KRONOS_SEED", "42")))
    print(f"  Loading {MODEL_ID} + {TOKENIZER_ID} on {DEVICE}...")
    tok = KronosTokenizer.from_pretrained(TOKENIZER_ID)
    model = Kronos.from_pretrained(MODEL_ID)
    model = model.to(DEVICE)
    model.eval()
    return KronosPredictor(model, tok, device=DEVICE, max_context=MAX_CONTEXT)


def _mean_close_path(predictor, x_df, x_ts, y_ts, pred_len):
    pred_df = predictor.predict(
        df=x_df, x_timestamp=x_ts, y_timestamp=y_ts,
        pred_len=pred_len, T=1.0, top_k=0, top_p=0.9,
        sample_count=SAMPLES, verbose=False,
    )
    return np.asarray(pred_df["close"], dtype=float), pred_df


def forecast_asset(predictor, snap):
    rows = snap.get("price_data") or []
    if len(rows) < MIN_BARS:
        return None

    # Per-asset horizon (build_snapshot sets this) beats the global default.
    pred_len = int(snap.get("kronos_pred_len") or PRED_LEN)

    df = pd.DataFrame(rows)
    # Stocks carry mixed UTC offsets across DST boundaries; harmonize to naive UTC.
    df["timestamps"] = pd.to_datetime(df["timestamps"], utc=True).dt.tz_localize(None)
    cols = [c for c in OHLCV if c in df.columns]
    df = df.dropna(subset=["close"])

    lookback = min(LOOKBACK, MAX_CONTEXT, len(df))
    x_df = df[cols].tail(lookback).reset_index(drop=True)
    x_ts = df["timestamps"].tail(lookback).reset_index(drop=True)

    delta = x_ts.iloc[-1] - x_ts.iloc[-2]
    y_ts = pd.Series([x_ts.iloc[-1] + delta * (i + 1) for i in range(pred_len)])

    path, pred_df = _mean_close_path(predictor, x_df, x_ts, y_ts, pred_len)

    # optional independent runs -> empirical p(up)
    p_up = None
    if OUTREP > 0:
        ups = 1 if path[-1] > float(snap["current_price"]) else 0
        for i in range(OUTREP):
            torch.manual_seed(1000 + i)
            r, _ = _mean_close_path(predictor, x_df, x_ts, y_ts, pred_len)
            ups += 1 if r[-1] > float(snap["current_price"]) else 0
        p_up = round(ups / (OUTREP + 1), 3)

    cur = float(snap["current_price"])
    fc = float(path[-1])
    closes = x_df["close"].to_numpy(dtype=float)
    atr_pct = float(np.mean(np.abs(np.diff(closes)[-14:])) / cur * 100.0) or 1.0
    exp_ret = (fc / cur - 1.0) * 100.0
    # z-score the forecast move against expected horizon volatility
    # (per-bar ATR scaled by sqrt of the horizon) so a normal move doesn't pin
    # the score at the rails.
    horizon_vol = atr_pct * (pred_len ** 0.5)
    z = exp_ret / horizon_vol if horizon_vol else 0.0
    score = float(100.0 * np.tanh(z))

    return {
        "asset": snap["asset"],
        "label": snap.get("label", snap["asset"]),
        "model": MODEL_ID,
        "interval": snap.get("candle_interval"),
        "lookback": int(lookback),
        "pred_len": pred_len,
        "bar_ts": str(x_ts.iloc[-1]),
        "current_price": cur,
        "forecast_close": round(fc, 4),
        "expected_return_pct": round(exp_ret, 3),
        "direction": "UP" if exp_ret > 0 else "DOWN",
        "atr_pct": round(atr_pct, 3),
        "horizon_vol_pct": round(horizon_vol, 3),
        "z": round(z, 3),
        "score": round(score, 1),
        "prob_up": p_up,
        "forecast_path": [
            {"t": str(t), "close": round(float(c), 4)}
            for t, c in zip(pred_df.index, path)
        ],
        "as_of": snap.get("data_as_of"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ─────────────────────────────────────────────────────────────
# ACCURACY TRACKER (kronos_track.json)
# ─────────────────────────────────────────────────────────────
TRACK_FILE = "kronos_track.json"


def _load_track():
    path = os.path.join(DATA_DIR, TRACK_FILE)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _resolve_track(track):
    """Score resolved forecasts: for each unresolved record, look up the price
    `pred_len` bars after its bar_ts in the asset's current snapshot."""
    by_asset = {}
    for p in glob.glob(os.path.join(DATA_DIR, "snapshot_*.json")):
        if "summary" in os.path.basename(p):
            continue
        try:
            with open(p) as f:
                s = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        rows = s.get("price_data") or []
        if not rows:
            continue
        ts = pd.to_datetime([r["timestamps"] for r in rows], utc=True)\
               .tz_localize(None).strftime("%Y-%m-%d %H:%M:%S")
        by_asset[s.get("asset")] = (list(ts), [float(r["close"]) for r in rows])

    resolved = 0
    for rec in track:
        if rec.get("hit") is not None:
            continue
        data = by_asset.get(rec["asset"])
        if not data:
            continue
        ts, closes = data
        try:
            i = ts.index(rec["bar_ts"])
        except (ValueError, KeyError):
            continue
        j = i + int(rec["pred_len"])
        if j >= len(closes) or closes[i] <= 0:
            continue
        realized = (closes[j] / closes[i] - 1.0) * 100.0
        rec["realized_ret_pct"] = round(realized, 3)
        rec["hit"] = bool((rec["direction"] == "UP") == (realized > 0))
        rec["resolved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        resolved += 1
    return resolved


def _update_track(forecasts):
    track = _load_track()
    _resolve_track(track)
    seen = {(r["asset"], r["bar_ts"]) for r in track}
    for asset, fc in forecasts.items():
        key = (asset, fc.get("bar_ts"))
        if fc.get("bar_ts") and key not in seen:
            track.append({
                "asset": asset,
                "label": fc.get("label"),
                "bar_ts": fc["bar_ts"],
                "interval": fc.get("interval"),
                "pred_len": fc.get("pred_len"),
                "direction": fc.get("direction"),
                "pred_ret_pct": fc.get("expected_return_pct"),
                "current_price": fc.get("current_price"),
                "forecast_close": fc.get("forecast_close"),
                "generated_at": fc.get("generated_at"),
                "realized_ret_pct": None,
                "hit": None,
                "resolved_at": None,
            })
            seen.add(key)
    # Keep the log bounded.
    track = track[-5000:]
    with open(os.path.join(DATA_DIR, TRACK_FILE), "w") as f:
        json.dump(track, f, indent=2)
    return track


def _print_scoreboard(track):
    scored = [r for r in track if r.get("hit") is not None]
    if not scored:
        print("  [kronos] tracker: no resolved forecasts yet")
        return
    hits = sum(1 for r in scored if r["hit"])
    print(f"  [kronos] tracker: {hits}/{len(scored)} hit "
          f"({100*hits/len(scored):.0f}%) across all resolved forecasts")
    per = {}
    for r in scored:
        per.setdefault(r["asset"], [0, 0])
        per[r["asset"]][0] += 1 if r["hit"] else 0
        per[r["asset"]][1] += 1
    for asset, (h, n) in sorted(per.items()):
        print(f"            {asset:<10} {h}/{n} ({100*h/n:.0f}%)")


def main():
    t0 = time.time()
    print("=" * 70)
    print("  KRONOS FORECAST LAYER")
    print(f"  model={MODEL_ID}  pred_len={PRED_LEN}  lookback={LOOKBACK}  "
          f"samples={SAMPLES}  device={DEVICE}")
    print("=" * 70)

    predictor = get_predictor()
    load_s = time.time() - t0

    files = [p for p in sorted(glob.glob(os.path.join(DATA_DIR, "snapshot_*.json")))
             if "summary" not in os.path.basename(p)]
    if not files:
        print("  No snapshots found. Run build_snapshot.py first.")
        return

    forecasts = {}
    for path in files:
        base = os.path.basename(path)
        try:
            with open(path) as f:
                snap = json.load(f)
            t1 = time.time()
            fc = forecast_asset(predictor, snap)
            dt = time.time() - t1
            if not fc:
                print(f"  {base:<28} SKIP (too few bars)")
                continue
            snap["kronos_forecast"] = fc
            with open(path, "w") as f:
                json.dump(snap, f, default=str)
            forecasts[fc["asset"]] = fc
            print(f"  {fc['label']:<16} {fc['direction']:<4} "
                  f"{fc['expected_return_pct']:+7.2f}%  fc={fc['forecast_close']:>12,.2f}  "
                  f"score {fc['score']:+6.1f}  ({dt:5.1f}s)")
        except Exception as e:
            print(f"  {base:<28} FAILED: {e}")

    out = os.path.join(DATA_DIR, "forecasts.json")
    with open(out, "w") as f:
        json.dump(forecasts, f, indent=2)

    # Accuracy tracker: log these forecasts, resolve past ones against realised
    # price, and print the running hit-rate.
    track = _update_track(forecasts)
    _print_scoreboard(track)

    print("-" * 70)
    print(f"  {len(forecasts)} forecasts -> forecasts.json  "
          f"(model load {load_s:.1f}s, total {time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()

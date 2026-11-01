#!/usr/bin/env python3
"""
MEXC USDT-M futures executor — human-approved, semi-autonomous.

You decide the trade; nothing fires until you pass the literal word EXECUTE.

    # preview (always safe)
    python3 mexc_exec.py order --symbol BTC_USDT --side long --qty 0.349 \
        --entry 77870 --sl 77613 --tp 78387

    # place it
    python3 mexc_exec.py order --symbol BTC_USDT --side long --qty 0.349 \
        --entry 77870 --sl 77613 --tp 78387 --confirm EXECUTE

Safety rails (env, see .env.example):
    MEXC_API_KEY / MEXC_API_SECRET   private futures API (trading, NO withdraw)
    MEXC_DRY_RUN        default 1    must be 0 to touch the exchange
    MEXC_MAX_NOTIONAL   default 30000
    MEXC_MAX_POSITIONS  default 3
    MEXC_MAX_DRIFT_PCT  default 0.5  reject if live price moved from --entry
    MEXC_LEVERAGE       default 10
    MEXC_OPEN_TYPE      default 1    (1 isolated, 2 cross)
    KILL_SWITCH         file in DATA_DIR halts everything
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("DATA_DIR", HERE)
BASE = "https://contract.mexc.com"
LOG = os.path.join(HERE, "mexc_exec.log")

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass

SIDE_OPEN_LONG, SIDE_CLOSE_SHORT, SIDE_OPEN_SHORT, SIDE_CLOSE_LONG = 1, 2, 3, 4
TYPE_LIMIT, TYPE_MARKET = 1, 5


def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_float(name, default):
    try:
        return float(env(name, str(default)))
    except ValueError:
        return float(default)


def env_int(name, default):
    try:
        return int(float(env(name, str(default))))
    except ValueError:
        return int(default)


def keys():
    return env("MEXC_API_KEY"), env("MEXC_API_SECRET")


def dry_run():
    return env("MEXC_DRY_RUN", "1") not in ("0", "false", "no", "off")


def log(msg):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def redact(text):
    _, secret = keys()
    ak, _ = keys()
    out = str(text)
    for s in (secret, ak):
        if s:
            out = out.replace(s, "<redacted>")
    return out


# ── HTTP + signing ──────────────────────────────────────────

def _request(method, path, params=None, signed=False):
    import requests  # imported lazily so --help works without deps

    params = params or {}
    headers = {"User-Agent": "jafar-exec/1.0"}
    if method == "GET":
        qs = urllib.parse.urlencode(sorted(params.items())) if params else ""
        url = BASE + path + (f"?{qs}" if qs else "")
        body = None
    else:
        body = json.dumps(params, separators=(",", ":"))
        url = BASE + path
        headers["Content-Type"] = "application/json"

    if signed:
        api_key, secret = keys()
        if not api_key or not secret:
            raise RuntimeError("MEXC_API_KEY / MEXC_API_SECRET not set in .env")
        ts = str(int(time.time() * 1000))
        param_str = qs if method == "GET" else body
        sign = hmac.new(secret.encode(), (api_key + ts + param_str).encode(),
                        hashlib.sha256).hexdigest()
        headers.update({"ApiKey": api_key, "Request-Time": ts, "Signature": sign})

    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    else:
        r = requests.post(url, data=body, headers=headers, timeout=15)
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"non-JSON response {r.status_code}: {r.text[:200]}")


def public_ticker(symbol):
    d = _request("GET", "/api/v1/contract/ticker", {"symbol": symbol})
    if not d.get("success"):
        raise RuntimeError(f"ticker failed: {d}")
    return d["data"]


def balance():
    d = _request("GET", "/api/v1/private/account/assets", signed=True)
    if not d.get("success"):
        raise RuntimeError(f"assets failed: {redact(d)}")
    for row in d.get("data", []):
        if row.get("currency") == "USDT":
            return row
    return {}


def open_positions(symbol=None):
    p = {"symbol": symbol} if symbol else {}
    d = _request("GET", "/api/v1/private/position/open_positions", p, signed=True)
    if not d.get("success"):
        raise RuntimeError(f"positions failed: {redact(d)}")
    return d.get("data", [])


def position_mode():
    d = _request("GET", "/api/v1/private/position/position_mode", signed=True)
    return d.get("data")


# ── rails ──────────────────────────────────────────────────

def kill_switch_on():
    return os.path.exists(os.path.join(os.environ.get("DATA_DIR", HERE), "KILL_SWITCH"))


def check_rails(symbol, side, qty, entry, sl, leverage, live):
    problems = []
    if kill_switch_on():
        problems.append("KILL_SWITCH file present")
    if qty <= 0:
        problems.append("qty must be > 0")

    notional = qty * entry
    max_notional = env_float("MEXC_MAX_NOTIONAL", 30000)
    if notional > max_notional:
        problems.append(f"notional ${notional:,.0f} > MEXC_MAX_NOTIONAL ${max_notional:,.0f}")

    drift = abs(live - entry) / entry * 100 if entry else 0.0
    max_drift = env_float("MEXC_MAX_DRIFT_PCT", 0.5)
    if drift > max_drift:
        problems.append(f"live {live:,.1f} is {drift:.2f}% from entry {entry:,.1f} (> {max_drift}%)")

    if sl:
        if side == "long" and sl >= entry:
            problems.append("long SL must be below entry")
        if side == "short" and sl <= entry:
            problems.append("short SL must be above entry")
    return problems, notional


def existing_count(symbol):
    try:
        return len([p for p in open_positions(symbol) if p.get("state") == 1])
    except Exception:
        return None


# ── commands ───────────────────────────────────────────────

def cmd_status(_a):
    api_key, secret = keys()
    print("MEXC executor status")
    print(f"  dry-run       : {dry_run()}  (MEXC_DRY_RUN={env('MEXC_DRY_RUN', '1')})")
    print(f"  api key       : {'set (' + api_key[:4] + '…)' if api_key else 'NOT SET'}")
    print(f"  api secret    : {'set' if secret else 'NOT SET'}")
    print(f"  leverage      : {env_int('MEXC_LEVERAGE', 10)}x")
    print(f"  open type     : {env_int('MEXC_OPEN_TYPE', 1)} (1 isolated, 2 cross)")
    print(f"  max notional  : ${env_float('MEXC_MAX_NOTIONAL', 30000):,.0f}")
    print(f"  max positions : {env_int('MEXC_MAX_POSITIONS', 3)}")
    print(f"  max drift     : {env_float('MEXC_MAX_DRIFT_PCT', 0.5)}%")
    print(f"  kill switch   : {'ACTIVE' if kill_switch_on() else 'clear'}")
    if api_key and secret:
        try:
            bal = balance()
            print(f"  USDT equity   : ${float(bal.get('equity', 0)):,.2f}")
            print(f"  available     : ${float(bal.get('availableBalance', 0)):,.2f}")
            print(f"  position mode : {position_mode()} (1 hedge, 2 one-way)")
            for p in open_positions():
                print(f"  position      : {p.get('symbol')} type {p.get('positionType')} "
                      f"vol {p.get('holdVol')} @ {p.get('holdAvgPrice')}")
        except Exception as e:
            print(f"  exchange query failed: {redact(e)}")


def cmd_quote(a):
    t = public_ticker(a.symbol)
    print(f"{a.symbol}: last {t['lastPrice']}  bid {t.get('bid1')}  ask {t.get('ask1')} "
          f"24h {t.get('lower24Price')}–{t.get('high24Price')}")


def build_order(a, live):
    side = a.side.lower()
    if side not in ("long", "short"):
        raise SystemExit("--side must be long or short")
    qty = float(a.qty)
    entry = float(a.entry) if a.entry else live
    sl = float(a.sl) if a.sl else None
    tp = float(a.tp) if a.tp else None
    leverage = int(a.leverage or env_int("MEXC_LEVERAGE", 10))
    open_type = int(a.open_type or env_int("MEXC_OPEN_TYPE", 1))
    order_type = TYPE_MARKET if a.market else TYPE_LIMIT
    side_code = SIDE_OPEN_LONG if side == "long" else SIDE_OPEN_SHORT

    payload = {
        "symbol": a.symbol,
        "price": 0 if a.market else round(entry, 4),
        "vol": qty,
        "leverage": leverage,
        "side": side_code,
        "type": order_type,
        "openType": open_type,
    }
    if sl:
        payload["stopLossPrice"] = sl
    if tp:
        payload["takeProfitPrice"] = tp
    return payload, qty, entry, sl, tp, leverage


def cmd_order(a):
    t = public_ticker(a.symbol)
    live = float(t["lastPrice"])
    payload, qty, entry, sl, tp, leverage = build_order(a, live)
    problems, notional = check_rails(a.symbol, a.side.lower(), qty, entry, sl, leverage, live)

    n_open = existing_count(a.symbol)
    if n_open is not None and n_open >= env_int("MEXC_MAX_POSITIONS", 3):
        problems.append(f"already {n_open} open positions (max {env_int('MEXC_MAX_POSITIONS', 3)})")

    print(f"{a.symbol} live {live:,.1f} | entry {entry:,.1f} | qty {qty} | "
          f"notional ${notional:,.0f} | margin ${notional / leverage:,.0f} @ {leverage}x")
    if sl:
        print(f"  SL {sl:,.1f} -> risk ${qty * abs(entry - sl):,.2f} "
              f"({qty * abs(entry - sl) / max(notional, 1) * 100:.2f}% of notional)")
    if tp:
        print(f"  TP {tp:,.1f} -> reward ${qty * abs(tp - entry):,.2f}")
    print(f"  payload: {json.dumps(payload)}")

    if problems:
        print("\nRAILS BLOCKED:")
        for p in problems:
            print(f"  - {p}")
        return 2

    confirmed = (a.confirm or "").strip() == "EXECUTE"
    if not confirmed or dry_run():
        why = "dry-run" if dry_run() and confirmed else "no --confirm EXECUTE"
        print(f"\nDRY RUN ({why}) — nothing sent. Add --confirm EXECUTE and MEXC_DRY_RUN=0 to fire.")
        return 0

    try:
        if sl or tp:
            d = _request("POST", f"/api/v1/private/position/change_leverage",
                         {"symbol": a.symbol, "leverage": leverage,
                          "openType": payload["openType"]}, signed=True)
            if not d.get("success"):
                log(f"leverage set skipped: {redact(d)}")
        resp = _request("POST", "/api/v1/private/order/submit", payload, signed=True)
    except Exception as e:
        log(f"ORDER FAILED {a.symbol}: {redact(e)}")
        return 1

    log(f"ORDER {a.symbol} {a.side} qty={qty} entry={entry} sl={sl} tp={tp}: {redact(resp)}")
    if not resp.get("success"):
        print(f"rejected: {redact(resp)}")
        return 1
    print(f"accepted: orderId={resp.get('data')}")
    return 0


def cmd_positions(_a):
    try:
        for p in open_positions():
            print(f"{p.get('symbol')} type {p.get('positionType')} state {p.get('state')} "
                  f"vol {p.get('holdVol')} avg {p.get('holdAvgPrice')} "
                  f"liq {p.get('liquidatePrice')} upnl {p.get('unrealized')}")
    except Exception as e:
        print(f"positions failed: {redact(e)}")
        return 1
    return 0


def cmd_close(a):
    try:
        pos = [p for p in open_positions(a.symbol) if p.get("state") == 1]
    except Exception as e:
        print(f"positions failed: {redact(e)}")
        return 1
    if not pos:
        print(f"no open {a.symbol} position")
        return 0
    for p in pos:
        is_long = int(p.get("positionType")) == 1
        payload = {
            "symbol": a.symbol,
            "price": 0,
            "vol": p.get("holdVol"),
            "side": SIDE_CLOSE_LONG if is_long else SIDE_CLOSE_SHORT,
            "type": TYPE_MARKET,
            "openType": p.get("openType", env_int("MEXC_OPEN_TYPE", 1)),
            "positionId": p.get("positionId"),
        }
        print(f"close {a.symbol} vol {payload['vol']} side {payload['side']} "
              f"positionId {payload['positionId']}")
        if (a.confirm or "").strip() != "EXECUTE" or dry_run():
            print("DRY RUN — add --confirm EXECUTE and MEXC_DRY_RUN=0 to fire")
            continue
        resp = _request("POST", "/api/v1/private/order/submit", payload, signed=True)
        log(f"CLOSE {a.symbol}: {redact(resp)}")
        print(f"  -> {redact(resp)}")
    return 0


def cmd_leverage(a):
    lev = int(a.set)
    payload = {"symbol": a.symbol, "leverage": lev,
               "openType": env_int("MEXC_OPEN_TYPE", 1), "positionType": 1}
    try:
        pos = [p for p in open_positions(a.symbol) if p.get("state") == 1]
    except Exception:
        pos = []
    if pos:
        payload = {"positionId": pos[0]["positionId"], "leverage": lev}
    print(f"leverage {a.symbol} -> {lev}x payload {json.dumps(payload)}")
    if dry_run():
        print("DRY RUN — set MEXC_DRY_RUN=0 to fire")
        return 0
    resp = _request("POST", "/api/v1/private/position/change_leverage", payload, signed=True)
    print(redact(resp))
    return 0 if resp.get("success") else 1


def main():
    ap = argparse.ArgumentParser(description="MEXC futures executor (human-approved)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("positions").set_defaults(fn=cmd_positions)

    q = sub.add_parser("quote")
    q.add_argument("symbol", default="BTC_USDT", nargs="?")
    q.set_defaults(fn=cmd_quote)

    o = sub.add_parser("order", help="preview or place a long/short")
    o.add_argument("--symbol", default="BTC_USDT")
    o.add_argument("--side", required=True, choices=["long", "short"])
    o.add_argument("--qty", required=True)
    o.add_argument("--entry", default=None)
    o.add_argument("--sl", default=None)
    o.add_argument("--tp", default=None)
    o.add_argument("--market", action="store_true")
    o.add_argument("--leverage", default=None)
    o.add_argument("--open-type", default=None)
    o.add_argument("--confirm", default="", help='literal "EXECUTE" to fire')
    o.set_defaults(fn=cmd_order)

    c = sub.add_parser("close")
    c.add_argument("symbol", default="BTC_USDT", nargs="?")
    c.add_argument("--confirm", default="")
    c.set_defaults(fn=cmd_close)

    lv = sub.add_parser("leverage")
    lv.add_argument("symbol", default="BTC_USDT", nargs="?")
    lv.add_argument("--set", required=True)
    lv.set_defaults(fn=cmd_leverage)

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()

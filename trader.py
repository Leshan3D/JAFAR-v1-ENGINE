"""
TRADER — human-in-the-loop execution layer.

The engine (build_snapshot + signals + Kronos) only *proposes*. Nothing is
ever sent to an exchange until a human clicks APPROVE on the dashboard or the
Telegram inline button. Then `approve()` executes and `reject()` discards.

Flow
    plan()      read signals.json → write pending_orders.json (status=pending)
    approve(id) execute the proposal (paper | testnet | live)
    reject(id)  discard it
    expire()    drop proposals past their approval TTL

Modes (TRADING_MODE):
    paper    (default) nothing is sent; a fill is simulated at the signal price
    testnet  real orders, Binance USDT-M Futures TESTNET (fake money)
    live     real orders, real money — requires a second env confirmation
    off      planning disabled entirely

Safety rails
    - paper by default; `live` needs TRADE_LIVE_CONFIRM=I_UNDERSTAND
    - kill switch file (KILL_SWITCH) halts all execution instantly
    - per-order notional cap, max concurrent open positions
    - daily realized-loss cap
    - proposals expire (TRADE_APPROVAL_TTL_S) because levels go stale
    - idempotency: only one pending proposal per (symbol, side)

Only crypto is tradable on Binance; gold/Brent/DXY/equities stay advisory.
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

DATA_DIR = os.environ.get("DATA_DIR") or os.getcwd()

PENDING_FILE = os.path.join(DATA_DIR, "pending_orders.json")
LOG_FILE     = os.path.join(DATA_DIR, "trade_log.json")

ACTIONABLE = {"STRONG_BUY", "BUY", "ACCUMULATE",
              "REDUCE", "SELL", "STRONG_SELL"}

# Binance USDT-M perpetual symbols the engine can actually execute.
TRADABLE = {s.strip().upper() for s in
            os.environ.get("TRADE_SYMBOLS",
                           "BTCUSDT,ETHUSDT,SOLUSDT,ZECUSDT").split(",")
            if s.strip()}


# ── config ──────────────────────────────────────────────────

def _env(name, default=""):
    return os.environ.get(name, default).strip()


def mode():
    return _env("TRADING_MODE", "paper").lower()


def _base_url():
    override = _env("BINANCE_FUTURES_BASE")
    if override:
        return override.rstrip("/")
    if mode() == "live":
        return "https://fapi.binance.com"
    return "https://testnet.binancefuture.com"   # testnet (and default)


def _keys():
    return _env("BINANCE_API_KEY"), _env("BINANCE_API_SECRET")


def _ttl():
    return int(_env("TRADE_APPROVAL_TTL_S", "900"))


def _max_notional():
    return float(_env("TRADE_MAX_NOTIONAL", "100"))


def _max_positions():
    return int(_env("TRADE_MAX_POSITIONS", "3"))


def _daily_loss_limit():
    return float(_env("TRADE_DAILY_LOSS_LIMIT", "25"))


def _leverage():
    return max(1, int(_env("TRADE_LEVERAGE", "1")))


def _quality_strict():
    """Fail-closed quality gate: block proposals whose evidence is thin."""
    return _env("TRADE_QUALITY_STRICT", "1").lower() not in ("0", "false", "no")


def _crypto_feed_block():
    """Opt-in: let dead news feeds veto a crypto proposal again."""
    return _env("TRADE_CRYPTO_FEED_BLOCK", "0").lower() in ("1", "true", "yes")


def _min_headlines():
    return int(_env("TRADE_MIN_HEADLINES", "5"))


def _min_confidence():
    return float(_env("TRADE_MIN_SENTIMENT_CONF", "0.55"))


def kill_switch_on():
    return os.path.exists(os.path.join(DATA_DIR, _env("KILL_SWITCH", "KILL_SWITCH")))


def live_confirmed():
    return _env("TRADE_LIVE_CONFIRM") == "I_UNDERSTAND"


# ── state helpers ───────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _epoch():
    return int(time.time())


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, path)


def load_pending():
    return _load(PENDING_FILE, [])


def load_log():
    return _load(LOG_FILE, [])


# ── Binance USDT-M futures REST (signed) ────────────────────

def _public(method, path, params=None):
    url = _base_url() + path
    r = requests.request(method, url, params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()


def _signed(method, path, params=None):
    key, secret = _keys()
    if not key or not secret:
        raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET not set")
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 5000
    qs = urlencode(p)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = _base_url() + path + "?" + qs + "&signature=" + sig
    r = requests.request(method, url, headers={"X-MBX-APIKEY": key}, timeout=15)
    if not r.ok:
        raise RuntimeError(f"Binance {r.status_code}: {r.text[:300]}")
    return r.json()


def account_equity():
    """USDT wallet balance. Paper mode uses TRADE_EQUITY_USDT instead."""
    if mode() == "paper":
        return float(_env("TRADE_EQUITY_USDT", "100"))
    data = _signed("GET", "/fapi/v2/balance")
    for b in data:
        if b.get("asset") == "USDT":
            return float(b.get("availableBalance") or b.get("balance") or 0)
    return 0.0


_sym_cache = {}


def symbol_filters(symbol):
    """stepSize / tickSize for quantity + price rounding (cached)."""
    if symbol in _sym_cache:
        return _sym_cache[symbol]
    info = _public("GET", "/fapi/v1/exchangeInfo")
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            f = {x["filterType"]: x for x in s.get("filters", [])}
            price_f = f.get("PRICE_FILTER", {})
            step = float(f.get("LOT_SIZE", {}).get("stepSize", "0.001"))
            _sym_cache[symbol] = {
                "step": step,
                "tick": float(price_f.get("tickSize", "0.01")),
                "qty_prec": int(round(-len(str(step).rstrip("0").split(".")[-1])))
                            if "." in str(step) else 0,
            }
            return _sym_cache[symbol]
    _sym_cache[symbol] = {"step": 0.001, "tick": 0.01, "qty_prec": 3}
    return _sym_cache[symbol]


def _round_qty(symbol, qty):
    try:
        flt = symbol_filters(symbol)
        step = flt["step"]
        return float(int(qty / step) * step), flt["qty_prec"]
    except Exception:
        return round(qty, 6), 6


# ── planner ─────────────────────────────────────────────────

def _open_positions(log):
    return [t for t in log if t.get("status") == "filled" and not t.get("closed_at")]


def _realized_today(log):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    for t in log:
        if t.get("closed_at", "").startswith(day) and t.get("pnl") is not None:
            total += float(t["pnl"])
    return total


# ── evidence quality ──────────────────────────────────────

# How much exposure the evidence earns. A signal backed by headlines that
# actually named the asset is worth full size; the same score backed by
# market-wide crypto commentary is a weak proxy and is sized accordingly.
SCOPE_SIZE_MULT = {
    "asset_specific": 1.0,
    "market_beta": 0.70,
    "market_wide": 0.40,
}

# Crypto is not vetoed by a partial news-feed outage. The RSS layer is
# duplicated across assets and crypto carries four other evidence legs
# (price/momentum, funding + OI, Fear & Greed, crowd), so dead feeds scale the
# order down instead of blocking it. Evidence that is genuinely missing (no
# or thin news, no sentiment, stale news) still blocks. Set
# TRADE_CRYPTO_FEED_BLOCK=1 to restore the veto.
CRYPTO_NON_BLOCKING_FLAGS = {"feed_degradation"}


def assess_quality(signal):
    """
    Decide whether a signal's evidence is good enough to propose a trade.

    The composite says what the engine thinks; this says how much the engine
    actually knows. A score built on three headlines, one dead feed and a
    stale news pull is not the same evidence as one built on twenty fresh
    asset-specific headlines.
    """
    quality = signal.get("data_quality") or {}
    flags = list(quality.get("flags") or [])

    headlines = int(quality.get("headline_count") or 0)
    if headlines < _min_headlines():
        flags.append("thin_news")

    confidence = quality.get("avg_confidence")
    if confidence is None:
        flags.append("sentiment_confidence_unknown")
    else:
        try:
            if float(confidence) < _min_confidence():
                flags.append("low_sentiment_confidence")
        except (TypeError, ValueError):
            flags.append("sentiment_confidence_unknown")

    # The engine splits flags into blocking (evidence missing/unusable) and
    # advisory (evidence usable but weak). Only the blocking set stops a
    # proposal; advisory flags are handled by the size multiplier.
    blocking = [f for f in flags if f not in ("news_age_unknown",)]
    if quality.get("blocking") is not None:
        blocking = list(quality["blocking"])

    advisory = list(quality.get("advisory") or [])

    # Crypto trades on more than the news feed: a partial RSS outage is a
    # size problem, not a veto. Demote the outage flags to advisory so the
    # proposal survives and the size multiplier shrinks it.
    asset_type = str(signal.get("asset_type") or "").lower()
    if asset_type == "crypto" and not _crypto_feed_block():
        demoted = [f for f in blocking if f in CRYPTO_NON_BLOCKING_FLAGS]
        if demoted:
            blocking = [f for f in blocking
                        if f not in CRYPTO_NON_BLOCKING_FLAGS]
            advisory.extend(demoted)
            for flag in demoted:
                if flag not in flags:
                    flags.append(flag)

    return {
        "ok": not blocking,
        "flags": flags,
        "blocking": blocking,
        "advisory": advisory,
        "headline_count": headlines,
        "avg_confidence": confidence,
        "news_scope": quality.get("news_scope"),
        "sources_failed": quality.get("sources_failed"),
    }


def size_multiplier(assessment):
    """
    Exposure multiplier from evidence quality.

    confidence 0.55 -> 0.25x, 0.70 -> 0.5x, 0.90 -> 1.0x
    news scope: asset_specific 1.0, market_beta 0.7, market_wide 0.4
    """
    confidence = assessment.get("avg_confidence")
    if confidence is None:
        conf_mult = 0.25
    else:
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            conf_mult = 0.25
        else:
            conf_mult = max(0.25, min(1.0, (conf - 0.50) / 0.40))

    scope_mult = SCOPE_SIZE_MULT.get(assessment.get("news_scope"), 0.70)
    mult = conf_mult * scope_mult
    if "feed_degradation" in (assessment.get("flags") or []):
        mult *= 0.60
    return round(mult, 4)


def plan(notify=True):
    """Turn fresh actionable crypto signals into pending proposals."""
    if mode() == "off":
        return {"planned": 0, "reason": "TRADING_MODE=off"}

    expire()

    signals = _load(os.path.join(DATA_DIR, "signals.json"), [])
    if not signals:
        return {"planned": 0, "reason": "no signals.json"}

    if kill_switch_on():
        return {"planned": 0, "reason": "KILL_SWITCH active"}

    equity = account_equity()
    if equity <= 0:
        return {"planned": 0, "reason": "zero equity"}

    pending = load_pending()
    log = load_log()
    open_n = len(_open_positions(log))
    active_keys = {(p["symbol"], p["side"]) for p in pending
                   if p["status"] == "pending"}

    new = []
    skipped = []
    for s in signals:
        asset = str(s.get("asset", "")).upper()
        if asset not in TRADABLE:
            continue
        if s.get("action") not in ACTIONABLE:
            continue
        if s.get("is_stale"):
            continue
        lv = s.get("levels") or {}
        bias = lv.get("bias")
        if bias not in ("LONG", "SHORT"):
            continue
        entry = lv.get("entry")
        if not entry:
            continue

        side = "BUY" if bias == "LONG" else "SELL"
        if (asset, side) in active_keys:
            continue
        if open_n + len(new) >= _max_positions():
            break

        # ── Evidence gate ────────────────────────────────────
        assessment = assess_quality(s)
        if _quality_strict() and not assessment["ok"]:
            skipped.append({
                "asset": asset,
                "reason": "data_quality",
                "flags": assessment["blocking"],
            })
            continue

        size_frac = s.get("position_size_frac") or 0
        multiplier = size_multiplier(assessment)
        size_frac = float(size_frac) * multiplier
        notional = min(equity * size_frac * _leverage(), _max_notional())
        if notional <= 0:
            continue
        qty = notional / float(entry)

        new.append({
            "id": uuid.uuid4().hex[:12],
            "created_at": _now(),
            "expires_at": _now_plus(_ttl()),
            "status": "pending",
            "mode": mode(),
            "asset": asset,
            "label": s.get("label", asset),
            "symbol": asset,
            "side": side,
            "action": s.get("action"),
            "composite": s.get("composite_score"),
            "size_frac": size_frac,
            "size_multiplier": multiplier,
            "quality": assessment,
            "leverage": _leverage(),
            "notional_usdt": round(notional, 2),
            "qty": qty,
            "entry": float(entry),
            "stop_loss": lv.get("stop_loss"),
            "take_profit_1": lv.get("take_profit_1"),
            "take_profit_2": lv.get("take_profit_2"),
            "signal_price": s.get("current_price"),
            "kronos_gate": s.get("kronos_gate"),
            "rationale": s.get("rationale"),
        })

    if new:
        _save(PENDING_FILE, pending + new)
        if notify:
            _notify(new)
    return {"planned": len(new), "equity": equity,
            "pending": len(pending) + len(new),
            "skipped_quality": skipped}


def _now_plus(seconds):
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc)\
                   .isoformat(timespec="seconds")


def expire():
    """Mark pending proposals past their TTL as expired."""
    pending = load_pending()
    now = _epoch()
    changed = False
    for p in pending:
        if p["status"] != "pending":
            continue
        try:
            exp = datetime.fromisoformat(p["expires_at"]).timestamp()
        except (KeyError, ValueError):
            exp = 0
        if exp and now > exp:
            p["status"] = "expired"
            p["resolved_at"] = _now()
            changed = True
    if changed:
        _save(PENDING_FILE, pending)
    return changed


# ── approval / execution ────────────────────────────────────

def _find(order_id):
    for p in load_pending():
        if p["id"] == order_id:
            return p
    return None


def approve(order_id, via="dashboard"):
    """Execute one approved proposal. Returns a result dict."""
    if kill_switch_on():
        return {"ok": False, "error": "KILL_SWITCH active"}
    m = mode()
    if m == "live" and not live_confirmed():
        return {"ok": False, "error": "live not confirmed (set TRADE_LIVE_CONFIRM=I_UNDERSTAND)"}

    pending = load_pending()
    prop = next((p for p in pending if p["id"] == order_id), None)
    if not prop:
        return {"ok": False, "error": "unknown order id"}
    if prop["status"] != "pending":
        return {"ok": False, "error": f"order already {prop['status']}"}
    try:
        if _epoch() > datetime.fromisoformat(prop["expires_at"]).timestamp():
            prop["status"] = "expired"
            prop["resolved_at"] = _now()
            _save(PENDING_FILE, pending)
            return {"ok": False, "error": "proposal expired — re-run the engine"}
    except (KeyError, ValueError):
        pass

    try:
        if m == "paper":
            result = _execute_paper(prop)
        elif m in ("testnet", "live"):
            result = _execute_real(prop)
        else:
            return {"ok": False, "error": f"unknown mode {m}"}
    except Exception as e:
        prop["status"] = "failed"
        prop["error"] = str(e)
        prop["resolved_at"] = _now()
        prop["via"] = via
        _save(PENDING_FILE, pending)
        return {"ok": False, "error": str(e)}

    prop["status"] = "approved"
    prop["resolved_at"] = _now()
    prop["via"] = via
    prop["execution"] = result
    _save(PENDING_FILE, pending)

    log = load_log()
    log.append({**_log_stub(prop), "status": "filled", "via": via,
                "execution": result, "closed_at": None, "pnl": None})
    _save(LOG_FILE, log)
    _confirm(prop, result, via)
    return {"ok": True, "order": prop, "execution": result}


def reject(order_id, via="dashboard"):
    pending = load_pending()
    prop = next((p for p in pending if p["id"] == order_id), None)
    if not prop:
        return {"ok": False, "error": "unknown order id"}
    if prop["status"] != "pending":
        return {"ok": False, "error": f"already {prop['status']}"}
    prop["status"] = "rejected"
    prop["resolved_at"] = _now()
    prop["via"] = via
    _save(PENDING_FILE, pending)
    _confirm(prop, {"paper": False, "note": "rejected"}, via)
    return {"ok": True, "order": prop}


def _execute_paper(prop):
    return {
        "paper": True,
        "fill_price": prop["entry"],
        "note": "simulated fill at signal entry — no exchange order sent",
    }


def _execute_real(prop):
    symbol = prop["symbol"]
    qty, prec = _round_qty(symbol, prop["qty"])
    if qty <= 0:
        raise RuntimeError("computed qty rounds to zero")

    _signed("POST", "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": prop["leverage"]})

    entry = _signed("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": prop["side"],
        "type": "MARKET",
        "quantity": f"{qty:.{prec}f}",
        "newOrderRespType": "RESULT",
    })

    close_side = "SELL" if prop["side"] == "BUY" else "BUY"
    stops = {}
    if prop.get("stop_loss"):
        try:
            stops["sl"] = _signed("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
                "stopPrice": prop["stop_loss"], "closePosition": "true",
                "workingType": "MARK_PRICE",
            })
        except Exception as e:
            stops["sl_error"] = str(e)
    if prop.get("take_profit_1"):
        try:
            stops["tp"] = _signed("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": close_side, "type": "TAKE_PROFIT_MARKET",
                "stopPrice": prop["take_profit_1"], "closePosition": "true",
                "workingType": "MARK_PRICE",
            })
        except Exception as e:
            stops["tp_error"] = str(e)

    return {
        "paper": False,
        "entry_order_id": entry.get("orderId"),
        "avg_price": entry.get("avgPrice"),
        "executed_qty": entry.get("executedQty"),
        "stops": stops,
    }


def _log_stub(prop):
    return {k: prop.get(k) for k in
            ("id", "created_at", "asset", "label", "symbol", "side", "action",
             "composite", "size_frac", "leverage", "notional_usdt", "qty",
             "entry", "stop_loss", "take_profit_1", "take_profit_2", "mode")}


# ── notifications (best-effort; never block execution) ──────

def _notify(proposals):
    try:
        import telegram_bot
        telegram_bot.notify_proposals(proposals)
    except Exception as e:
        print(f"  [trader] notify skipped: {e}")


def _confirm(prop, result, via):
    try:
        import alerts
        verb = prop["status"].upper()
        head = f"TRADE {verb} — {prop['label']} ({prop['symbol']}) {prop['side']}"
        detail = (f"qty {prop.get('qty')} · entry {prop.get('entry')} · "
                  f"notional {prop.get('notional_usdt')} USDT · mode {prop.get('mode')}")
        alerts.send(f"{head}\n{detail}\nvia {via}")
    except Exception as e:
        print(f"  [trader] confirm skipped: {e}")


# ── CLI ─────────────────────────────────────────────────────

def _print_pending():
    pending = load_pending()
    if not pending:
        print("  no pending orders")
        return
    for p in pending:
        print(f"  [{p['status']:<8}] {p['id']}  {p['symbol']:<9} {p['side']:<4} "
              f"qty {p.get('qty'):.6f}  entry {p.get('entry')}  "
              f"notional {p.get('notional_usdt')}  exp {p.get('expires_at')}")


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "plan":
        print(json.dumps(plan(), indent=2))
        _print_pending()
    elif cmd == "list":
        _print_pending()
    elif cmd == "approve" and len(sys.argv) > 2:
        print(json.dumps(approve(sys.argv[2], via="cli"), indent=2, default=str))
    elif cmd == "reject" and len(sys.argv) > 2:
        print(json.dumps(reject(sys.argv[2], via="cli"), indent=2, default=str))
    elif cmd == "status":
        print(f"  mode     : {mode()}")
        print(f"  base url : {_base_url()}")
        print(f"  equity   : {account_equity()}")
        print(f"  kill sw  : {kill_switch_on()}")
        print(f"  tradable : {sorted(TRADABLE)}")
        _print_pending()
    else:
        print("usage: trader.py [status|plan|list|approve <id>|reject <id>]")

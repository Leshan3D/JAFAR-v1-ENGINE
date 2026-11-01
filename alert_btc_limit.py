#!/usr/bin/env python3
"""
Watch MEXC BTC_USDT and alert when a limit BUY fills (or price runs away first).

Why price-based: the public feed can't read your private order status, but a
resting limit BUY at TRIGGER is filled as soon as the best ask trades at/below
it. We poll the public ticker and fire when min(ask, last) <= TRIGGER.

Alerts: desktop notification + sound + Telegram (via alerts.py, reusing the
bot's adopted chat id). Fires each condition once, then exits.

Config (env or --flag):
  ALERT_SYMBOL   default BTC_USDT
  ALERT_TRIGGER  default 77870   (your limit price)
  ALERT_RUNAWAY  default 78400   (price ran up without filling)
  ALERT_POLL_S   default 3
  ALERT_QUIET    default 0       (1 = no startup "armed" ping)
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("DATA_DIR", HERE)

VENUE_SYMBOL = {"mexc": "BTC_USDT", "bybit": "BTCUSDT", "okx": "BTC-USDT-SWAP"}
STATE = os.path.join(HERE, "btc_limit_alert_state.json")
LOG = os.path.join(HERE, "btc_limit_alert.log")

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        try:
            for _line in open(os.path.join(HERE, ".env")):
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
        except OSError:
            pass


def log(msg):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def quote(venue, symbol):
    if venue == "bybit":
        url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.load(r)["result"]["list"][0]
        return (float(d["lastPrice"]),
                float(d["ask1Price"] or d["lastPrice"]),
                float(d["bid1Price"] or d["lastPrice"]))
    if venue == "okx":
        url = f"https://www.okx.com/api/v5/market/ticker?instId={symbol}"
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.load(r)["data"][0]
        return float(d["last"]), float(d["askPx"]), float(d["bidPx"])
    url = f"https://contract.mexc.com/api/v1/contract/ticker?symbol={symbol}"
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.load(r)["data"]
    return float(d["lastPrice"]), float(d["ask1"]), float(d["bid1"])


def desktop(title, body, urgency="normal"):
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-a", "BTC limit alert", title, body],
            env=env, timeout=10, check=False,
        )
    except Exception as e:
        log(f"notify-send failed: {e}")


def sound():
    for path in (
        "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
        "/usr/share/sounds/freedesktop/stereo/complete.oga",
    ):
        if os.path.exists(path):
            try:
                subprocess.run(["paplay", path], timeout=20, check=False)
                return
            except Exception:
                pass
    print("\a", end="", flush=True)


def telegram(text):
    try:
        sys.path.insert(0, HERE)
        import alerts
        return alerts.send(text)
    except Exception as e:
        log(f"telegram failed: {e}")
        return False


def fire(kind, symbol, last, ask, bid, trigger, runaway):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    if kind == "fill":
        title = f"{symbol} limit FILLED @ {trigger:,.0f}"
        body = (f"Last {last:,.1f} | ask {ask:,.1f} | bid {bid:,.1f} | {ts}\n"
                f"Your buy should be live — set/adjust TP & SL now.")
        urgency = "critical"
    else:
        title = f"{symbol} limit MISSED — price ran away"
        body = (f"Last {last:,.1f} (>= {runaway:,.0f}), never filled @ {trigger:,.0f} | {ts}\n"
                f"Do not chase; reassess the setup.")
        urgency = "critical"

    log(f"{kind.upper()}: {body}")
    desktop(title, body, urgency)
    sound()
    telegram(f"{title}\n{body}")
    try:
        with open(STATE, "w") as f:
            json.dump({"kind": kind, "symbol": symbol, "last": last, "ask": ask,
                       "bid": bid, "trigger": trigger, "runaway": runaway, "ts": ts}, f, indent=2)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default=os.environ.get("ALERT_VENUE", "mexc"),
                    choices=sorted(VENUE_SYMBOL))
    ap.add_argument("--symbol", default=os.environ.get("ALERT_SYMBOL"))
    ap.add_argument("--trigger", type=float, default=float(os.environ.get("ALERT_TRIGGER", "77870")))
    ap.add_argument("--runaway", type=float, default=float(os.environ.get("ALERT_RUNAWAY", "78400")))
    ap.add_argument("--poll", type=float, default=float(os.environ.get("ALERT_POLL_S", "3")))
    ap.add_argument("--once", action="store_true", help="print current state and exit")
    ap.add_argument("--quiet", action="store_true", default=os.environ.get("ALERT_QUIET") == "1")
    a = ap.parse_args()
    if not a.symbol:
        a.symbol = VENUE_SYMBOL[a.venue]

    try:
        last, ask, bid = quote(a.venue, a.symbol)
    except Exception as e:
        last = ask = bid = None
        log(f"quote error on startup: {e}")

    if a.once:
        if last is None:
            print("quote unavailable")
        else:
            state = "fill" if min(ask, last) <= a.trigger else (
                "runaway" if last >= a.runaway else "waiting")
            print(f"{a.symbol} last={last} ask={ask} bid={bid} | "
                  f"trigger={a.trigger} runaway={a.runaway} -> {state}")
        return

    log(f"watching {a.venue}:{a.symbol}: fill<= {a.trigger:,.0f}, runaway>= {a.runaway:,.0f}, poll {a.poll}s")
    if not a.quiet:
        msg = (f"Armed: {a.symbol} limit @ {a.trigger:,.0f}. "
               f"Will alert on fill (ask<= {a.trigger:,.0f}) or if it runs >= {a.runaway:,.0f}.")
        desktop("BTC limit alert armed", msg)
        telegram(f"✅ {msg}")

    while True:
        try:
            last, ask, bid = quote(a.venue, a.symbol)
        except Exception as e:
            log(f"quote error: {e}")
            time.sleep(max(a.poll, 5))
            continue

        if min(ask, last) <= a.trigger:
            fire("fill", a.symbol, last, ask, bid, a.trigger, a.runaway)
            return
        if last >= a.runaway:
            fire("runaway", a.symbol, last, ask, bid, a.trigger, a.runaway)
            return
        time.sleep(a.poll)


if __name__ == "__main__":
    main()

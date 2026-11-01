#!/usr/bin/env python3
"""
Position manager for the BTC long entered @ 77,870 (Bybit BTCUSDT perp).

Watches the live Bybit price and fires ONE alert per milestone, persisted so a
restart never double-pings:
  TP1   78,387.2  -> sell 50%, move stop to breakeven
  BE    give-back -> only after TP1: price back at entry, confirm stop is BE
  TP2   78,622.4  -> sell next 30%
  RUN   80,159.9  -> close/trail the last 20% (Kronos target)
  SL    77,613.0  -> stop breached, position should be flat

Alerts: desktop + sound + Telegram (reuses alerts.py / adopted chat id).
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
LOG = os.path.join(HERE, "btc_position_alert.log")
STATE = os.path.join(HERE, "btc_position_state.json")

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


def quote(symbol="BTCUSDT"):
    url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.load(r)["result"]["list"][0]
    return float(d["lastPrice"]), float(d["ask1Price"] or d["lastPrice"]), float(d["bid1Price"] or d["lastPrice"])


def desktop(title, body, urgency="normal"):
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    try:
        subprocess.run(["notify-send", "-u", urgency, "-a", "BTC position", title, body],
                       env=env, timeout=10, check=False)
    except Exception as e:
        log(f"notify-send failed: {e}")


def sound():
    for path in ("/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
                 "/usr/share/sounds/freedesktop/stereo/complete.oga"):
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


def load_state():
    try:
        with open(STATE) as f:
            return set(json.load(f).get("fired", []))
    except (OSError, json.JSONDecodeError):
        return set()


def save_state(fired):
    try:
        with open(STATE, "w") as f:
            json.dump({"fired": sorted(fired)}, f, indent=2)
    except OSError:
        pass


class Cfg:
    def __init__(self, a):
        self.symbol = a.symbol
        self.entry = a.entry
        self.sl = a.sl
        self.tp1 = a.tp1
        self.tp2 = a.tp2
        self.runner = a.runner
        self.qty = a.qty
        self.acct = a.acct
        self.poll = a.poll


def notify(kind, cfg, last, fired):
    pnl = cfg.qty * (last - cfg.entry)
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    msgs = {
        "TP1": (f"TP1 HIT {cfg.tp1:,.1f}",
                f"Sell 50% (~{cfg.qty*0.5:.3f} BTC) and move stop to breakeven {cfg.entry:,.0f}.\n"
                f"Last {last:,.1f} | unrealized on full size +${pnl:,.0f} | {ts}"),
        "BE": (f"Back at entry {cfg.entry:,.0f} after TP1",
               f"Price gave back to entry — confirm stop is at breakeven, protect the runner.\n"
               f"Last {last:,.1f} | {ts}"),
        "TP2": (f"TP2 HIT {cfg.tp2:,.1f}",
                f"Sell next 30% (~{cfg.qty*0.3:.3f} BTC); keep stop at/above breakeven and trail.\n"
                f"Last {last:,.1f} | unrealized on full size +${pnl:,.0f} | {ts}"),
        "RUN": (f"Runner target {cfg.runner:,.1f} HIT",
                f"Close or trail the last 20% (~{cfg.qty*0.2:.3f} BTC). Kronos target reached.\n"
                f"Last {last:,.1f} | unrealized on full size +${pnl:,.0f} | {ts}"),
        "SL": (f"STOP {cfg.sl:,.1f} BREACHED",
               f"Position should be flat — check the Breakout terminal.\n"
               f"Last {last:,.1f} | {ts}"),
    }
    title, body = msgs[kind]
    log(f"{kind}: {title} | {body.replace(chr(10), ' ')}")
    desktop(title, body, "critical")
    sound()
    telegram(f"{title}\n{body}")
    fired.add(kind)
    save_state(fired)


def once(cfg):
    last, ask, bid = quote(cfg.symbol)
    pnl = cfg.qty * (last - cfg.entry)
    pnl_pct = pnl / cfg.acct * 100
    print(f"{cfg.symbol} last={last:,.1f} ask={ask:,.1f} bid={bid:,.1f}")
    print(f"entry {cfg.entry:,.0f} | SL {cfg.sl:,.0f} | TP1 {cfg.tp1:,.1f} | "
          f"TP2 {cfg.tp2:,.1f} | RUN {cfg.runner:,.1f}")
    print(f"unrealized ${pnl:+,.2f} ({pnl_pct:+.2f}% acct) | fired={sorted(load_state())}")
    print(f"next: to TP1 {cfg.tp1-last:+.1f} | to SL {cfg.sl-last:+.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=os.environ.get("POS_SYMBOL", "BTCUSDT"))
    ap.add_argument("--entry", type=float, default=float(os.environ.get("POS_ENTRY", "77870")))
    ap.add_argument("--sl", type=float, default=float(os.environ.get("POS_SL", "77613")))
    ap.add_argument("--tp1", type=float, default=float(os.environ.get("POS_TP1", "78387.2")))
    ap.add_argument("--tp2", type=float, default=float(os.environ.get("POS_TP2", "78622.4")))
    ap.add_argument("--runner", type=float, default=float(os.environ.get("POS_RUNNER", "80159.9")))
    ap.add_argument("--qty", type=float, default=float(os.environ.get("POS_QTY", "0.349")))
    ap.add_argument("--acct", type=float, default=float(os.environ.get("POS_ACCT", "4967.92")))
    ap.add_argument("--poll", type=float, default=float(os.environ.get("POS_POLL_S", "3")))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--quiet", action="store_true", default=os.environ.get("ALERT_QUIET") == "1")
    a = ap.parse_args()
    cfg = Cfg(a)

    if a.once:
        once(cfg)
        return

    fired = load_state()
    log(f"watching position {cfg.symbol}: entry {cfg.entry:,.0f} SL {cfg.sl:,.0f} "
        f"TP1 {cfg.tp1:,.1f} TP2 {cfg.tp2:,.1f} RUN {cfg.runner:,.1f} | fired={sorted(fired)}")
    if not a.quiet:
        desktop("BTC position watcher armed",
                f"Will ping TP1 {cfg.tp1:,.0f}, TP2 {cfg.tp2:,.0f}, runner {cfg.runner:,.0f}, or stop {cfg.sl:,.0f}.")
        telegram(f"✅ Position watcher armed — TP1 {cfg.tp1:,.0f} / TP2 {cfg.tp2:,.0f} / "
                 f"runner {cfg.runner:,.0f} / SL {cfg.sl:,.0f}")

    while True:
        try:
            last, ask, bid = quote(cfg.symbol)
        except Exception as e:
            log(f"quote error: {e}")
            time.sleep(max(cfg.poll, 5))
            continue

        if "SL" not in fired and last <= cfg.sl:
            notify("SL", cfg, last, fired)
            return

        if "TP1" not in fired and last >= cfg.tp1:
            notify("TP1", cfg, last, fired)

        if ("TP1" in fired and "TP2" not in fired and "BE" not in fired
                and last <= cfg.entry):
            notify("BE", cfg, last, fired)

        if "TP2" not in fired and last >= cfg.tp2:
            notify("TP2", cfg, last, fired)

        if "RUN" not in fired and last >= cfg.runner:
            notify("RUN", cfg, last, fired)
            return

        time.sleep(cfg.poll)


if __name__ == "__main__":
    main()

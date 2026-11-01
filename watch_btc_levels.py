#!/usr/bin/env python3
"""
BTC round-level watcher — pings once per level on first touch.

Default levels: 78,500 / 79,000 / 79,500 / 80,000 / 80,500 (upward).
Override anytime with --levels.

    python3 watch_btc_levels.py --once          # where are we vs the levels?
    python3 watch_btc_levels.py                 # watch forever (systemd)
    python3 watch_btc_levels.py --reset         # re-arm every level
    python3 watch_btc_levels.py --dry --levels 78100   # test without pinging

Alerts: desktop + sound + Telegram, once per level. State persists in
btc_levels_state.json so a restart doesn't re-spam.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("DATA_DIR", HERE)
sys.path.insert(0, HERE)

from alert_btc_limit import quote, desktop, sound, telegram  # reuse helpers

STATE = os.path.join(HERE, "btc_levels_state.json")
LOG = os.path.join(HERE, "btc_levels_alert.log")
DEFAULT_LEVELS = [78500.0, 79000.0, 79500.0, 80000.0, 80500.0]


def log(msg):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_fired():
    try:
        with open(STATE) as f:
            return {float(x) for x in json.load(f).get("fired", [])}
    except (OSError, ValueError):
        return set()


def save_fired(fired, last=None):
    try:
        with open(STATE, "w") as f:
            json.dump({"fired": sorted(fired), "last": last,
                       "ts": datetime.now(timezone.utc).astimezone().isoformat()},
                      f, indent=2)
    except OSError as e:
        log(f"state save failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default=os.environ.get("LEVEL_VENUE", "bybit"),
                    choices=["bybit", "mexc", "okx"])
    ap.add_argument("--symbol", default=os.environ.get("LEVEL_SYMBOL", "BTCUSDT"))
    ap.add_argument("--levels", default=os.environ.get(
        "LEVELS", ",".join(str(int(x)) for x in DEFAULT_LEVELS)))
    ap.add_argument("--poll", type=float, default=float(os.environ.get("LEVEL_POLL_S", "15")))
    ap.add_argument("--once", action="store_true", help="print distance to levels and exit")
    ap.add_argument("--reset", action="store_true", help="re-arm all levels and exit")
    ap.add_argument("--dry", action="store_true", help="log fires but send nothing (test)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    levels = sorted({float(x) for x in a.levels.replace(" ", "").split(",") if x})

    if a.reset:
        save_fired(set())
        print(f"re-armed: {', '.join(f'{x:,.0f}' for x in levels)}")
        return 0

    fired = load_fired()

    try:
        last, ask, bid = quote(a.venue, a.symbol)
    except Exception as e:
        log(f"quote error on startup: {e}")
        last = ask = bid = None

    if a.once:
        if last is None:
            print("quote unavailable")
            return 1
        print(f"{a.symbol} last={last:,.1f}  (fired: {sorted(fired) or 'none'})")
        for lv in levels:
            state = "FIRED" if lv in fired else ("at/above" if last >= lv else f"{(lv - last):+,.0f}")
            print(f"  {lv:,.0f}  -> {state}")
        return 0

    log(f"watching {a.venue}:{a.symbol} levels {[f'{x:,.0f}' for x in levels]} "
        f"poll {a.poll}s dry={a.dry} fired={sorted(fired) or 'none'}")
    if not a.quiet and not a.dry:
        desktop("BTC level watcher armed",
                f"Watching {', '.join(f'{x:,.0f}' for x in levels)} (last {last:,.0f})")
        telegram(f"✅ BTC level watcher armed: {', '.join(f'{x:,.0f}' for x in levels)}")

    while True:
        try:
            last, ask, bid = quote(a.venue, a.symbol)
        except Exception as e:
            log(f"quote error: {e}")
            time.sleep(max(a.poll, 5))
            continue

        for lv in levels:
            if lv not in fired and last >= lv:
                ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                nxt = next((x for x in levels if x > lv), None)
                title = f"BTC hit {lv:,.0f}"
                body = (f"Last {last:,.1f} | ask {ask:,.1f} | bid {bid:,.1f} | {ts}\n"
                        + (f"Next level {nxt:,.0f}. " if nxt else "Last level cleared. ")
                        + "Re-check the engine before shorting/fading.")
                log(f"LEVEL {lv:,.0f}: {body}")
                if not a.dry:
                    desktop(title, body, "critical")
                    sound()
                    telegram(f"{title}\n{body}")
                fired.add(lv)
                save_fired(fired, last)

        if len(fired) >= len(levels):
            log("all levels cleared — watcher done")
            return 0
        time.sleep(a.poll)


if __name__ == "__main__":
    sys.exit(main() or 0)

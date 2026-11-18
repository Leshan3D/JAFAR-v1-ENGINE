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


def load_state():
    """Returns (fired_levels, reference_price_at_arm)."""
    try:
        with open(STATE) as f:
            d = json.load(f)
        return {float(x) for x in d.get("fired", [])}, d.get("ref")
    except (OSError, ValueError):
        return set(), None


def save_state(fired, last=None, ref=None):
    try:
        with open(STATE, "w") as f:
            json.dump({"fired": sorted(fired), "ref": ref, "last": last,
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
        try:
            ref, _, _ = quote(a.venue, a.symbol)
        except Exception as e:
            log(f"quote error on reset: {e}")
            ref = None
        save_state(set(), ref=ref)
        print(f"re-armed at ref {ref:,.1f}: {', '.join(f'{x:,.0f}' for x in levels)}" if ref
              else f"re-armed (no ref): {', '.join(f'{x:,.0f}' for x in levels)}")
        return 0

    fired, ref = load_state()

    try:
        last, ask, bid = quote(a.venue, a.symbol)
    except Exception as e:
        log(f"quote error on startup: {e}")
        last = ask = bid = None

    if a.once:
        if last is None:
            print("quote unavailable")
            return 1
        print(f"{a.symbol} last={last:,.1f}  ref={ref if ref is None else f'{ref:,.1f}'}"
              f"  (fired: {sorted(fired) or 'none'})")
        base = ref if ref is not None else last
        for lv in levels:
            if lv in fired:
                state = "FIRED"
            else:
                direction = "down" if lv < base else "up"
                state = f"{direction:>4} {(lv - last):+,.0f}"
            print(f"  {lv:,.0f}  -> {state}")
        return 0

    if ref is None and last is not None:
        ref = last
        save_state(fired, last, ref)

    log(f"watching {a.venue}:{a.symbol} levels {[f'{x:,.0f}' for x in levels]} "
        f"poll {a.poll}s dry={a.dry} ref={ref} fired={sorted(fired) or 'none'}")
    if not a.quiet and not a.dry:
        shown = f"{last:,.0f}" if last is not None else "n/a"
        desktop("BTC level watcher armed",
                f"Watching {', '.join(f'{x:,.0f}' for x in levels)} (last {shown})")
        telegram(f"✅ BTC level watcher armed: {', '.join(f'{x:,.0f}' for x in levels)}")

    while True:
        try:
            last, ask, bid = quote(a.venue, a.symbol)
        except Exception as e:
            log(f"quote error: {e}")
            time.sleep(max(a.poll, 5))
            continue

        for lv in levels:
            if lv in fired:
                continue
            # Direction is decided from the price when the watcher was armed,
            # so a stop below the market fires on the way DOWN and a target
            # above it fires on the way UP.
            downward = lv < (ref if ref is not None else last)
            hit = (last <= lv) if downward else (last >= lv)
            if not hit:
                continue
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
            save_state(fired, last, ref)

        if len(fired) >= len(levels):
            log("all levels cleared — watcher done")
            return 0
        time.sleep(a.poll)


if __name__ == "__main__":
    sys.exit(main() or 0)

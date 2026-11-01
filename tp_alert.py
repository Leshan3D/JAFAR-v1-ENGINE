#!/usr/bin/env python3
"""
JAFAR trade alert watcher for the Omarchy desktop.

Polls Binance for a BTCUSDT-style symbol and fires a native Omarchy desktop
notification when entry neighbors are touched: TP1, TP2 (normal urgency) and
SL (critical urgency), each with live P&L for a given margin x leverage.

One-shot per level by default; use --rearm <seconds> to re-watch a level after
the price leaves its trigger zone again.

Usage:
  python3 tp_alert.py --entry 85919 --sl 86633 --tp1 84233 --tp2 83414 \\
      --margin 100 --leverage 7
  python3 tp_alert.py --once      # single check, prints status, exits
"""

import argparse
import datetime
import json
import subprocess
import sys
import time
import urllib.request

BINANCE_URL = "https://api.binance.com/api/v3/ticker/price?symbol={symbol}"

symbol_glyph = "\u20bf"


def glyph_for(symbol):
    return "\u25c8" if not symbol.startswith("BTC") else "\u20bf"


def get_price(symbol):
    req = urllib.request.Request(
        BINANCE_URL.format(symbol=symbol),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return float(json.load(r)["price"])


def notify(headline, description, urgency="normal", glyph=None, timeout_ms=10000):
    glyph = glyph or symbol_glyph
    args = ["-g", glyph, "-u", urgency, "-t", str(timeout_ms), headline, description]
    for cmd in (["omarchy", "notification", "send"] + args,
                ["omarchy-notification-send"] + args,
                ["notify-send", "-u", urgency, headline, description]):
        try:
            subprocess.run(cmd, check=False)
            return
        except FileNotFoundError:
            continue
        except OSError as e:
            print(f"[tp_alert] notify failed: {e}", flush=True)


def build_levels(entry, sl, tp1, tp2, margin, leverage, confirm=2):
    short = sl > entry
    notional = margin * leverage
    levels = []
    specs = [
        ("SL", sl, "critical", "\u26a0", 30000),
        ("TP1", tp1, "normal", "\U0001f3af", 10000),
        ("TP2", tp2, "normal", "\U0001f3af", 15000),
    ]
    for name, level, urgency, glyph, ms in specs:
        if level is None:
            continue
        if short:
            tp_trigger = (lambda p, lv=level: p <= lv)
            sl_trigger = (lambda p, lv=level: p >= lv)
            hit = tp_trigger if name.startswith("TP") else sl_trigger
            pnl = (entry - level) / entry * notional
        else:
            tp_trigger = (lambda p, lv=level: p >= lv)
            sl_trigger = (lambda p, lv=level: p <= lv)
            hit = tp_trigger if name.startswith("TP") else sl_trigger
            pnl = (level - entry) / entry * notional
        dist_pct = abs(level / entry - 1) * 100
        levels.append(dict(name=name, level=level, hit=hit, pnl=pnl,
                           dist_pct=dist_pct, urgency=urgency, glyph=glyph,
                           ms=ms, fired=False, streak=0, confirm=confirm))
    return levels, short


def main():
    ap = argparse.ArgumentParser(description="Binance price alert -> Omarchy")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--sl", type=float)
    ap.add_argument("--tp1", type=float)
    ap.add_argument("--tp2", type=float)
    ap.add_argument("--margin", type=float, default=100)
    ap.add_argument("--leverage", type=float, default=7)
    ap.add_argument("--poll", type=float, default=15.0)
    ap.add_argument("--confirm", type=int, default=2, help="consecutive polls in-zone before firing")
    ap.add_argument("--rearm", type=float)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--log")
    args = ap.parse_args()

    global symbol_glyph
    symbol_glyph = glyph_for(args.symbol)

    levels, short = build_levels(args.entry, args.sl, args.tp1, args.tp2,
                                 args.margin, args.leverage, confirm=args.confirm)
    side = "SHORT" if short else "LONG"
    print(f"[tp_alert] Watching {args.symbol} ({side}) entry ${args.entry:,.2f} "
          f"| SL ${args.sl or 0:,.2f} | TP1 ${args.tp1 or 0:,.2f} | TP2 ${args.tp2 or 0:,.2f} "
          f"| margin ${args.margin:,.0f} x{args.leverage:g}", flush=True)

    last_notified_price = {}
    try:
        while True:
            try:
                price = get_price(args.symbol)
            except Exception as e:
                print(f"[tp_alert] poll error: {e}", flush=True)
                if args.once:
                    sys.exit(2)
                time.sleep(args.poll)
                continue

            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            if args.verbose:
                print(f"[{now}] ${price:,.2f}", flush=True)

            for lv in levels:
                zone_active = lv["hit"](price)
                if zone_active:
                    lv["streak"] += 1
                else:
                    lv["streak"] = 0
                if zone_active and abs(price - args.entry) < 0.6 * args.entry \
                        and lv["streak"] >= lv["confirm"] and not lv["fired"]:
                    lv["fired"] = True
                    sign = "+" if lv["pnl"] >= 0 else ""
                    desc = (
                        f"Price ${price:,.2f} touched {lv['name']} ${lv['level']:,.2f} "
                        f"({lv['dist_pct']:.2f}% from entry) | "
                        f"P&L {sign}${lv['pnl']:,.2f} ({lv['pnl'] / args.margin * 100:+.1f}% on margin)"
                    )
                    notify(f"{args.symbol} {lv['name']} HIT", desc,
                           urgency=lv["urgency"], glyph=lv["glyph"], timeout_ms=lv["ms"])
                    line = f"[{now}] {lv['name']} hit @ ${price:,.2f} | {desc}"
                    print(line, flush=True)
                    if args.log:
                        with open(args.log, "a") as f:
                            f.write(line + "\n")
                elif not zone_active and lv["fired"] and args.rearm:
                    if last_notified_price.get(lv["name"]) != price:
                        lv["fired"] = False
                        last_notified_price[lv["name"]] = price
                        if args.verbose:
                            print(f"[tp_alert] re-armed {lv['name']}", flush=True)

            if args.once:
                sys.exit(0)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("[tp_alert] stopped", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
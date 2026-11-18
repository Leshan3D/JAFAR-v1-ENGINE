"""
SERVICE CONDUCTOR — runs the trading pipeline on a schedule.

Designed for a container (Railway) but runs anywhere:
    python3 run_service.py

Loops (all env-tunable, see .env.example):
  full pipeline   every PIPELINE_INTERVAL_S     news + FinBERT + FRED + Polymarket + F&G
  light refresh   every PRICE_REFRESH_INTERVAL_S price-only update + signals recompute
  zone watcher    every ZONE_POLL_INTERVAL_S     entry-zone crossings + action flips → Telegram
  macro alarms    every MACRO_POLL_INTERVAL_S    pre-print warnings + post-print pipeline wake

Everything writes to DATA_DIR (cwd). The Telegram bot runs as a thread and
wakes this loop via trigger_run for /run.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# ── environment bootstrap ───────────────────────────────────

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.getcwd()
os.makedirs(DATA_DIR, exist_ok=True)
os.chdir(DATA_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except ImportError:
    pass

import alerts  # noqa: E402  (needs DATA_DIR settled first)

FULL_INTERVAL = int(os.environ.get("PIPELINE_INTERVAL_S", 3600))
PRICE_INTERVAL = int(os.environ.get("PRICE_REFRESH_INTERVAL_S", 900))
ZONE_INTERVAL = int(os.environ.get("ZONE_POLL_INTERVAL_S", 60))
STOCK_ZONE_INTERVAL = 300
ZONE_BAND = 0.003          # entry ±0.3% counts as "in the zone"
FLIP_COOLDOWN = 6 * 3600
ZONE_COOLDOWN = 6 * 3600

MACRO_ENABLED = os.environ.get("MACRO_ALERTS_ENABLED", "1").strip() not in ("0", "false", "no")
MACRO_POLL_INTERVAL_S = int(os.environ.get("MACRO_POLL_INTERVAL_S", 60))
MACRO_LEADS = [int(x) for x in
               os.environ.get("MACRO_LEAD_MIN", "120,30,10").split(",")
               if x.strip()]
MACRO_POST_DELAY_S = int(os.environ.get("MACRO_POST_DELAY_S", 45))
MACRO_POST_GRACE_S = int(os.environ.get("MACRO_POST_EVENT_GRACE_S", 600))
MACRO_LEAD_COOLDOWN = 20 * 3600
MACRO_POST_COOLDOWN = 24 * 3600

MACRO_PC_ALARM_ENABLED = os.environ.get(
    "MACRO_PC_ALARM_ENABLED", "1").strip() not in ("0", "false", "no")
MACRO_PC_ALARM_LEADS = [int(x) for x in
                        os.environ.get("MACRO_PC_ALARM_LEAD_MIN",
                                       "15,5").split(",") if x.strip()]
MACRO_PC_ALARM_SOUND = os.environ.get(
    "MACRO_PC_ALARM_SOUND",
    "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga")
MACRO_PC_ALARM_SECONDS = int(os.environ.get("MACRO_PC_ALARM_SECONDS", 30))
MACRO_PC_ALARM_VOLUME = os.environ.get("MACRO_PC_ALARM_VOLUME", "65536")
MACRO_PC_ALARM_ICON = os.environ.get("MACRO_PC_ALARM_ICON", "dialog-warning")

STATE_FILE = os.path.join(DATA_DIR, "service_state.json")
run_event = threading.Event()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def get_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def set_state(**kw):
    st = get_state()
    st.update(kw)
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"  [service] cannot persist state: {e}")


# ── full pipeline ───────────────────────────────────────────

def run_script(script, timeout):
    env = dict(os.environ)
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(HERE, script)],
            cwd=DATA_DIR, env=env, timeout=timeout,
        )
        return p.returncode == 0, int(time.time() - t0)
    except subprocess.TimeoutExpired:
        return False, int(time.time() - t0)


def _prune_orphan_snapshots():
    """Remove snapshot files whose asset is no longer on the watchlist."""
    import build_snapshot as bs

    keep = set()
    for cfg in bs.WATCHLIST:
        safe = (cfg["asset"].replace("=", "_").replace("/", "_")
                            .replace("-", "_").replace(".", "_"))
        keep.add(f"snapshot_{safe}.json")
    for path in glob.glob(os.path.join(DATA_DIR, "snapshot_*.json")):
        base = os.path.basename(path)
        if "summary" in base or base in keep:
            continue
        try:
            os.remove(path)
            print(f"  [service] pruned stale {base}")
        except OSError:
            pass


def _actionable_summary():
    try:
        with open(os.path.join(DATA_DIR, "signals.json")) as f:
            sigs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return "signals.json not written"
    act = [f"{s.get('asset')} {s.get('action')} {s.get('composite_score'):+.1f}"
           for s in sigs if s.get("action") not in ("HOLD", "ERROR", "NO_DATA")]
    hold = sum(1 for s in sigs if s.get("action") == "HOLD")
    parts = ", ".join(act) if act else "nothing actionable"
    return f"{parts} · {hold} on HOLD · {len(sigs)} assets scanned"


def plan_trades():
    """Human-in-the-loop: turn fresh signals into pending proposals (never
    auto-executes). Best-effort — a failure here never affects the pipeline."""
    if os.environ.get("TRADING_MODE", "paper").lower() == "off":
        return
    try:
        import importlib
        import trader
        # The service is long-lived: re-read trader.py each cycle so gate
        # changes take effect without restarting (and re-running) the pipeline.
        importlib.reload(trader)
        res = trader.plan()
        if res.get("planned"):
            print(f"  [trader] {res['planned']} trade proposal(s) pending approval "
                  f"(mode {trader.mode()}, equity {res.get('equity')})")
        else:
            print(f"  [trader] no proposals ({res.get('reason', 'nothing actionable')})")
    except Exception as e:
        print(f"  [trader] planner skipped: {e}")


def do_full(manual):
    print(f"\n  [service] full pipeline start ({'manual' if manual else 'scheduled'})")
    set_state(last_full_run_start=_now())
    ok, dur = run_script("build_snapshot.py", timeout=3600)
    if ok:
        ok_brief, _ = run_script("generate brief.py", timeout=900)
        if not ok_brief:
            print("  [service] WARN — brief generation failed (non-fatal)")
    set_state(last_full_run=_now(), last_full_ok=ok,
              last_full_duration=dur,
              **({} if ok else {"last_error": "build_snapshot failed/timed out"}))
    if ok:
        _prune_orphan_snapshots()
        plan_trades()
    print(f"  [service] full pipeline {'OK' if ok else 'FAILED'} in {dur}s")
    if manual:
        alerts.send(
            f"PIPELINE {'OK' if ok else 'FAILED'} ({dur}s)\n"
            f"{_actionable_summary()}"
        )


# ── light price refresh ─────────────────────────────────────

def _atomic_write(fname, payload):
    tmp = os.path.join(DATA_DIR, fname + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, os.path.join(DATA_DIR, fname))


def regenerate_signals():
    """Rebuild snapshot_summary.json + signals.json from stored snapshots."""
    from signals import evaluate_snapshot

    summary, sigs = [], []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "snapshot_*.json"))):
        base = os.path.basename(path)
        if "summary" in base:
            continue
        try:
            with open(path) as f:
                s = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        sc = {k: v for k, v in s.items() if k != "price_data"}
        sc["candle_count"] = len(s.get("price_data") or [])
        sc["headline_count"] = len(s.get("news_headlines") or [])
        sc["news_headlines"] = (s.get("news_headlines") or [])[:5]
        try:
            sig = evaluate_snapshot(s)
        except Exception as e:
            sig = {
                "asset": s.get("asset"),
                "label": s.get("label", s.get("asset")),
                "action": "ERROR",
                "composite_score": None,
                "current_price": s.get("current_price"),
                "levels": {},
                "position_size_frac": None,
                "error": str(e),
            }
        sc["signal"] = sig
        sigs.append(sig)
        summary.append(sc)
    _atomic_write("snapshot_summary.json", summary)
    _atomic_write("signals.json", sigs)
    return sigs


def do_light():
    import build_snapshot as bs
    from fetch_price import fetch_binance_ohlcv, fetch_stock_ohlcv
    from signals import compute_freshness

    print(f"\n  [service] light price refresh ({_now()})")
    refreshed, failed = 0, []
    for cfg in bs.WATCHLIST:
        asset = cfg["asset"]
        safe = (asset.replace("=", "_").replace("/", "_")
                     .replace("-", "_").replace(".", "_"))
        fname = f"snapshot_{safe}.json"
        path = os.path.join(DATA_DIR, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                snap = json.load(f)
            if cfg["asset_type"] == "crypto":
                df = fetch_binance_ohlcv(
                    asset, interval=cfg["candle_interval"],
                    limit=cfg["candle_limit"])
            else:
                period = "200d" if cfg["candle_limit"] > 100 else "60d"
                df = fetch_stock_ohlcv(
                    asset, period=period, interval=cfg["candle_interval"])
            if df.empty:
                raise ValueError("empty frame")
            snap["price_data"] = df[["timestamps", "open", "high",
                                     "low", "close", "volume",
                                     "amount"]].to_dict(orient="records")
            snap["current_price"] = float(df["close"].iloc[-1])
            if cfg["candle_interval"] != "1d":
                try:
                    if cfg["asset_type"] == "crypto":
                        ddf = fetch_binance_ohlcv(asset, interval="1d", limit=120)
                    else:
                        ddf = fetch_stock_ohlcv(asset, period="1y", interval="1d")
                    snap["daily_closes"] = [float(x) for x in ddf["close"].tolist()]
                except Exception:
                    pass  # hourly run refreshes the RSI leg anyway
            snap.update(compute_freshness(
                snap["price_data"][-1].get("timestamps"),
                interval=cfg["candle_interval"] or "1h"))
            # Refresh the derivatives tape too: funding and open interest
            # move faster than the hourly full run, and the positioning
            # factor is now part of the composite.
            if cfg["asset_type"] == "crypto":
                try:
                    from fetch_price import fetch_binance_futures_context
                    deriv = fetch_binance_futures_context(asset)
                    if deriv:
                        deriv["basis_pct"] = (
                            (deriv["mark_price"] - deriv["index_price"])
                            / deriv["index_price"] * 100
                            if deriv.get("mark_price") and deriv.get("index_price")
                            else None)
                        deriv["open_interest_change_pct_24h"] = bs.update_oi_track(
                            asset, deriv.get("open_interest_usd"))
                        snap["derivatives"] = deriv
                except Exception as e:
                    print(f"  [light] derivatives refresh failed for {asset}: {e}")
            _atomic_write(fname, snap)
            refreshed += 1
            time.sleep(0.4)  # stay polite with yfinance
        except Exception as e:
            failed.append(f"{asset}: {e}")
    # Refresh Kronos against the fresh candles so the factor is not stale
    # between hourly full runs (best-effort; failure never blocks signals).
    if os.environ.get("KRONOS_ON_LIGHT", "1").lower() not in ("0", "false", "no"):
        bs.run_kronos_layer()
    sigs = regenerate_signals()
    _prune_orphan_snapshots()

    set_state(last_light_refresh=_now(),
              **({"last_error": failed[0]} if failed else {}))
    print(f"  [service] refreshed {refreshed}/{len(bs.WATCHLIST)} snapshots, "
          f"{len(sigs)} signals regenerated"
          + (f", failures: {'; '.join(failed[:3])}" if failed else ""))


# ── zone watcher ────────────────────────────────────────────

def us_market_open():
    t = datetime.now(timezone.utc)
    if t.weekday() >= 5:
        return False
    mins = t.hour * 60 + t.minute
    return 13 * 60 + 30 <= mins < 20 * 60


def _live_price(sig):
    asset = sig.get("asset", "")
    if sig.get("asset_type") == "crypto" or asset.endswith("USDT"):
        import requests
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": asset}, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    import yfinance as yf
    fi = yf.Ticker(asset).fast_info
    price = fi.get("last_price") or fi.get("lastPrice")
    if not price:
        hist = yf.Ticker(asset).history(period="1d", interval="1m")
        price = float(hist["Close"].iloc[-1])
    return float(price)


def _fmt(x, nd=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "n/a"
    return f"{x:,.{nd}f}" if abs(x) < 1000 else f"{x:,.2f}"


def _zone_message(sig, price):
    lv = sig.get("levels") or {}
    entry = lv.get("entry")
    bias = lv.get("bias")
    size = sig.get("position_size_frac")
    window = ("pullback to entry — limit-buy window" if bias == "LONG"
              else "rally to entry — limit-sell window" if bias == "SHORT"
              else "price at entry level")
    return "\n".join([
        f"ENTRY ZONE — {sig.get('label')} ({sig.get('asset')})",
        f"action {sig.get('action')} ({sig.get('composite_score'):+.1f}) · "
        f"price {_fmt(price)}",
        f"entry {_fmt(entry)} (±{ZONE_BAND * 100:.1f}%) — {window}",
        f"SL {_fmt(lv.get('stop_loss'))} · TP1 {_fmt(lv.get('take_profit_1'))} · "
        + (f"size {size * 100:.0f}% acct" if size else "size n/a"),
        "signal age: " + ("STALE" if sig.get("is_stale") else "fresh"),
    ])


def _nearest_lead(lead_s, leads_min, tol_s=0):
    """Pick the tightest lead window that still covers lead_s (or None).

    Prevents firing 120/30/10 all at once when the service starts close to an
    event; each alert fires once as its window is entered.
    """
    if lead_s <= 0:
        return None
    windows = [m for m in leads_min if m * 60 + tol_s >= lead_s]
    return min(windows, key=lambda m: m * 60) if windows else None


def _pc_alarm(title, body):
    """Fire a loud local alarm: critical popup + repeating tone. Non-blocking."""
    if not MACRO_PC_ALARM_ENABLED:
        return
    threading.Thread(target=_pc_alarm_worker, args=(title, body),
                     daemon=True).start()


def _pc_alarm_worker(title, body):
    notif = shutil.which("notify-send")
    if notif:
        try:
            subprocess.run(
                [notif, "-u", "critical", "-a", "Jafar",
                 "-i", MACRO_PC_ALARM_ICON, "-t", "0", title, body],
                timeout=10, check=False)
        except Exception as e:
            print(f"  [macro] notify-send failed: {type(e).__name__}: {e}")

    player = shutil.which("paplay")
    if not player or not os.path.exists(MACRO_PC_ALARM_SOUND):
        return
    deadline = time.time() + MACRO_PC_ALARM_SECONDS
    while time.time() < deadline:
        try:
            subprocess.run(
                [player, f"--volume={MACRO_PC_ALARM_VOLUME}",
                 MACRO_PC_ALARM_SOUND],
                timeout=MACRO_PC_ALARM_SECONDS + 10, check=False)
        except Exception as e:
            print(f"  [macro] paplay failed: {type(e).__name__}: {e}")
            return


def macro_loop():
    """Alarm clock for scheduled macro releases.

    Pre-event: warn at MACRO_LEAD_MIN minutes out so entries are not chased
    into a print. Post-event: wait MACRO_POST_DELAY_S (let the number and the
    first headlines land), then wake the full pipeline so the print is
    ingested immediately instead of on the next hourly tick.
    """
    if not MACRO_ENABLED:
        print("  [macro] alarms disabled (MACRO_ALERTS_ENABLED=0)")
        return

    import macro_calendar

    max_lead = (max(MACRO_LEADS) if MACRO_LEADS else 0) * 60
    horizon = max_lead + MACRO_POST_GRACE_S + 3600

    while True:
        time.sleep(MACRO_POLL_INTERVAL_S)
        try:
            now = time.time()
            events = macro_calendar.window_events(
                lookback_s=MACRO_POST_DELAY_S + MACRO_POST_GRACE_S,
                horizon_s=horizon)

            for ev in events:
                lead_s = ev["ts_utc"] - now

                lead_min = _nearest_lead(lead_s, MACRO_LEADS,
                                         MACRO_POLL_INTERVAL_S)
                if lead_min and alerts.throttled(
                        f"macro:pre:{ev['id']}:{lead_min}",
                        MACRO_LEAD_COOLDOWN):
                    alerts.send(macro_calendar.pre_message(ev, lead_s))

                if -MACRO_POST_GRACE_S <= lead_s <= -MACRO_POST_DELAY_S and \
                        alerts.throttled(f"macro:post:{ev['id']}",
                                         MACRO_POST_COOLDOWN):
                    alerts.send(macro_calendar.post_message(ev))
                    run_event.set()

                pc_min = _nearest_lead(lead_s, MACRO_PC_ALARM_LEADS,
                                       MACRO_POLL_INTERVAL_S)
                if pc_min and alerts.throttled(
                        f"macro:pc:{ev['id']}:{pc_min}", MACRO_LEAD_COOLDOWN):
                    _pc_alarm(
                        f"⏰ {ev['short']} in "
                        f"{macro_calendar._lead_label(lead_s)}",
                        f"{ev['name']}\n{macro_calendar._stamp(ev)}\n"
                        "Jafar macro alarm — no new entries around the print.")

                if -MACRO_POST_GRACE_S <= lead_s <= 0 and \
                        alerts.throttled(f"macro:pc-print:{ev['id']}",
                                         MACRO_POST_COOLDOWN):
                    _pc_alarm(
                        f"📊 {ev['short']} PRINTING NOW",
                        f"{ev['name']}\n{macro_calendar._stamp(ev)}\n"
                        "Volatility expected — ingest & reassess.")
        except Exception as e:
            print(f"  [macro] loop error: {type(e).__name__}: {e}")


def zone_loop():
    """Poll actionable signals; alert on entry-zone crossings + action flips."""
    import requests

    actions_path = os.path.join(DATA_DIR, "last_actions.json")
    last_actions = {}
    try:
        with open(actions_path) as f:
            last_actions = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    prev_price = {}       # asset -> last sampled price
    stock_fetched_at = 0.0

    while True:
        time.sleep(ZONE_INTERVAL)
        try:
            try:
                with open(os.path.join(DATA_DIR, "signals.json")) as f:
                    sigs = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                continue

            # action flips (covers full + light refreshes)
            for s in sigs:
                asset, action = s.get("asset"), s.get("action")
                old = last_actions.get(asset)
                if action and old and action != old and \
                        action not in ("HOLD", "ERROR", "NO_DATA") and \
                        alerts.throttled(f"flip:{asset}", FLIP_COOLDOWN):
                    score = s.get("composite_score")
                    score_s = f"{score:+.1f}" if score is not None else "?"
                    alerts.send(
                        f"ACTION FLIP — {s.get('label')} ({asset})\n"
                        f"{old} → {action} (score {score_s}, "
                        f"price {_fmt(s.get('current_price'))})"
                    )
                if action:
                    last_actions[asset] = action
            tmp = actions_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(last_actions, f, indent=2)
            os.replace(tmp, actions_path)

            # crypto: fetch every tick; stocks: every STOCK_ZONE_INTERVAL and
            # only during US regular hours (closed market = static close,
            # polling it buys nothing).
            now = time.time()
            stocks_fresh = now - stock_fetched_at >= STOCK_ZONE_INTERVAL
            if stocks_fresh:
                stock_fetched_at = now
            market_open = us_market_open()

            for s in sigs:
                action = s.get("action")
                lv = s.get("levels") or {}
                entry = lv.get("entry")
                if not action or action in ("HOLD", "ERROR", "NO_DATA") \
                        or entry is None:
                    continue
                is_crypto = (s.get("asset_type") == "crypto"
                             or str(s.get("asset", "")).endswith("USDT"))
                if not is_crypto and not (market_open and stocks_fresh):
                    continue
                try:
                    price = _live_price(s)
                except Exception:
                    continue
                prev = prev_price.get(s.get("asset"))
                prev_price[s.get("asset")] = price

                lo, hi = entry * (1 - ZONE_BAND), entry * (1 + ZONE_BAND)
                inside = lo <= price <= hi
                was_inside = (prev is not None and lo <= prev <= hi)
                if inside and not was_inside and \
                        alerts.throttled(f"zone:{s.get('asset')}", ZONE_COOLDOWN):
                    alerts.send(_zone_message(s, price))
        except Exception as e:
            print(f"  [zone] loop error: {e}")


# ── dashboard server ────────────────────────────────────────

_dashboard_proc = None
_dashboard_reported = False


def _port_in_use(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _ensure_dashboard():
    """Run dashboard.py alongside the service (spawned, auto-respawned)."""
    global _dashboard_proc, _dashboard_reported
    if _dashboard_proc is not None and _dashboard_proc.poll() is None:
        return
    port = int(os.environ.get("PORT", "5000"))
    if _port_in_use(port):
        if not _dashboard_reported:
            print(f"  [service] something already listens on :{port} — "
                  f"not spawning the dashboard")
            _dashboard_reported = True
        return
    _dashboard_reported = False
    env = dict(os.environ)
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    try:
        _dashboard_proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "dashboard.py")],
            cwd=DATA_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"  [service] dashboard spawned on PORT={port} (pid "
              f"{_dashboard_proc.pid})")
    except OSError as e:
        print(f"  [service] dashboard spawn failed: {e}")


# ── main loop ───────────────────────────────────────────────

def main():
    import telegram_bot

    print("=" * 60)
    print("  JAFAR INTELLIGENCE — service conductor")
    print(f"  data dir      : {DATA_DIR}")
    print(f"  full pipeline : every {FULL_INTERVAL}s")
    print(f"  price refresh : every {PRICE_INTERVAL}s")
    print(f"  zone poll     : every {ZONE_INTERVAL}s")
    if MACRO_ENABLED:
        print(f"  macro alarms  : every {MACRO_POLL_INTERVAL_S}s — "
              f"Telegram leads {MACRO_LEADS} min, post-run +{MACRO_POST_DELAY_S}s")
        pc = ("on — " + ",".join(str(x) for x in MACRO_PC_ALARM_LEADS) +
              "min + print, " + f"{MACRO_PC_ALARM_SECONDS}s tone"
              if MACRO_PC_ALARM_ENABLED else "off")
        print(f"  pc alarm      : {pc}")
    else:
        print("  macro alarms  : disabled")
    print(f"  telegram      : "
          f"{'bot on' if alerts.token() else 'no token (disabled)'}")
    print("=" * 60)

    set_state(started_at=_now())

    threading.Thread(target=zone_loop, daemon=True).start()
    threading.Thread(target=macro_loop, daemon=True).start()
    threading.Thread(target=telegram_bot.run,
                     args=(run_event.set, get_state),
                     daemon=True).start()

    next_full = 0.0                    # boot: run immediately
    next_light = time.time() + PRICE_INTERVAL

    while True:
        now = time.time()
        _ensure_dashboard()
        try:
            if run_event.is_set():
                run_event.clear()
                do_full(manual=True)
                next_full = time.time() + FULL_INTERVAL
                next_light = time.time() + PRICE_INTERVAL
            elif now >= next_full:
                do_full(manual=False)
                next_full = time.time() + FULL_INTERVAL
                next_light = time.time() + PRICE_INTERVAL
            elif now >= next_light:
                do_light()
                next_light = time.time() + PRICE_INTERVAL
        except Exception as e:
            set_state(last_error=str(e))
            print(f"  [service] loop error: {e}")
            time.sleep(10)

        sleep_for = max(1, min(next_full, next_light) - time.time())
        run_event.wait(timeout=min(sleep_for, 30))


if __name__ == "__main__":
    main()

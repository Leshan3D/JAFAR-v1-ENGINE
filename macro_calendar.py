"""
MACRO CALENDAR — scheduled US macro releases (CPI, NFP, PPI, PCE, GDP, FOMC).

Read-only schedule source so the alarm layer can warn BEFORE a print instead of
reacting after it. Dates come from FRED's release calendar (authoritative,
same key the macro module already uses); FOMC is bundled because FRED's FOMC
"Press Release" release id is a daily artifact rather than a meeting list.

Nothing here touches signals, forecasts, or Kronos — it is a standalone
schedule feed consumed by run_service.macro_loop() and telegram_bot /calendar.
"""

import os
import json
import time
import requests
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("DATA_DIR") or os.getcwd()
CACHE_FILE = "macro_calendar.json"
MANUAL_FILE = "macro_calendar_manual.json"

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
FRED_RELEASE_DATES = "https://api.stlouisfed.org/fred/release/dates"

FRED_RELEASES = {
    10: {"short": "CPI", "name": "CPI (Consumer Price Index)",
         "importance": "high", "time_et": "08:30",
         "affects": "gold, USD, equities"},
    50: {"short": "NFP", "name": "Nonfarm Payrolls (Employment Situation)",
         "importance": "high", "time_et": "08:30",
         "affects": "equities, USD, gold"},
    54: {"short": "PCE", "name": "Core PCE (Personal Income & Outlays)",
         "importance": "high", "time_et": "08:30",
         "affects": "gold, Fed rate path"},
    46: {"short": "PPI", "name": "PPI (Producer Price Index)",
         "importance": "medium", "time_et": "08:30",
         "affects": "gold, USD"},
    53: {"short": "GDP", "name": "GDP (advance)",
         "importance": "medium", "time_et": "08:30",
         "affects": "equities, USD"},
    9: {"short": "RETAIL", "name": "Retail Sales",
        "importance": "medium", "time_et": "08:30",
        "affects": "equities, USD"},
}

# Fed statement lands on day 2 of each 2026 meeting at 14:00 ET.
# Source: federalreserve.gov/monetarypolicy/fomccalendars.htm (announced a
# year ahead, so this only needs editing once a year).
FOMC_META = {"short": "FOMC", "importance": "high", "time_et": "14:00",
             "affects": "gold, USD, all risk",
             "name": "FOMC decision + press conference"}
FOMC_STATEMENT_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

REFRESH_S = int(os.environ.get("MACRO_REFRESH_S", 12 * 3600))
HORIZON_DAYS = int(os.environ.get("MACRO_CALENDAR_HORIZON_DAYS", 180))
# Refresh early if the calendar runs this close to running dry.
RUNWAY_S = 7 * 86400
REQUEST_TIMEOUT_S = 25


def _now():
    return time.time()


def _read(fname):
    try:
        with open(os.path.join(DATA_DIR, fname)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write(fname, payload):
    path = os.path.join(DATA_DIR, fname)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, path)
    except OSError as e:
        print(f"  [macro] WARN — cannot persist {fname}: {e}")


def _et_ts(day, time_et):
    h, m = (int(x) for x in time_et.split(":"))
    wall = datetime.strptime(day, "%Y-%m-%d").replace(hour=h, minute=m,
                                                       tzinfo=NY)
    return wall.astimezone(UTC).timestamp()


def _event(short, name, day, time_et, importance, affects, source):
    return {
        "id": f"{short.lower()}-{day}",
        "short": short,
        "name": name,
        "date": day,
        "time_et": time_et,
        "ts_utc": _et_ts(day, time_et),
        "importance": importance,
        "affects": affects,
        "source": source,
    }


def _fred_dates(rid, start, end):
    if not FRED_API_KEY:
        return []
    try:
        r = requests.get(FRED_RELEASE_DATES, params={
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "release_id": rid,
            "realtime_start": start.isoformat(),
            "realtime_end": end.isoformat(),
            "include_release_dates_with_no_data": "true",
            "limit": 1000,
        }, timeout=REQUEST_TIMEOUT_S)
        if r.status_code != 200:
            print(f"  [macro] release {rid} returned {r.status_code}: "
                  f"{r.text[:160]}")
            return []
        return [x.get("date") for x in r.json().get("release_dates", [])
                if x.get("date")]
    except requests.RequestException as e:
        print(f"  [macro] FRED release {rid} failed: {type(e).__name__}: {e}")
        return []


def _bundled_events(today):
    out = []
    for day in FOMC_STATEMENT_2026:
        if day >= (today - timedelta(days=1)).isoformat():
            out.append(_event(FOMC_META["short"], FOMC_META["name"], day,
                              FOMC_META["time_et"], FOMC_META["importance"],
                              FOMC_META["affects"], "bundled"))
    return out


def _build():
    today = date.today()
    end = today + timedelta(days=HORIZON_DAYS)
    events = []
    fred_ok = False
    for rid, meta in FRED_RELEASES.items():
        days = _fred_dates(rid, today, end)
        if days:
            fred_ok = True
        for day in days:
            if day >= today.isoformat():
                events.append(_event(meta["short"], meta["name"], day,
                                     meta["time_et"], meta["importance"],
                                     meta["affects"], "fred"))
    events += _bundled_events(today)
    return events, fred_ok


def _merge(events):
    manual = _read(MANUAL_FILE)
    if isinstance(manual, list):
        by_id = {e["id"]: e for e in events}
        for m in manual:
            if not isinstance(m, dict) or not m.get("name") or not m.get("date"):
                continue
            time_et = m.get("time_et", "08:30")
            short = str(m.get("short") or m["name"])[:8].upper()
            ev = _event(short, m["name"], str(m["date"]), time_et,
                        m.get("importance", "medium"),
                        m.get("affects", "—"), "manual")
            by_id[ev["id"]] = ev
        events = list(by_id.values())
    return sorted(events, key=lambda e: e["ts_utc"])


def _runway_ok(events):
    if not events:
        return False
    return max(e["ts_utc"] for e in events) - _now() > RUNWAY_S


def load(refresh=None):
    """Return the full cached calendar, refreshing only when stale."""
    cache = _read(CACHE_FILE)
    fresh = (cache or {}).get("events")
    age_ok = cache and (_now() - float(cache.get("generated", 0)) < REFRESH_S)
    if refresh is None:
        refresh = not (age_ok and _runway_ok(fresh))
    if not refresh:
        return _merge(fresh)

    built, fred_ok = _build()
    if fred_ok:
        _write(CACHE_FILE, {"generated": _now(), "events": built})
        return _merge(built)
    if fresh:
        print("  [macro] FRED unreachable — using cached calendar")
        return _merge(fresh)
    print("  [macro] no calendar available (FRED down, no cache)")
    return _merge(built)


def window_events(lookback_s=0.0, horizon_s=45 * 86400, refresh=None):
    now = _now()
    evs = [e for e in load(refresh=refresh)
           if now - lookback_s <= e["ts_utc"] <= now + horizon_s]
    return sorted(evs, key=lambda e: e["ts_utc"])


def upcoming(n=10, horizon_s=365 * 86400, refresh=None):
    return window_events(horizon_s=horizon_s, refresh=refresh)[:n]


def next_event(horizon_s=45 * 86400, refresh=None):
    evs = window_events(horizon_s=horizon_s, refresh=refresh)
    return evs[0] if evs else None


def _lead_label(seconds):
    seconds = max(1, int(seconds))
    if seconds >= 3600:
        h = round(seconds / 3600)
        return f"{h} h"
    return f"{max(1, round(seconds / 60))} min"


def _stamp(ev):
    dt = datetime.fromtimestamp(ev["ts_utc"], UTC)
    return (f"{ev['date']} {ev['time_et']} ET / "
            f"{dt.strftime('%a %d %b %H:%M')} UTC")


def pre_message(ev, lead_s):
    return "\n".join([
        f"⏰ MACRO ALERT — {ev['short']} in {_lead_label(lead_s)}",
        ev["name"],
        f"release: {_stamp(ev)}",
        f"impact: {ev['affects']} · importance {ev['importance'].upper()}",
        "prep: no new entries in the 15 min around the print — expect a "
        "spike both ways. Existing stops stay where they are.",
    ])


def post_message(ev):
    return "\n".join([
        f"📊 {ev['short']} PRINTED — {ev['name']}",
        f"released {_stamp(ev)} · source {ev['source']}",
        "kicking a full pipeline run now to ingest the print; board, news and "
        "signals refresh in ~2-5 min.",
    ])


def format_list(evs=None, n=10):
    if evs is None:
        evs = upcoming(n=n)
    if not evs:
        return "MACRO CALENDAR — no scheduled releases in view."
    lines = ["MACRO CALENDAR (next "
             f"{len(evs)} · times UTC / ET)"]
    for e in evs:
        dt = datetime.fromtimestamp(e["ts_utc"], UTC)
        lines.append(
            f"  {dt.strftime('%a %m-%d %H:%M')}Z  {e['short']:<7} "
            f"{e['importance'].upper():<6} {e['name']}  ({e['time_et']} ET)"
        )
    return "\n".join(lines)

#!/usr/bin/env python3
"""
MSCI November 2026 Index Review watcher + post-decision BTC scan.

MSCI publishes the November review lists shortly after 23:00 CET
(= 22:00 UTC) on 2026-11-11. This script:

  1. sleeps until 22:00 UTC (or polls immediately with --now)
  2. polls the deterministic MSCI document URLs until the list appears
  3. downloads + de-texts the standard/small/micro public lists and the
     press release, then greps them for digital-asset-treasury names
  4. waits for the news tape to catch up (--settle seconds)
  5. runs a BTC-only JAFAR scan, saving signals + forecasts to the run dir
  6. restores the full 11-asset book and restarts trading-pipeline
  7. writes RESULT.md and pushes a Telegram summary

Everything is written under $ENGINE/msci_watch/<run-stamp>/ — nothing touches
the OS drive.

Usage
  python3 msci_watch.py                       # wait for 22:00Z, then poll
  python3 msci_watch.py --now                 # poll right away (detection test)
  python3 msci_watch.py --now --dry-run       # detect only, no engine run
  python3 msci_watch.py --at 2026-11-11T22:00:00Z --until 2026-11-12T04:00:00Z
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import fcntl
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ENGINE = os.path.dirname(os.path.abspath(__file__))
OUTROOT = os.path.join(ENGINE, "msci_watch")
LOCK = os.path.join(OUTROOT, ".lock")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

BASE = "https://app2.msci.com"
DOCS = [
    ("standard list",   f"{BASE}/eqb/gimi/stdindex/MSCI_Nov26_STPublicList.pdf"),
    ("small cap list",  f"{BASE}/eqb/gimi/smallcap/MSCI_Nov26_SCPublicList.pdf"),
    ("micro cap list",  f"{BASE}/eqb/gimi/stdindex/MSCI_Nov26_MicroPublicList.pdf"),
    ("press release",   f"{BASE}/eqb/pressreleases/archive/MSCI_Nov26_QIRPR.pdf"),
]
REVIEW_PAGE = f"{BASE}/eqb/gimi/stdindex/index_review.html"
CONSULT_PAGE = "https://www.msci.com/index-consultations"

# Names on the digital-asset-treasury watchlist, plus the obvious control names.
WATCH = ["STRATEGY", "METAPLANET", "YELLOW CAKE", "SHARPLINK", "SKEL",
         "UPEXI", "TON STRATEGY", "DEFI DEVELOPMENT"]
CONTROL = ["MSTR", "BITCOIN", "MICROSTRATEGY"]

POLL_EVERY = 120          # seconds between probes
DEFAULT_SETTLE = 900      # let headlines reach the news feeds before scanning


# ── plumbing ─────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(OUTROOT, "watch.log"), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def now():
    return datetime.now(timezone.utc)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def http_get(url, timeout=45, retries=3):
    """Return (status, bytes) — status 0 on network failure."""
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA,
                                        "Accept": "*/*"})
            with urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except HTTPError as e:
            return e.code, b""
        except (URLError, TimeoutError, OSError) as e:
            if attempt == retries - 1:
                return 0, str(e).encode()
            time.sleep(5 * (attempt + 1))
    return 0, b""


def installed(url):
    """True when the document exists and is a real PDF (not a 404 page)."""
    status, body = http_get(url, timeout=40, retries=2)
    if status != 200:
        return False, status
    if body[:4] != b"%PDF":
        return False, status
    return True, status


def discover_docs():
    """Parse the review page for the actual Nov-2026 document hrefs.

    The filenames are stable (MSCI_Nov26_*) but the paths have moved before,
    so the live page wins when it offers something better than the guesses.
    """
    status, body = http_get(REVIEW_PAGE, timeout=40)
    if status != 200:
        return []
    html = body.decode("utf-8", "replace")
    hrefs = re.findall(r'HREF="([^"]+)"', html, re.I) + \
        re.findall(r'href="([^"]+)"', html)
    seen, out = set(), []
    for h in hrefs:
        if "Nov26" not in h and "Nov_2026" not in h:
            continue
        url = h if h.startswith("http") else f"{BASE}/{h.lstrip('/')}"
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out[:12]


def pdf_text(path):
    try:
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, timeout=120)
        return out.stdout.decode("utf-8", "replace")
    except Exception as e:
        log(f"  pdftotext failed on {os.path.basename(path)}: {e}")
        return ""


def grep_names(text, names):
    hits = {}
    up = text.upper()
    for n in names:
        c = up.count(n)
        if c:
            hits[n] = c
    return hits


def context_lines(text, names, width=90):
    out = []
    for i, line in enumerate(text.splitlines()):
        up = line.upper()
        if any(n in up for n in names):
            out.append(f"{i+1:>6}: {line.strip()[:width]}")
    return out


# ── detection ────────────────────────────────────────────────────────────
def detect():
    """Returns (bool, evidence) — True once MSCI's Nov 2026 lists are live."""
    ev = {}
    ok, st = installed(DOCS[0][1])
    ev["standard_list"] = f"{st} ({'PDF' if ok else 'absent'})"
    if ok:
        return True, ev
    for label, url in DOCS[1:]:
        pass
    status, body = http_get(REVIEW_PAGE, timeout=40)
    html = body.decode("utf-8", "replace") if status == 200 else ""
    ev["review_page"] = status
    if re.search(r"November\s+2026\s+Index\s+Review", html, re.I):
        ev["review_page"] = f"{status} — November 2026 section present"
        return True, ev
    # direct href for the Nov26 files anywhere on the page
    if re.search(r"MSCI_Nov26_[A-Za-z]*PublicList", html):
        ev["review_page"] = f"{status} — Nov26 list link present"
        return True, ev
    return False, ev


# ── engine run ───────────────────────────────────────────────────────────
def load_dotenv():
    env = dict(os.environ)
    path = os.path.join(ENGINE, ".env")
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            k = k.strip()
            env.setdefault(k, v)
            # alerts.py reads os.environ directly, so the .env values must be
            # exported too or a systemd-spawned run sends nothing.
            os.environ.setdefault(k, v)
    return env


def sc(*args, check=False):
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, check=check)


def service_active():
    return sc("is-active", "trading-pipeline").stdout.strip() == "active"


def run_btc_scan(rundir, env):
    sig = os.path.join(ENGINE, "signals.json")
    fc = os.path.join(ENGINE, "forecasts.json")
    tmpdir = os.path.join(tempfile.gettempdir(), "jafar-msci")
    bk_sig = os.path.join(tmpdir, "msci_signals_full_backup.json")
    bk_fc = os.path.join(tmpdir, "msci_forecasts_full_backup.json")
    os.makedirs(tmpdir, exist_ok=True)

    log("stopping trading-pipeline for the focused run")
    sc("stop", "trading-pipeline")
    for _ in range(30):
        if not service_active():
            break
        time.sleep(2)

    for src, dst in ((sig, bk_sig), (fc, bk_fc)):
        if os.path.exists(src):
            shutil.copy2(src, dst)
    log("backed up the full book")

    run_env = dict(env)
    run_env["JAFAR_ONLY"] = "BTCUSDT"
    run_env["TZ"] = os.environ.get("TZ", "UTC")
    log("running JAFAR_ONLY=BTCUSDT build_snapshot.py")
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-u", "build_snapshot.py"],
                          cwd=ENGINE, env=run_env,
                          capture_output=True, text=True)
    elapsed = time.time() - t0
    with open(os.path.join(rundir, "btc_scan.log"), "w") as f:
        f.write(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    log(f"scan exit={proc.returncode} in {elapsed:.0f}s")

    ok = proc.returncode == 0 and os.path.exists(sig)
    if ok:
        shutil.copy2(sig, os.path.join(rundir, "signals_BTC_postmsci.json"))
        if os.path.exists(fc):
            shutil.copy2(fc, os.path.join(rundir, "forecasts_BTC_postmsci.json"))
        log("captured BTC signals.json")

    if os.path.exists(bk_sig):
        shutil.copy2(bk_sig, sig)
    if os.path.exists(bk_fc):
        shutil.copy2(bk_fc, fc)
    log("restored the full 11-asset book")

    sc("start", "trading-pipeline")
    for _ in range(30):
        if service_active():
            break
        time.sleep(2)
    log(f"trading-pipeline: {sc('is-active','trading-pipeline').stdout.strip()}")
    return ok


def summarise_signal(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    if not data:
        return None
    x = data[0]
    lv = x.get("levels") or {}
    fac = {f["factor"]: f["score"] for f in x.get("factors", [])}
    return {
        "label": x.get("label"),
        "action": x.get("action"),
        "score": x.get("composite_score"),
        "price": x.get("current_price"),
        "as_of": x.get("data_as_of"),
        "stale": x.get("is_stale"),
        "factors": fac,
        "levels": lv,
        "kronos": x.get("kronos_gate"),
        "size": x.get("position_size_frac"),
    }


# ── main ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="2026-11-11T22:00:00Z")
    ap.add_argument("--until", default="2026-11-12T04:00:00Z")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--settle", type=int, default=DEFAULT_SETTLE)
    ap.add_argument("--once", action="store_true",
                    help="probe once and exit (smoke test)")
    args = ap.parse_args()

    os.makedirs(OUTROOT, exist_ok=True)

    stamp = now().strftime("%Y%m%d_%H%M%SZ")
    rundir = os.path.join(OUTROOT, stamp)
    os.makedirs(rundir, exist_ok=True)

    done_marker = os.path.join(OUTROOT, ".done")
    if os.path.exists(done_marker) and not args.once and not args.now:
        with open(done_marker) as f:
            prev = f.read().strip()
        log(f"already completed: {prev} — exiting")
        return 0

    log(f"watcher start | run dir {rundir}")

    start_at = now() if (args.now or args.once) else parse_ts(args.at)
    deadline = parse_ts(args.until)
    if start_at > now():
        secs = (start_at - now()).total_seconds()
        log(f"sleeping {secs/3600:.2f} h until {start_at:%Y-%m-%d %H:%M:%SZ}")
        time.sleep(secs)

    detected = False
    evidence = {}
    while now() < deadline:
        detected, evidence = detect()
        log(f"probe: {evidence}")
        if detected or args.once:
            break
        nxt = min(POLL_EVERY, max(1, (deadline - now()).total_seconds()))
        time.sleep(nxt)

    if not detected:
        msg = ("MSCI watcher: not published by deadline "
               f"{deadline:%Y-%m-%d %H:%M:%SZ}. Last probe: {evidence}")
        log(msg)
        with open(os.path.join(rundir, "RESULT.md"), "w") as f:
            f.write("# MSCI November 2026 review — NOT DETECTED\n\n"
                    f"Deadline: {deadline:%Y-%m-%d %H:%M:%SZ}\n\n"
                    f"Last probe: `{evidence}`\n")
        if args.once:
            return 0
        try:
            from alerts import send as tg_send
            tg_send(msg)
        except Exception as e:
            log(f"telegram failed: {e}")
        return 2

    log("DETECTED — downloading MSCI documents")

    # Lock only around the work: both the in-session process and the systemd
    # backup timer may poll, but only one of them may stop the service and run
    # the scan. The loser waits, then finds the sentinel and exits.
    lockfh = open(LOCK, "w")
    fcntl.flock(lockfh, fcntl.LOCK_EX)
    if os.path.exists(done_marker):
        with open(done_marker) as f:
            prev = f.read().strip()
        log(f"another instance already completed: {prev} — exiting")
        return 0

    targets = list(DOCS)
    for url in discover_docs():
        if not any(u == url for _, u in targets):
            targets.append((os.path.basename(url), url))
    log(f"  {len(targets)} candidate documents")

    docs = {}
    for label, url in targets:
        ok, st = installed(url)
        if not ok:
            log(f"  {label}: absent ({st})")
            continue
        _, body = http_get(url, timeout=90, retries=2)
        path = os.path.join(rundir, os.path.basename(url))
        with open(path, "wb") as f:
            f.write(body)
        text = pdf_text(path) if path.endswith(".pdf") else body.decode("utf-8", "replace")
        sqlite_path = os.path.join(rundir, os.path.basename(url) + ".txt")
        with open(sqlite_path, "w") as f:
            f.write(text)
        docs[label] = text
        log(f"  {label}: {len(body)/1024:.0f} KB, {len(text)} chars text")

    # consultations page can carry the explicit non-operating list
    st, body = http_get(CONSULT_PAGE, timeout=45)
    if st == 200:
        with open(os.path.join(rundir, "index_consultations.html"), "wb") as f:
            f.write(body)
        docs["consultations page"] = body.decode("utf-8", "replace")

    hits, ctx = {}, {}
    for label, text in docs.items():
        h = grep_names(text, WATCH + CONTROL)
        if h:
            hits[label] = h
            lines = context_lines(text, WATCH, width=110)
            if lines:
                ctx[label] = lines[:200]
    log(f"name hits: {hits}")

    found = sorted({n for h in hits.values() for n in h if n in WATCH})
    log(f"watchlist names present in the list: {found or 'NONE'}")

    verdict = "REMOVED" if found else "NOT-ON-LIST"
    lines = [
        "# MSCI November 2026 Index Review — result",
        "",
        f"- Detected: {now():%Y-%m-%d %H:%M:%SZ}",
        f"- Verdict: **{verdict}**",
        f"- Watchlist names found: {', '.join(found) if found else 'none'}",
        f"- Documents downloaded: {len(docs)}",
        "",
        "## Hits per document",
        "```",
        json.dumps(hits, indent=2),
        "```",
    ]
    if ctx:
        lines += ["", "## Context lines"]
        for label, ls in ctx.items():
            lines += ["", f"### {label}", "```", *ls, "```"]
    with open(os.path.join(rundir, "RESULT.md"), "w") as f:
        f.write("\n".join(lines) + "\n")

    if args.dry_run:
        log("dry run — skipping the engine scan")
        return 0

    log(f"settling {args.settle}s for headlines to reach the news feeds")
    time.sleep(args.settle)

    env = load_dotenv()
    ok = run_btc_scan(rundir, env)

    sig = summarise_signal(os.path.join(rundir, "signals_BTC_postmsci.json"))
    if sig:
        fac = "  ".join(f"{k} {v:+.1f}" for k, v in sig["factors"].items())
        lv = sig["levels"] or {}
        tg = (f"MSCI Nov-2026 review: {verdict}\n"
              f"Found: {', '.join(found) if found else 'none of ' + str(len(WATCH)) + ' watchlist names'}\n\n"
              f"BTC post-decision: {sig['action']} {sig['score']:+.1f} @ {sig['price']}\n"
              f"  {fac}\n"
              f"  entry {lv.get('entry')} SL {lv.get('stop_loss')} "
              f"TP1 {lv.get('take_profit_1')}\n"
              f"  as_of {sig['as_of']} stale={sig['stale']}\n"
              f"  dir {rundir}")
    else:
        tg = f"MSCI Nov-2026 review: {verdict}. BTC scan {'ok' if ok else 'FAILED'}. {rundir}"
    try:
        from alerts import send as tg_send
        tg_send(tg)
        log("telegram sent")
    except Exception as e:
        log(f"telegram failed: {e}")

    with open(done_marker, "w") as f:
        f.write(rundir)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

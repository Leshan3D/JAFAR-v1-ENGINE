"""
JAFAR INTELLIGENCE — Live Dashboard Server
Flask backend serving real-time data from snapshot JSON files.
Auto-refreshes every 5 minutes via JavaScript polling.

Run: python dashboard_server.py
Then open: http://localhost:5000
"""

from flask import Flask, jsonify, render_template_string
import json
import os
import glob
from datetime import datetime, timezone

app = Flask(__name__)

# DATA_DIR lets the same code run locally (files next to the script) and in
# the container (snapshots live on the mounted volume).
SNAPSHOT_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))

ASSET_META = {
    "BTCUSDT":   {"label": "Bitcoin",       "ticker": "BTC",   "color": "#F7931A", "icon": "₿"},
    "ETHUSDT":   {"label": "Ethereum",      "ticker": "ETH",   "color": "#627EEA", "icon": "Ξ"},
    "SOLUSDT":   {"label": "Solana",        "ticker": "SOL",   "color": "#9945FF", "icon": "◎"},
    "GC_F":      {"label": "Gold Futures",  "ticker": "GC=F",  "color": "#C9A84C", "icon": "Au"},
    "ZECUSDT":   {"label": "Zcash",         "ticker": "ZEC",   "color": "#F4B31D", "icon": "Z"},
    "BZ_F":      {"label": "Brent Crude Oil", "ticker": "BZ=F",  "color": "#6B6B6B", "icon": "🛢"},
    "NVDA":      {"label": "NVIDIA",        "ticker": "NVDA",  "color": "#76B900", "icon": "N"},
    "META":      {"label": "Meta Platforms","ticker": "META",  "color": "#0081FB", "icon": "M"},
    "AMD":       {"label": "AMD",           "ticker": "AMD",   "color": "#ED1C24", "icon": "A"},
    "DX_Y_NYB":  {"label": "US Dollar Index","ticker": "DXY",  "color": "#1A6BB5", "icon": "$"},
}


def load_snapshot(asset_key):
    fname = os.path.join(SNAPSHOT_DIR, f"snapshot_{asset_key}.json")
    if not os.path.exists(fname):
        return None
    with open(fname) as f:
        return json.load(f)


def load_summary():
    fname = os.path.join(SNAPSHOT_DIR, "snapshot_summary.json")
    if not os.path.exists(fname):
        return []
    with open(fname) as f:
        return json.load(f)


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/summary")
def api_summary():
    summary = load_summary()
    # Enrich with asset meta
    enriched = []
    for s in summary:
        asset_key = s.get("asset","").replace("=","_").replace("-","_").replace(".","_")
        meta = ASSET_META.get(asset_key, {})
        s["brand_color"] = meta.get("color", "#C9A84C")
        s["brand_icon"]  = meta.get("icon",  "•")
        s["brand_label"] = meta.get("label", s.get("label", asset_key))
        s["ticker"]      = meta.get("ticker", s.get("asset",""))
        enriched.append(s)
    return jsonify(enriched)


@app.route("/api/asset/<asset_key>")
def api_asset(asset_key):
    snap = load_snapshot(asset_key)
    if not snap:
        return jsonify({"error": f"No snapshot found for {asset_key}"}), 404
    meta = ASSET_META.get(asset_key, {})
    snap["brand_color"] = meta.get("color", "#C9A84C")
    snap["brand_icon"]  = meta.get("icon",  "•")
    return jsonify(snap)


@app.route("/api/assets")
def api_assets():
    assets = []
    for key in ASSET_META.keys():
        snap = load_snapshot(key)
        if snap:
            meta = ASSET_META[key]
            # Get last 60 close prices for sparkline
            price_data = snap.get("price_data", [])
            closes = [p["close"] for p in price_data[-60:] if "close" in p]
            assets.append({
                "key":             key,
                "asset":           snap.get("asset"),
                "label":           meta["label"],
                "ticker":          meta["ticker"],
                "icon":            meta["icon"],
                "brand_color":     meta["color"],
                "current_price":   snap.get("current_price"),
                "overall_sentiment": snap.get("overall_sentiment","UNKNOWN"),
                "sentiment_counts":  snap.get("sentiment_counts",{}),
                "avg_confidence":    snap.get("avg_confidence"),
                "fear_greed":        snap.get("fear_greed"),
                "polymarket_signals":snap.get("polymarket_signals"),
                "macro_context":     snap.get("macro_context"),
                "news_headlines":    snap.get("news_headlines",[])[:8],
                "news_sources_hit":  snap.get("news_sources_hit",[]),
                "news_sentiment":    snap.get("news_sentiment",[])[:8],
                "candle_count":      len(price_data),
                "closes":            closes,
                "snapshot_time":     snap.get("snapshot_time",""),
                # Freshness: the dashboard's 5-min auto-refresh only re-reads
                # these JSON files, so it can display hours-old data while
                # looking live. Surface the real age so that's visible.
                "data_as_of":        snap.get("data_as_of"),
                "age_seconds":       snap.get("age_seconds"),
                "is_stale":          snap.get("is_stale"),
                "stale_after_seconds": snap.get("stale_after_seconds"),
                "candle_interval":   snap.get("candle_interval"),
                "news_scope":        snap.get("news_scope"),
                "news_scope_note":   snap.get("news_scope_note"),
                "errors":            snap.get("errors",[]),
            })
    return jsonify(assets)


@app.route("/api/fng")
def api_fng():
    # Pull from any crypto snapshot
    snap = load_snapshot("BTCUSDT")
    if snap and snap.get("fear_greed"):
        return jsonify(snap["fear_greed"])
    return jsonify({"value": None, "classification": "Unknown"})


# ── trade proposals (human-in-the-loop execution) ──────────

def _load_json_file(name, default):
    fname = os.path.join(SNAPSHOT_DIR, name)
    if not os.path.exists(fname):
        return default
    try:
        with open(fname) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


@app.route("/api/proposals")
def api_proposals():
    pend = _load_json_file("pending_orders.json", [])
    log = _load_json_file("trade_log.json", [])
    pend = sorted(pend, key=lambda p: p.get("created_at", ""), reverse=True)
    return jsonify({
        "pending": [p for p in pend if p.get("status") == "pending"],
        "recent": pend[:20],
        "open_positions": [t for t in log
                           if t.get("status") == "filled" and not t.get("closed_at")],
        "log_recent": log[-10:],
    })


@app.route("/api/orders/<order_id>/approve", methods=["POST"])
def api_approve(order_id):
    import trader
    return jsonify(trader.approve(order_id, via="dashboard"))


@app.route("/api/orders/<order_id>/reject", methods=["POST"])
def api_reject(order_id):
    import trader
    return jsonify(trader.reject(order_id, via="dashboard"))


@app.route("/api/trader/status")
def api_trader_status():
    import trader
    return jsonify({
        "mode": trader.mode(),
        "base_url": trader._base_url(),
        "kill_switch": trader.kill_switch_on(),
        "equity": trader.account_equity(),
        "tradable": sorted(trader.TRADABLE),
    })


DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JAFAR Intelligence</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;900&family=JetBrains+Mono:wght@300;400;500&family=Crimson+Pro:ital,wght@0,300;0,400;1,300&display=swap" rel="stylesheet">
<style>
:root {
  --void:      #05050A;
  --obsidian:  #08080E;
  --deep:      #0C0C18;
  --panel:     #0F0F1C;
  --panel2:    #131325;
  --edge:      #1A1A30;
  --edge2:     #222240;
  --gold:      #C9A84C;
  --gold-dim:  #7A6430;
  --gold-bright:#E8C86A;
  --papyrus:   #E8DFC8;
  --pap-dim:   #8A8270;
  --bear:      #C0392B;
  --bear-dim:  #6B2318;
  --bull:      #1A8C4E;
  --bull-dim:  #0F4A2A;
  --neutral:   #5D6D7E;
  --fear:      #E74C3C;
  --greed:     #27AE60;
  --mono:      'JetBrains Mono', monospace;
  --display:   'Cinzel', serif;
  --body:      'Crimson Pro', serif;
}
* { margin:0; padding:0; box-sizing:border-box; }
html { scroll-behavior:smooth; }
body {
  background:var(--void);
  color:var(--papyrus);
  font-family:var(--body);
  min-height:100vh;
  overflow-x:hidden;
}
/* starfield */
body::before {
  content:''; position:fixed; inset:0; pointer-events:none; z-index:0;
  background-image:
    radial-gradient(1px 1px at 7%  11%, rgba(201,168,76,.3) 0%,transparent 100%),
    radial-gradient(1px 1px at 22% 44%, rgba(201,168,76,.18) 0%,transparent 100%),
    radial-gradient(1px 1px at 46% 17%, rgba(201,168,76,.35) 0%,transparent 100%),
    radial-gradient(1px 1px at 70% 62%, rgba(201,168,76,.22) 0%,transparent 100%),
    radial-gradient(1px 1px at 88%  8%, rgba(201,168,76,.28) 0%,transparent 100%),
    radial-gradient(1px 1px at 13% 77%, rgba(201,168,76,.12) 0%,transparent 100%),
    radial-gradient(1px 1px at 57% 87%, rgba(201,168,76,.18) 0%,transparent 100%),
    radial-gradient(2px 2px at 81% 32%, rgba(201,168,76,.1) 0%,transparent 100%);
}
.wrap { position:relative; z-index:1; max-width:1600px; margin:0 auto; padding:0 24px 60px; }

/* ── TOPBAR ── */
.topbar {
  position:sticky; top:0; z-index:100;
  background:rgba(8,8,14,.95);
  backdrop-filter:blur(12px);
  border-bottom:1px solid var(--edge);
  padding:0 24px;
  display:flex; align-items:center; justify-content:space-between;
  height:56px;
}
.topbar-left { display:flex; align-items:center; gap:16px; }
.eye-mini { width:36px; height:24px; }
.brand { font-family:var(--display); font-size:13px; font-weight:900; letter-spacing:.2em; color:var(--gold); }
.brand-sub { font-family:var(--mono); font-size:8px; color:var(--pap-dim); letter-spacing:.25em; margin-top:1px; }
.topbar-center { display:flex; gap:32px; }
.price-pill { display:flex; align-items:center; gap:6px; }
.price-pill-icon { font-size:11px; font-weight:700; }
.price-pill-val { font-family:var(--mono); font-size:11px; color:var(--papyrus); }
.price-pill-chg { font-family:var(--mono); font-size:9px; }
.price-pill-chg.up { color:var(--greed); }
.price-pill-chg.dn { color:var(--fear); }
.topbar-right { display:flex; align-items:center; gap:20px; }
.clock-group { display:flex; gap:16px; }
.clk { text-align:right; }
.clk-lbl { font-family:var(--mono); font-size:7px; color:var(--pap-dim); letter-spacing:.2em; display:block; }
.clk-val  { font-family:var(--mono); font-size:11px; color:var(--gold); display:block; }
.live-dot { width:6px; height:6px; background:var(--greed); border-radius:50%; animation:blink 2s infinite; box-shadow:0 0 6px var(--greed); flex-shrink:0; }
@keyframes blink { 0%,100%{opacity:1}50%{opacity:.4} }

/* ── HEADER ── */
.header { padding:32px 0 20px; border-bottom:1px solid var(--gold-dim); margin-bottom:24px; display:flex; align-items:center; justify-content:space-between; }
.header-left { display:flex; align-items:center; gap:20px; }
.eye-large { width:80px; height:54px; }
.header-title h1 { font-family:var(--display); font-size:24px; font-weight:900; letter-spacing:.22em; color:var(--gold); }
.header-title p { font-family:var(--mono); font-size:9px; color:var(--pap-dim); letter-spacing:.28em; margin-top:4px; }
.header-right { text-align:right; }
.last-update { font-family:var(--mono); font-size:9px; color:var(--pap-dim); letter-spacing:.1em; }
.last-update span { color:var(--gold); display:block; font-size:11px; }
.refresh-btn {
  margin-top:8px; padding:5px 14px; background:transparent;
  border:1px solid var(--gold-dim); border-radius:2px; cursor:pointer;
  font-family:var(--mono); font-size:9px; letter-spacing:.2em; color:var(--gold-dim);
  text-transform:uppercase; transition:all .2s;
}
.refresh-btn:hover { background:var(--gold-dim); color:var(--obsidian); }

/* ── FNG + SUMMARY ROW ── */
.summary-row { display:grid; grid-template-columns:280px 1fr; gap:16px; margin-bottom:24px; }
.fng-card {
  background:linear-gradient(135deg,var(--panel) 0%,#100808 100%);
  border:1px solid var(--bear-dim); border-radius:3px; padding:20px;
  display:flex; flex-direction:column; gap:12px;
}
.fng-lbl { font-family:var(--mono); font-size:8px; letter-spacing:.28em; color:var(--pap-dim); text-transform:uppercase; }
.fng-num { font-family:var(--display); font-size:52px; font-weight:900; line-height:1; }
.fng-cls { font-family:var(--display); font-size:11px; letter-spacing:.18em; margin-top:2px; }
.fng-bar { height:4px; background:linear-gradient(90deg,#E74C3C 0%,#E67E22 25%,#F1C40F 50%,#2ECC71 75%,#27AE60 100%); border-radius:2px; position:relative; margin-top:4px; }
.fng-needle { position:absolute; top:-5px; width:2px; height:14px; background:var(--papyrus); border-radius:1px; transform:translateX(-50%); transition:left 1s ease; box-shadow:0 0 4px rgba(232,223,200,.7); }
.fng-bar-labels { display:flex; justify-content:space-between; font-family:var(--mono); font-size:7px; color:var(--pap-dim); margin-top:4px; }
.portfolio-card {
  background:var(--panel); border:1px solid var(--edge); border-radius:3px; padding:20px;
}
.port-title { font-family:var(--display); font-size:9px; letter-spacing:.22em; color:var(--gold-dim); text-transform:uppercase; margin-bottom:14px; padding-bottom:9px; border-bottom:1px solid var(--edge); }
.port-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }
.port-item { }
.port-lbl { font-family:var(--mono); font-size:7px; letter-spacing:.2em; color:var(--pap-dim); text-transform:uppercase; margin-bottom:3px; }
.port-val { font-family:var(--mono); font-size:16px; font-weight:500; color:var(--papyrus); }
.port-val.bear { color:var(--fear); }
.port-val.bull { color:var(--greed); }
.port-val.gold { color:var(--gold); }
.port-sub { font-family:var(--mono); font-size:8px; color:var(--pap-dim); margin-top:2px; }

/* ── DIVIDER ── */
.divider { display:flex; align-items:center; gap:14px; margin:28px 0; }
.div-line { flex:1; height:1px; background:linear-gradient(90deg,transparent,var(--gold-dim),transparent); }
.div-glyph { font-family:var(--display); font-size:11px; color:var(--gold-dim); letter-spacing:.32em; white-space:nowrap; }

/* ── ASSET CARD ── */
.asset-card {
  margin-bottom:20px;
  border-radius:3px;
  overflow:hidden;
  border:1px solid var(--edge);
  transition:border-color .3s;
  position:relative;
}
.asset-card:hover { border-color:var(--gold-dim); }
.asset-card::before {
  content:'';
  position:absolute;
  left:0; top:0; bottom:0;
  width:3px;
}
/* Brand color left border applied via JS */

.card-header {
  display:grid;
  grid-template-columns:200px 1fr 180px 180px 160px;
  gap:0;
  background:var(--panel);
  border-bottom:1px solid var(--edge);
  align-items:stretch;
}
.card-header-cell {
  padding:16px 18px;
  border-right:1px solid var(--edge);
}
.card-header-cell:last-child { border-right:none; }

/* Asset identity */
.asset-icon { font-size:24px; font-weight:900; margin-bottom:2px; }
.asset-ticker-name { font-family:var(--display); font-size:13px; font-weight:600; letter-spacing:.1em; color:var(--papyrus); }
.asset-full-name { font-family:var(--mono); font-size:8px; color:var(--pap-dim); letter-spacing:.15em; margin-top:2px; }
.asset-source { font-family:var(--mono); font-size:7px; color:var(--pap-dim); letter-spacing:.1em; margin-top:4px; }

/* Price cell */
.price-lbl { font-family:var(--mono); font-size:7px; color:var(--pap-dim); letter-spacing:.2em; text-transform:uppercase; margin-bottom:4px; }
.price-val { font-family:var(--mono); font-size:26px; font-weight:500; line-height:1; }
.price-val.bear { color:var(--fear); }
.price-val.bull { color:var(--greed); }
.price-val.flat { color:var(--papyrus); }
.price-candles { font-family:var(--mono); font-size:8px; color:var(--pap-dim); margin-top:6px; }

/* Sentiment cell */
.sent-lbl { font-family:var(--mono); font-size:7px; color:var(--pap-dim); letter-spacing:.2em; text-transform:uppercase; margin-bottom:8px; }
.sent-signal { font-family:var(--display); font-size:14px; font-weight:600; letter-spacing:.1em; margin-bottom:8px; }
.sent-signal.bear { color:var(--bear); }
.sent-signal.bull { color:var(--bull); }
.sent-signal.neutral { color:var(--neutral); }
.sent-bar-row { display:flex; align-items:center; gap:6px; margin-bottom:4px; }
.sent-bar-lbl { font-family:var(--mono); font-size:7px; color:var(--pap-dim); width:44px; }
.sent-bar-bg { flex:1; height:3px; background:var(--edge); border-radius:2px; overflow:hidden; }
.sent-bar-fill { height:100%; border-radius:2px; transition:width 1.2s ease; }
.sent-bar-fill.p { background:var(--bull); }
.sent-bar-fill.n { background:var(--bear); }
.sent-bar-fill.u { background:var(--neutral); }
.sent-bar-num { font-family:var(--mono); font-size:7px; color:var(--pap-dim); width:16px; text-align:right; }
.sent-conf { font-family:var(--mono); font-size:8px; color:var(--pap-dim); margin-top:6px; }

/* Polymarket cell */
.poly-lbl { font-family:var(--mono); font-size:7px; color:var(--pap-dim); letter-spacing:.2em; text-transform:uppercase; margin-bottom:8px; }
.poly-arc-wrap { display:flex; align-items:center; gap:12px; }
.poly-arc-svg { width:64px; height:36px; }
.poly-text { }
.poly-signal { font-family:var(--display); font-size:11px; font-weight:600; letter-spacing:.08em; }
.poly-signal.bull { color:var(--bull); }
.poly-signal.bear { color:var(--bear); }
.poly-signal.neutral { color:var(--neutral); }
.poly-signal.unknown { color:var(--pap-dim); }
.poly-prob { font-family:var(--mono); font-size:18px; font-weight:500; color:var(--gold); margin:2px 0; }
.poly-question { font-family:var(--body); font-style:italic; font-size:10px; color:var(--pap-dim); line-height:1.3; margin-top:4px; }

/* Macro cell */
.macro-lbl { font-family:var(--mono); font-size:7px; color:var(--pap-dim); letter-spacing:.2em; text-transform:uppercase; margin-bottom:8px; }
.macro-row { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }
.macro-row-lbl { font-family:var(--mono); font-size:8px; color:var(--pap-dim); }
.macro-row-val { font-family:var(--mono); font-size:10px; color:var(--papyrus); font-weight:500; }
.macro-signal { font-family:var(--display); font-size:9px; letter-spacing:.1em; margin-top:8px; padding:3px 7px; border-radius:2px; display:inline-block; }
.macro-signal.bull { background:rgba(26,140,78,.15); color:var(--bull); border:1px solid rgba(26,140,78,.3); }
.macro-signal.bear { background:rgba(192,57,43,.15); color:var(--bear); border:1px solid rgba(192,57,43,.3); }
.macro-signal.neutral { background:rgba(93,109,126,.15); color:var(--neutral); border:1px solid rgba(93,109,126,.3); }

/* Card body — sparkline + news */
.card-body { display:grid; grid-template-columns:1fr 380px; background:var(--deep); }
.card-sparkline { padding:16px 18px; border-right:1px solid var(--edge); }
.spark-title { font-family:var(--mono); font-size:7px; color:var(--pap-dim); letter-spacing:.2em; text-transform:uppercase; margin-bottom:8px; }
.spark-svg { width:100%; height:80px; }
.card-news { padding:14px 18px; }
.news-title { font-family:var(--mono); font-size:7px; color:var(--pap-dim); letter-spacing:.2em; text-transform:uppercase; margin-bottom:8px; }
.news-list { list-style:none; }
.news-item { display:flex; gap:8px; align-items:flex-start; padding:6px 0; border-bottom:1px solid var(--edge); }
.news-item:last-child { border-bottom:none; }
.news-dot { width:5px; height:5px; border-radius:50%; flex-shrink:0; margin-top:4px; }
.news-dot.p { background:var(--bull); box-shadow:0 0 3px var(--bull); }
.news-dot.n { background:var(--bear); box-shadow:0 0 3px var(--bear); }
.news-dot.u { background:var(--neutral); }
.news-hl { font-size:11px; color:var(--papyrus); line-height:1.35; }
.news-src { font-family:var(--mono); font-size:8px; color:var(--pap-dim); margin-top:1px; }

/* Sources strip */
.sources-strip { background:var(--panel); border-top:1px solid var(--edge); padding:8px 18px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.src-lbl { font-family:var(--mono); font-size:7px; color:var(--pap-dim); letter-spacing:.15em; text-transform:uppercase; margin-right:4px; }
.src-badge { font-family:var(--mono); font-size:7px; letter-spacing:.1em; padding:1px 6px; border-radius:2px; border:1px solid var(--edge2); color:var(--pap-dim); text-transform:uppercase; }
.src-badge.on { border-color:var(--gold-dim); color:var(--gold-dim); }

/* ── LOADING STATE ── */
.loading { text-align:center; padding:80px 0; }
.loading-text { font-family:var(--display); font-size:14px; letter-spacing:.2em; color:var(--gold-dim); animation:fade 1.5s infinite; }
@keyframes fade { 0%,100%{opacity:.4}50%{opacity:1} }

/* ── FOOTER ── */
.footer { border-top:1px solid var(--edge); padding-top:20px; margin-top:40px; display:flex; justify-content:space-between; align-items:center; }
.footer-l { font-family:var(--mono); font-size:8px; color:var(--pap-dim); letter-spacing:.12em; line-height:1.9; }
.footer-r { font-family:var(--display); font-size:10px; letter-spacing:.3em; color:var(--gold-dim); }

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width:4px; }
::-webkit-scrollbar-track { background:var(--obsidian); }
::-webkit-scrollbar-thumb { background:var(--gold-dim); border-radius:2px; }

@media(max-width:1200px) {
  .card-header { grid-template-columns:160px 1fr 160px 160px; }
  .card-header-cell:nth-child(5) { display:none; }
  .port-grid { grid-template-columns:repeat(2,1fr); }
  .topbar-center { display:none; }
}
@media(max-width:900px) {
  .card-header { grid-template-columns:1fr 1fr; }
  .card-body { grid-template-columns:1fr; }
  .summary-row { grid-template-columns:1fr; }
}
/* ── Trade proposals ── */
.trade-bar { display:flex; justify-content:space-between; align-items:center; gap:16px;
  background:var(--panel); border:1px solid var(--edge); border-radius:10px;
  padding:10px 16px; margin-bottom:16px; }
.trade-mode { font-family:var(--mono); font-size:10px; letter-spacing:.18em; color:var(--gold-bright); }
.trade-mode.kill { color:var(--fear); }
.trade-hint { font-family:var(--body); font-size:13px; color:var(--pap-dim); font-style:italic; }
.prop-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:16px; }
.prop-card { background:var(--panel); border:1px solid var(--edge); border-radius:12px; padding:16px 18px; position:relative; }
.prop-card.pending { border-color:var(--gold-dim); }
.prop-card.approved { border-color:var(--bull); opacity:.85; }
.prop-card.rejected, .prop-card.expired, .prop-card.failed { border-color:var(--edge2); opacity:.6; }
.prop-head { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:10px; }
.prop-sym { font-family:var(--display); font-size:16px; color:var(--papyrus); letter-spacing:.05em; }
.prop-side { font-family:var(--mono); font-size:11px; letter-spacing:.14em; padding:3px 9px; border-radius:5px; }
.prop-side.buy { background:var(--bull-dim); color:#7FE0A8; }
.prop-side.sell { background:var(--bear-dim); color:#F0A0A0; }
.prop-row { display:flex; justify-content:space-between; font-family:var(--mono); font-size:10px;
  color:var(--pap-dim); padding:3px 0; border-bottom:1px solid var(--edge); }
.prop-row span:last-child { color:var(--papyrus); }
.prop-gate { font-family:var(--mono); font-size:9px; color:var(--gold-dim); margin-top:8px; letter-spacing:.08em; }
.prop-actions { display:flex; gap:10px; margin-top:14px; }
.btn-approve, .btn-reject { flex:1; font-family:var(--mono); font-size:11px; letter-spacing:.14em;
  padding:9px 0; border-radius:7px; border:1px solid transparent; cursor:pointer; transition:.15s; }
.btn-approve { background:var(--bull-dim); color:#9FE8BD; border-color:var(--bull); }
.btn-approve:hover { background:var(--bull); color:#04120A; }
.btn-reject { background:transparent; color:var(--pap-dim); border-color:var(--edge2); }
.btn-reject:hover { background:var(--bear-dim); color:#F0A0A0; }
.prop-badge { font-family:var(--mono); font-size:9px; letter-spacing:.16em; padding:2px 8px; border-radius:4px; }
.prop-badge.pending { background:var(--gold-dim); color:#0A0A12; }
.prop-badge.approved { background:var(--bull); color:#04120A; }
.prop-badge.rejected, .prop-badge.expired, .prop-badge.failed { background:var(--edge2); color:var(--pap-dim); }
.prop-exp { font-family:var(--mono); font-size:9px; color:var(--fear); margin-top:8px; }
.prop-why { font-family:var(--mono); font-size:10px; line-height:1.5; color:var(--pap-dim);
  white-space:pre-wrap; margin-top:10px; padding:10px 12px; background:rgba(255,255,255,.02);
  border-left:2px solid var(--edge2); border-radius:6px; max-height:240px; overflow:auto; }
</style>
</head>
<body>

<!-- TOPBAR -->
<div class="topbar">
  <div class="topbar-left">
    <svg class="eye-mini" viewBox="0 0 144 100" fill="none">
      <path d="M20 28 Q72 8 124 28" stroke="#C9A84C" stroke-width="2" fill="none"/>
      <path d="M8 55 Q36 22 72 20 Q108 22 136 55 Q108 80 72 82 Q36 80 8 55Z" stroke="#C9A84C" stroke-width="1.8" fill="none"/>
      <circle cx="72" cy="52" r="16" fill="none" stroke="#C9A84C" stroke-width="1.5"/>
      <circle cx="72" cy="52" r="7" fill="#C9A84C"/>
      <circle cx="72" cy="52" r="3" fill="#08080E"/>
      <path d="M36 74 L28 90" stroke="#C9A84C" stroke-width="1.5" fill="none"/>
      <path d="M36 74 L44 80" stroke="#C9A84C" stroke-width="1.2" fill="none"/>
    </svg>
    <div>
      <div class="brand">JAFAR INTELLIGENCE</div>
      <div class="brand-sub">Market Oracle · Local Operations</div>
    </div>
  </div>
  <div class="topbar-center" id="ticker-bar">
    <!-- filled by JS -->
  </div>
  <div class="topbar-right">
    <div class="clock-group">
      <div class="clk"><span class="clk-lbl">Local EAT</span><span class="clk-val" id="clk-nbi">—</span></div>
      <div class="clk"><span class="clk-lbl">UTC</span><span class="clk-val" id="clk-utc">—</span></div>
      <div class="clk"><span class="clk-lbl">New York ET</span><span class="clk-val" id="clk-ny">—</span></div>
    </div>
    <div class="live-dot"></div>
  </div>
</div>

<div class="wrap">

  <!-- HEADER -->
  <header class="header">
    <div class="header-left">
      <svg class="eye-large" viewBox="0 0 144 100" fill="none">
        <path d="M20 28 Q72 8 124 28" stroke="#C9A84C" stroke-width="2.5" fill="none" stroke-linecap="round"/>
        <path d="M8 55 Q36 22 72 20 Q108 22 136 55 Q108 80 72 82 Q36 80 8 55Z" stroke="#C9A84C" stroke-width="2" fill="none"/>
        <circle cx="72" cy="52" r="18" fill="none" stroke="#C9A84C" stroke-width="1.8"/>
        <circle cx="72" cy="52" r="8" fill="#C9A84C"/>
        <circle cx="72" cy="52" r="3.5" fill="#07070D"/>
        <circle cx="75" cy="49" r="1.5" fill="#E8C96A" opacity=".8"/>
        <line x1="8" y1="55" x2="2" y2="55" stroke="#C9A84C" stroke-width="1.8"/>
        <line x1="136" y1="55" x2="142" y2="55" stroke="#C9A84C" stroke-width="1.8"/>
        <path d="M36 74 L28 92 L32 96" stroke="#C9A84C" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M36 74 L44 82" stroke="#C9A84C" stroke-width="1.5" fill="none" stroke-linecap="round"/>
        <path d="M108 74 L116 80 L124 76" stroke="#C9A84C" stroke-width="1.5" fill="none" stroke-linecap="round"/>
        <circle cx="72" cy="52" r="22" stroke="#C9A84C" stroke-width="0.4" opacity=".2"/>
      </svg>
      <div class="header-title">
        <h1>JAFAR INTELLIGENCE</h1>
        <p>Analyst Terminal · Data Layer Active · Forecast Layer Pending</p>
      </div>
    </div>
    <div class="header-right">
      <div class="last-update">Last data sync<span id="last-sync">—</span></div>
      <button class="refresh-btn" onclick="loadAllData()">↻ Refresh Now</button>
    </div>
  </header>

  <!-- FNG + SUMMARY -->
  <div class="summary-row">
    <div class="fng-card">
      <div class="fng-lbl">Crypto Fear &amp; Greed Index</div>
      <div class="fng-num" id="fng-num" style="color:var(--fear)">—</div>
      <div class="fng-cls" id="fng-cls" style="color:var(--bear)">LOADING...</div>
      <div>
        <div class="fng-bar"><div class="fng-needle" id="fng-needle" style="left:50%"></div></div>
        <div class="fng-bar-labels"><span>Fear</span><span>Neutral</span><span>Greed</span></div>
      </div>
    </div>
    <div class="portfolio-card">
      <div class="port-title">𓂀 Portfolio Overview</div>
      <div class="port-grid" id="port-grid">
        <div class="port-item"><div class="port-lbl">Assets Tracked</div><div class="port-val gold" id="port-assets">—</div><div class="port-sub">live positions</div></div>
        <div class="port-item"><div class="port-lbl">Consensus Signal</div><div class="port-val" id="port-signal">—</div><div class="port-sub">FinBERT NLP</div></div>
        <div class="port-item"><div class="port-lbl">Avg Confidence</div><div class="port-val gold" id="port-conf">—</div><div class="port-sub">sentiment scoring</div></div>
        <div class="port-item"><div class="port-lbl">Data Sources</div><div class="port-val gold" id="port-sources">—</div><div class="port-sub">active feeds</div></div>
        <div class="port-item"><div class="port-lbl">Total Headlines</div><div class="port-val gold" id="port-news">—</div><div class="port-sub">scored by FinBERT</div></div>
        <div class="port-item"><div class="port-lbl">Polymarket Mkts</div><div class="port-val gold" id="port-poly">—</div><div class="port-sub">prediction markets</div></div>
        <div class="port-item"><div class="port-lbl">Refresh Cycle</div><div class="port-val gold">5 min</div><div class="port-sub">auto-refresh</div></div>
        <div class="port-item"><div class="port-lbl">Next Refresh</div><div class="port-val gold" id="port-next">—</div><div class="port-sub">countdown</div></div>
      </div>
    </div>
  </div>

  <!-- ASSET CARDS -->
  <div class="divider">
    <div class="div-line"></div>
    <div class="div-glyph">𓂀 &nbsp; ASSET INTELLIGENCE — DEEP DIVE</div>
    <div class="div-line"></div>
  </div>

  <div id="assets-container">
    <div class="loading"><div class="loading-text">LOADING INTELLIGENCE DATA...</div></div>
  </div>

  <!-- TRADE PROPOSALS (human-in-the-loop) -->
  <div class="divider">
    <div class="div-line"></div>
    <div class="div-glyph">⚖ &nbsp; TRADE PROPOSALS — HUMAN APPROVAL REQUIRED</div>
    <div class="div-line"></div>
  </div>

  <div class="trade-bar">
    <div class="trade-mode" id="trade-mode">MODE: —</div>
    <div class="trade-hint" id="trade-hint">Engine proposes · you approve · executor sends.</div>
  </div>
  <div id="proposals-container">
    <div class="loading"><div class="loading-text">NO PROPOSALS LOADED</div></div>
  </div>

  <!-- FOOTER -->
  <footer class="footer">
    <div class="footer-l">
      JAFAR INTELLIGENCE · PRIVATE RESEARCH TERMINAL · LOCAL, LOCAL<br>
      Data: Binance · yfinance · FRED · GDELT · Polymarket · FinBERT NLP<br>
      Not financial advice · Research purposes only · All signals require human judgment
    </div>
    <div class="footer-r">𓂀 · ☽ · ☥</div>
  </footer>

</div>

<script>
// ── Clocks ────────────────────────────────────────────────────
function pad(n){return String(n).padStart(2,'0')}
function updateClocks(){
  const now=new Date();
  const nbi=new Date(now.getTime()+3*3600000);
  const ny =new Date(now.getTime()-4*3600000);
  document.getElementById('clk-utc').textContent=pad(now.getUTCHours())+':'+pad(now.getUTCMinutes())+':'+pad(now.getUTCSeconds());
  document.getElementById('clk-nbi').textContent=pad(nbi.getUTCHours())+':'+pad(nbi.getUTCMinutes())+':'+pad(nbi.getUTCSeconds());
  document.getElementById('clk-ny').textContent=pad(ny.getUTCHours())+':'+pad(ny.getUTCMinutes())+':'+pad(ny.getUTCSeconds());
}
setInterval(updateClocks,1000);
updateClocks();

// ── Countdown ─────────────────────────────────────────────────
let nextRefresh=300;
setInterval(()=>{
  nextRefresh--;
  if(nextRefresh<=0){nextRefresh=300;loadAllData();}
  const m=Math.floor(nextRefresh/60);
  const s=nextRefresh%60;
  const el=document.getElementById('port-next');
  if(el) el.textContent=m+'m '+pad(s)+'s';
},1000);

// ── Sparkline ─────────────────────────────────────────────────
function drawSparkline(svgEl, closes, color){
  if(!closes||closes.length<2){svgEl.innerHTML='';return;}
  const W=svgEl.clientWidth||400, H=80;
  const mn=Math.min(...closes), mx=Math.max(...closes);
  const range=mx-mn||1;
  const pts=closes.map((v,i)=>{
    const x=(i/(closes.length-1))*W;
    const y=H-((v-mn)/range)*(H-8)-4;
    return x+','+y;
  });
  const path='M '+pts.join(' L ');
  svgEl.innerHTML=`
    <defs>
      <linearGradient id="sg${Math.random().toString(36).slice(2)}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity="0.2"/>
        <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <path d="${path} L ${W},${H} L 0,${H} Z" fill="url(#sg${Math.random().toString(36).slice(2)})" opacity="0.6"/>
    <path d="${path}" stroke="${color}" stroke-width="1.5" fill="none"/>
  `;
}

// ── Polymarket probability arc ─────────────────────────────────
function drawPolyArc(svgEl, prob, color){
  if(prob===null||prob===undefined){svgEl.innerHTML='<text x="32" y="22" font-family="JetBrains Mono" font-size="9" fill="#5D6D7E" text-anchor="middle">N/A</text>';return;}
  const r=26, cx=32, cy=32;
  const angle=prob*180; // 0-180 degrees for semicircle
  const rad=(angle-180)*Math.PI/180;
  const ex=cx+r*Math.cos(rad);
  const ey=cy+r*Math.sin(rad);
  const largeArc=angle>90?1:0;
  svgEl.innerHTML=`
    <path d="M ${cx-r} ${cy} A ${r} ${r} 0 0 1 ${cx+r} ${cy}" stroke="#1A1A30" stroke-width="5" fill="none"/>
    <path d="M ${cx-r} ${cy} A ${r} ${r} 0 ${largeArc} 1 ${ex.toFixed(1)} ${ey.toFixed(1)}" stroke="${color}" stroke-width="5" fill="none" stroke-linecap="round"/>
    <text x="${cx}" y="${cy-6}" font-family="JetBrains Mono" font-size="10" fill="${color}" text-anchor="middle" font-weight="500">${Math.round(prob*100)}%</text>
    <text x="${cx}" y="${cy+4}" font-family="JetBrains Mono" font-size="7" fill="#8A8270" text-anchor="middle">BULL</text>
  `;
}

// ── Sentiment color helpers ───────────────────────────────────
function sentClass(s){
  if(!s) return 'neutral';
  const u=s.toUpperCase();
  if(u.includes('BULL')||u==='BULLISH') return 'bull';
  if(u.includes('BEAR')||u==='BEARISH') return 'bear';
  return 'neutral';
}
function sentLabel(s){
  if(!s||s==='UNKNOWN') return '—';
  return s;
}
function polyColor(sig){
  if(!sig) return '#5D6D7E';
  const s=sig.toUpperCase();
  if(s.includes('BULL')) return '#1A8C4E';
  if(s.includes('BEAR')) return '#C0392B';
  return '#5D6D7E';
}
function priceClass(sent){
  const s=(sent||'').toUpperCase();
  if(s==='BULLISH') return 'bull';
  if(s==='BEARISH') return 'bear';
  return 'flat';
}
function formatPrice(p){
  if(p===null||p===undefined) return '—';
  if(p>1000) return '$'+p.toLocaleString('en-US',{maximumFractionDigits:2});
  if(p>1)    return '$'+p.toFixed(4);
  return '$'+p.toFixed(6);
}

// ── Build asset card HTML ──────────────────────────────────────
function buildAssetCard(a){
  const sc=a.sentiment_counts||{};
  const pos=sc.positive||0, neg=sc.negative||0, neu=sc.neutral||0;
  const total=Math.max(pos+neg+neu,1);
  const pctP=Math.round(pos/total*100);
  const pctN=Math.round(neg/total*100);
  const pctU=Math.round(neu/total*100);
  const conf=a.avg_confidence?Math.round(a.avg_confidence*100)+'%':'—';
  const poly=a.polymarket_signals||{};
  const polyProb=poly.avg_prob_yes!==undefined?poly.avg_prob_yes:null;
  const polySig=poly.signal||'UNKNOWN';
  const polyTop=poly.top_market;
  const macro=a.macro_context||{};
  const sentCls=sentClass(a.overall_sentiment);
  const priceCls=priceClass(a.overall_sentiment);
  const polCol=polyColor(polySig);
  const newsItems=(a.news_sentiment||[]).slice(0,6).map((n,i)=>{
    const hl=a.news_headlines[i]||'';
    const src=(a.news_sources_hit||[])[i]||'';
    const dotCls=n.sentiment==='positive'?'p':n.sentiment==='negative'?'n':'u';
    const conf2=n.confidence?Math.round(n.confidence*100)+'%':'';
    return `<li class="news-item">
      <div class="news-dot ${dotCls}"></div>
      <div>
        <div class="news-hl">${hl.slice(0,90)}</div>
        <div class="news-src">${n.sentiment||''} ${conf2} · ${src}</div>
      </div>
    </li>`;
  }).join('');

  const srcBadges=(a.news_sources_hit||[]).map(s=>
    `<span class="src-badge on">${s}</span>`
  ).join('');

  const macroRows=macro&&macro.DGS10?`
    <div class="macro-row"><span class="macro-row-lbl">10Y Yield</span><span class="macro-row-val">${macro.DGS10?.toFixed(2)||'—'}%</span></div>
    <div class="macro-row"><span class="macro-row-lbl">2Y Yield</span><span class="macro-row-val">${macro.DGS2?.toFixed(2)||'—'}%</span></div>
    <div class="macro-row"><span class="macro-row-lbl">Real Yield</span><span class="macro-row-val">${macro.DFII10?.toFixed(2)||'—'}%</span></div>
    <div class="macro-row"><span class="macro-row-lbl">Breakeven</span><span class="macro-row-val">${macro.T10YIE?.toFixed(2)||'—'}%</span></div>
    <div class="macro-row"><span class="macro-row-lbl">Fed Funds</span><span class="macro-row-val">${macro.FEDFUNDS?.toFixed(2)||'—'}%</span></div>
    <div class="macro-row"><span class="macro-row-lbl">Yield Curve</span><span class="macro-row-val">${macro.yield_curve_status||'—'}</span></div>
    <div class="macro-signal ${sentClass(macro.gold_macro_signal?.toLowerCase().replace('_tailwind','').replace('_headwind',''))}">${macro.gold_macro_signal||'N/A'}</div>
  `:'<div style="font-family:var(--mono);font-size:8px;color:var(--pap-dim);margin-top:4px;">N/A for this asset</div>';

  return `
<div class="asset-card" id="card-${a.key}" data-color="${a.brand_color}">
  <div class="card-header">

    <!-- Identity -->
    <div class="card-header-cell" style="border-left:3px solid ${a.brand_color}">
      <div class="asset-icon" style="color:${a.brand_color}">${a.icon}</div>
      <div class="asset-ticker-name" style="color:${a.brand_color}">${a.ticker}</div>
      <div class="asset-full-name">${a.label}</div>
      <div class="asset-source">${a.candle_count} candles · Live</div>
    </div>

    <!-- Price -->
    <div class="card-header-cell">
      <div class="price-lbl">Current Price</div>
      <div class="price-val ${priceCls}">${formatPrice(a.current_price)}</div>
      <div class="price-candles">${a.candle_count} candles · ${a.news_headlines?.length||0} headlines</div>
    </div>

    <!-- Sentiment -->
    <div class="card-header-cell">
      <div class="sent-lbl">NLP Sentiment</div>
      <div class="sent-signal ${sentCls}">${sentLabel(a.overall_sentiment)}</div>
      <div class="sent-bar-row"><span class="sent-bar-lbl">Positive</span><div class="sent-bar-bg"><div class="sent-bar-fill p" style="width:${pctP}%"></div></div><span class="sent-bar-num">${pos}</span></div>
      <div class="sent-bar-row"><span class="sent-bar-lbl">Negative</span><div class="sent-bar-bg"><div class="sent-bar-fill n" style="width:${pctN}%"></div></div><span class="sent-bar-num">${neg}</span></div>
      <div class="sent-bar-row"><span class="sent-bar-lbl">Neutral</span><div class="sent-bar-bg"><div class="sent-bar-fill u" style="width:${pctU}%"></div></div><span class="sent-bar-num">${neu}</span></div>
      <div class="sent-conf">Avg confidence: ${conf}</div>
    </div>

    <!-- Polymarket -->
    <div class="card-header-cell">
      <div class="poly-lbl">Polymarket Signal</div>
      <div class="poly-arc-wrap">
        <svg class="poly-arc-svg" id="arc-${a.key}" viewBox="0 0 64 36"></svg>
        <div class="poly-text">
          <div class="poly-signal ${sentCls}">${polySig.replace(/_/g,' ')}</div>
          <div class="poly-prob">${polyProb!==null?Math.round(polyProb*100)+'%':'—'}</div>
        </div>
      </div>
      ${polyTop?`<div class="poly-question">${polyTop.question?.slice(0,70)||''}</div>`:''}
    </div>

    <!-- Macro -->
    <div class="card-header-cell">
      <div class="macro-lbl">Macro Context</div>
      ${macroRows}
    </div>

  </div>

  <!-- Sparkline + News -->
  <div class="card-body">
    <div class="card-sparkline">
      <div class="spark-title">Price History (last ${Math.min(a.closes?.length||0,60)} candles)</div>
      <svg class="spark-svg" id="spark-${a.key}" viewBox="0 0 400 80" preserveAspectRatio="none"></svg>
    </div>
    <div class="card-news">
      <div class="news-title">Intelligence Feed</div>
      <ul class="news-list">${newsItems||'<li style="font-family:var(--mono);font-size:8px;color:var(--pap-dim);padding:8px 0;">No headlines loaded</li>'}</ul>
    </div>
  </div>

  <!-- Sources -->
  <div class="sources-strip">
    <span class="src-lbl">Sources:</span>
    ${srcBadges||'<span class="src-badge">None</span>'}
    ${a.is_stale
      ? `<span style="font-family:var(--mono);font-size:7px;background:var(--red-dim,rgba(180,60,60,.22));color:#e08a8a;padding:2px 6px;border-radius:2px;border:1px solid rgba(180,60,60,.5);">⚠ STALE — data ${Math.round((a.age_seconds||0)/60)} min old, refresh the pipeline</span>`
      : `<span style="font-family:var(--mono);font-size:7px;color:var(--pap-dim);">${a.age_seconds!=null?Math.round(a.age_seconds/60)+' min old':''}</span>`}
    ${a.news_scope==='market_beta'
      ? `<span style="font-family:var(--mono);font-size:7px;background:rgba(90,150,110,.18);color:#8fc9a4;padding:2px 6px;border-radius:2px;border:1px solid rgba(90,150,110,.4);" title="${(a.news_scope_note||'').replace(/"/g,'')}">news: BTC-beta (justified)</span>`
      : a.news_scope==='market_wide'
      ? `<span style="font-family:var(--mono);font-size:7px;background:rgba(180,140,40,.18);color:#d8b45a;padding:2px 6px;border-radius:2px;border:1px solid rgba(180,140,40,.4);" title="${(a.news_scope_note||'').replace(/"/g,'')}">⚠ news: weak market-wide proxy</span>`
      : ''}
    <span style="margin-left:auto;font-family:var(--mono);font-size:7px;color:var(--pap-dim);">${a.snapshot_time?.slice(0,19)||''} UTC</span>
  </div>
</div>`;
}

// ── Load all data ──────────────────────────────────────────────
async function loadAllData(){
  try {
    // Assets
    const res=await fetch('/api/assets');
    const assets=await res.json();

    // FNG
    const fngRes=await fetch('/api/fng');
    const fng=await fngRes.json();

    // Update FNG
    if(fng.value!==null){
      document.getElementById('fng-num').textContent=fng.value;
      document.getElementById('fng-cls').textContent=fng.classification?.toUpperCase()||'';
      document.getElementById('fng-needle').style.left=fng.value+'%';
      const v=fng.value;
      const color=v<25?'#E74C3C':v<50?'#E67E22':v<75?'#F1C40F':'#27AE60';
      document.getElementById('fng-num').style.color=color;
    }

    // Ticker bar
    const tickerBar=document.getElementById('ticker-bar');
    tickerBar.innerHTML=assets.slice(0,6).map(a=>`
      <div class="price-pill">
        <span class="price-pill-icon" style="color:${a.brand_color}">${a.icon}</span>
        <span class="price-pill-val">${formatPrice(a.current_price)}</span>
      </div>
    `).join('');

    // Portfolio summary
    const bearCount=assets.filter(a=>a.overall_sentiment==='BEARISH').length;
    const bullCount=assets.filter(a=>a.overall_sentiment==='BULLISH').length;
    const consensus=bearCount>bullCount?'BEARISH':bullCount>bearCount?'BULLISH':'NEUTRAL';
    const avgConf=assets.reduce((s,a)=>s+(a.avg_confidence||0),0)/Math.max(assets.length,1);
    const totalNews=assets.reduce((s,a)=>s+(a.news_headlines?.length||0),0);
    const allSrcs=new Set(assets.flatMap(a=>a.news_sources_hit||[]));

    document.getElementById('port-assets').textContent=assets.length;
    const sigEl=document.getElementById('port-signal');
    sigEl.textContent=consensus;
    sigEl.className='port-val '+sentClass(consensus);
    document.getElementById('port-conf').textContent=Math.round(avgConf*100)+'%';
    document.getElementById('port-sources').textContent=allSrcs.size;
    document.getElementById('port-news').textContent=totalNews;
    document.getElementById('port-poly').textContent='Active';
    document.getElementById('last-sync').textContent=new Date().toUTCString().split(' ')[4]+' UTC';

    // Render asset cards
    const container=document.getElementById('assets-container');
    container.innerHTML=assets.map(buildAssetCard).join('');

    // Draw sparklines and arcs after DOM update
    requestAnimationFrame(()=>{
      assets.forEach(a=>{
        const sparkEl=document.getElementById('spark-'+a.key);
        if(sparkEl&&a.closes) drawSparkline(sparkEl,a.closes,a.brand_color);
        const arcEl=document.getElementById('arc-'+a.key);
        if(arcEl){
          const poly=a.polymarket_signals||{};
          drawPolyArc(arcEl,poly.avg_prob_yes,polyColor(poly.signal));
        }
      });
    });

  } catch(err){
    console.error('Data load error:',err);
    document.getElementById('assets-container').innerHTML=
      '<div class="loading"><div class="loading-text" style="color:var(--fear)">ERROR LOADING DATA — IS THE SERVER RUNNING?</div></div>';
  }
}

// ── Trade proposals ───────────────────────────────────────────
function fmtMoney(x){return x==null?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:2})}
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}

function propCard(p){
  const side=String(p.side||'').toLowerCase();
  const gate=p.kronos_gate&&p.kronos_gate.opposed
    ?`⚠ Kronos ${p.kronos_gate.opposed?'opposed':'agreed'} · action ${p.kronos_gate.action_before}→${p.kronos_gate.action_after} · size ×${p.kronos_gate.size_mult}`
    :'Kronos: neutral / no gate';
  const exp=p.status==='pending'
    ?`<div class="prop-exp">expires ${String(p.expires_at||'').slice(11,19)} UTC</div>`:'';
  const actions=p.status==='pending'?`
    <div class="prop-actions">
      <button class="btn-approve" onclick="act('${p.id}','approve')">✓ APPROVE</button>
      <button class="btn-reject" onclick="act('${p.id}','reject')">✗ REJECT</button>
    </div>`:'';
  return `<div class="prop-card ${p.status}">
    <div class="prop-head">
      <div class="prop-sym">${p.label||p.symbol}</div>
      <div class="prop-side ${side}">${p.side}</div>
    </div>
    <div class="prop-row"><span>action / score</span><span>${p.action} · ${p.composite==null?'—':(p.composite>0?'+':'')+Number(p.composite).toFixed(1)}</span></div>
    <div class="prop-row"><span>qty</span><span>${Number(p.qty).toFixed(6)}</span></div>
    <div class="prop-row"><span>entry</span><span>${fmtMoney(p.entry)}</span></div>
    <div class="prop-row"><span>stop</span><span>${fmtMoney(p.stop_loss)}</span></div>
    <div class="prop-row"><span>tp1 / tp2</span><span>${fmtMoney(p.take_profit_1)} / ${fmtMoney(p.take_profit_2)}</span></div>
    <div class="prop-row"><span>notional (${p.leverage}×)</span><span>${fmtMoney(p.notional_usdt)} USDT</span></div>
    <div class="prop-badge ${p.status}">${String(p.status).toUpperCase()}</div>
    <div class="prop-gate">${gate}</div>
    ${p.rationale?`<div class="prop-why">${esc(p.rationale)}</div>`:''}
    ${exp}${actions}
  </div>`;
}

async function loadProposals(){
  try{
    const st=await (await fetch('/api/trader/status')).json();
    const modeEl=document.getElementById('trade-mode');
    modeEl.textContent='MODE: '+String(st.mode||'?').toUpperCase()+(st.kill_switch?' · KILL SWITCH ON':'');
    modeEl.className='trade-mode'+(st.kill_switch?' kill':'');
    const data=await (await fetch('/api/proposals')).json();
    const list=(data.pending||[]).concat((data.recent||[]).filter(p=>p.status!=='pending'));
    const cont=document.getElementById('proposals-container');
    if(!list.length){
      cont.innerHTML='<div class="loading"><div class="loading-text">NO PROPOSALS — ENGINE HAS NOTHING ACTIONABLE</div></div>';
      return;
    }
    cont.innerHTML='<div class="prop-grid">'+list.map(propCard).join('')+'</div>';
  }catch(e){ console.error('proposals load error',e); }
}

async function act(id,what){
  if(what==='approve' && !confirm('Approve and EXECUTE this order? This sends to the exchange in testnet/live mode.')) return;
  const btn=event.target; btn.disabled=true; btn.textContent='…';
  try{
    const r=await fetch(`/api/orders/${id}/${what}`,{method:'POST'});
    const j=await r.json();
    if(!j.ok) alert('Failed: '+(j.error||'unknown'));
  }catch(e){ alert('Request error: '+e); }
  loadProposals();
}

// ── Init ──────────────────────────────────────────────────────
loadAllData();
loadProposals();
setInterval(loadAllData, 5*60*1000);
setInterval(loadProposals, 30*1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print("="*60)
    print("  JAFAR INTELLIGENCE — Dashboard Server")
    print("="*60)
    print(f"  Looking for snapshots in: {SNAPSHOT_DIR}")
    print(f"  Open browser at: http://localhost:5000")
    print(f"  Auto-refreshes every 5 minutes")
    print(f"  Press Ctrl+C to stop")
    print("="*60)
    # PORT: Railway (and friends) inject the port the service must bind.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
"""
TELEGRAM BOT - query the trading engine from your phone.

Long-polling (no inbound URL), so it works behind Railway's NAT with zero
config. Runs as a thread inside run_service.py.

Commands:
  /help          list commands
  /status        service uptime, last runs, data freshness
  /calendar      upcoming macro releases (CPI, NFP, FOMC…)
  /signals       compact table of every asset
  /signal <A>    full signal: score, levels, sizing, rationale
  /price <A>     live price + distance to entry/SL/TP1
  /news <A>      latest headlines for an asset
  /positions     pending proposals + open positions
  /run           kick off a full pipeline run now

Free text goes to an optional OpenAI-compatible LLM wired to signals.json
(set OPENAI_API_KEY); without a key it replies with /help.
"""

import glob
import html
import json
import os
import threading
import time

import requests

import alerts

DATA_DIR = os.environ.get("DATA_DIR") or os.getcwd()
API      = "https://api.telegram.org/bot{token}"

HELP = (
    "JAFAR INTELLIGENCE - commands:\n"
    "/status - service health + data freshness\n"
    "/calendar - upcoming macro releases (CPI, NFP, FOMC…)\n"
    "/signals - all assets, actions, scores\n"
    "/signal zec - full signal for one asset\n"
    "/price btc - live price vs entry/SL/TP\n"
    "/news gold - latest headlines\n"
    "/positions - pending proposals + open positions\n"
    "/run - run the full pipeline now\n"
    "Or just type a question - it goes to the LLM with the full Jafar "
    "context (signals, factors, Kronos, news, Polymarket, positions)."
)


# ── data helpers ────────────────────────────────────────────

def _load_json(fname, default):
    try:
        with open(os.path.join(DATA_DIR, fname)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_signals():
    return _load_json("signals.json", [])


def load_snapshots():
    out = {}
    for path in glob.glob(os.path.join(DATA_DIR, "snapshot_*.json")):
        base = os.path.basename(path)
        if "summary" in base:
            continue
        try:
            with open(path) as f:
                snap = json.load(f)
                out[snap.get("asset", base)] = snap
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return out


def find_signal(query, signals=None):
    signals = signals if signals is not None else load_signals()
    q = query.strip().upper()
    ql = query.strip().lower()
    for s in signals:
        if q == s.get("asset", "").upper():
            return s
    for s in signals:
        if q in s.get("asset", "").upper() or ql in str(s.get("label", "")).lower():
            return s
    return None


def live_price(sig):
    asset      = sig.get("asset", "")
    asset_type = sig.get("asset_type", "")
    if asset_type == "crypto" or asset.endswith("USDT"):
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
    if x is None:
        return "n/a"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{x:,.{nd}f}" if abs(x) < 1000 else f"{x:,.2f}"


def _freshness_line(sig):
    if sig.get("data_as_of"):
        age_min = (sig.get("age_seconds") or 0) / 60
        flag = "STALE" if sig.get("is_stale") else "fresh"
        return f"data {str(sig['data_as_of'])[:16]} · {age_min:.0f} min · {flag}"
    return "freshness unknown"


# ── command handlers ────────────────────────────────────────

def cmd_status(state):
    lines = ["SERVICE"]
    if state.get("started_at"):
        lines.append(f"  up since {state['started_at']}")
    lines.append(f"  full pipeline : {state.get('last_full_run') or 'pending'}"
                 f"{' OK' if state.get('last_full_ok') else ''}"
                 + (f" ({state.get('last_full_duration')}s)"
                    if state.get("last_full_duration") else ""))
    lines.append(f"  price refresh : {state.get('last_light_refresh') or 'pending'}")
    if state.get("last_error"):
        lines.append(f"  last error    : {str(state['last_error'])[:120]}")

    signals = load_signals()
    if not signals:
        lines.append("\nDATA\n  no signals yet - first pipeline run in progress")
        return "\n".join(lines)

    lines.append(f"\nDATA (signals.json, {len(signals)} assets)")
    for s in signals:
        stale = " ⚠STALE" if s.get("is_stale") else ""
        lines.append(
            f"  {str(s.get('label'))[:13]:<13} {s.get('action'):<12} "
            f"{s.get('composite_score'):+.1f}  {_fmt(s.get('current_price'))}"
            f"{stale}"
        )
    return "\n".join(lines)


def cmd_calendar():
    try:
        import macro_calendar
        return macro_calendar.format_list(n=10)
    except Exception as e:
        return f"Calendar unavailable: {type(e).__name__}: {e}"


def cmd_signals():
    signals = load_signals()
    if not signals:
        return "No signals yet - first pipeline run in progress."
    lines = ["SIGNALS"]
    for s in signals:
        stale = " ⚠" if s.get("is_stale") else ""
        lines.append(
            f"{s.get('asset'):<11} {s.get('action'):<12} "
            f"{s.get('composite_score'):+.1f}  {_fmt(s.get('current_price'))}{stale}"
        )
    return "\n".join(lines)


def cmd_signal(query):
    if not query:
        return "usage: /signal zec  (or nvda, gold, btc…)"
    sig = find_signal(query)
    if not sig:
        return f"No signal matches '{query}'. Try /signals."
    lv = sig.get("levels") or {}
    size = sig.get("position_size_frac")
    lines = [
        f"{sig.get('label')} ({sig.get('asset')}) - {sig.get('action')} "
        f"({sig.get('composite_score'):+.1f})",
        f"price {_fmt(sig.get('current_price'))} · bias {lv.get('bias', 'n/a')}",
        f"entry {_fmt(lv.get('entry'))}  SL {_fmt(lv.get('stop_loss'))}",
        f"TP1 {_fmt(lv.get('take_profit_1'))}  TP2 {_fmt(lv.get('take_profit_2'))}",
        f"size {size * 100:.0f}% acct" if size else "size n/a (no-trade)",
        _freshness_line(sig),
        "",
        (sig.get("rationale") or "").strip(),
    ]
    return "\n".join(lines)


def cmd_price(query):
    if not query:
        return "usage: /price btc"
    sig = find_signal(query)
    if not sig:
        return f"No signal matches '{query}'. Try /signals."
    try:
        price = live_price(sig)
    except Exception as e:
        return f"Live price unavailable for {sig.get('asset')}: {e}"
    lv = sig.get("levels") or {}

    def dist(level):
        if level is None or not price:
            return "n/a"
        return f"{(price - level) / level * 100:+.2f}%"

    lines = [f"{sig.get('label')}: {_fmt(price)} "
             f"(signal @ {_fmt(sig.get('current_price'))})"]
    if lv.get("entry") is not None:
        lines.append(f"vs entry {dist(lv['entry'])}  "
                     f"vs SL {dist(lv['stop_loss'])}  "
                     f"vs TP1 {dist(lv.get('take_profit_1'))}")
    return "\n".join(lines)


def cmd_news(query):
    if not query:
        return "usage: /news gold"
    sig = find_signal(query)
    if not sig:
        return f"No asset matches '{query}'. Try /signals."
    snap = load_snapshots().get(sig.get("asset"))
    headlines = (snap or {}).get("news_headlines") or []
    if not headlines:
        return f"No headlines stored for {sig.get('label')}."
    lines = [f"{sig.get('label')} - latest headlines:"]
    for h in headlines[:5]:
        if isinstance(h, dict):
            title  = h.get("title") or h.get("headline") or str(h)
            source = h.get("source") or h.get("feed") or ""
            url    = h.get("url") or h.get("link") or ""
            lines.append(f"• {title}" + (f" ({source})" if source else ""))
            if url:
                lines.append(f"  {url}")
        else:
            lines.append(f"• {h}")
    return "\n".join(lines)


# ── optional LLM free chat ──────────────────────────────────

def _jafar_dossier():
    """Rich, condensed snapshot of the whole engine for the LLM."""
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    out = [f"=== JAFAR INTELLIGENCE - LIVE CONTEXT @ {ts} ==="]

    st = _load_json("service_state.json", {}) or {}
    out.append(
        "SERVICE: mode=%s kill_switch=%s last_full_run=%s last_full_ok=%s "
        "duration=%ss" % (
            os.environ.get("TRADING_MODE", "paper"),
            os.path.exists(os.path.join(DATA_DIR, "KILL_SWITCH")),
            st.get("last_full_run"), st.get("last_full_ok"),
            st.get("last_full_duration")))

    sigs = load_signals()
    out.append("\nSIGNALS (authoritative - reason from these):")
    for s in sigs:
        lv = s.get("levels") or {}
        out.append(
            f"- {s.get('asset')} [{s.get('label')}] {s.get('action')} "
            f"score {s.get('composite_score')} price {s.get('current_price')} "
            f"stale={s.get('is_stale')} as_of={s.get('data_as_of')}")
        if lv:
            out.append(
                f"    levels: bias {lv.get('bias')} entry {lv.get('entry')} "
                f"SL {lv.get('stop_loss')} TP1 {lv.get('take_profit_1')} "
                f"TP2 {lv.get('take_profit_2')}")
        fparts = [f"{f.get('factor')}(w{f.get('weight')})={f.get('score')}"
                  for f in (s.get("factors") or [])]
        if fparts:
            out.append("    factors: " + ", ".join(fparts))
        km = (s.get("factor_meta") or {}).get("kronos")
        if km:
            out.append(
                f"    kronos: {km.get('direction')} "
                f"{km.get('expected_return_pct')}% /{km.get('pred_len')}bars "
                f"z={km.get('z')} score={km.get('score')}")
        g = s.get("kronos_gate")
        if g:
            out.append(
                f"    kronos_gate: {g.get('action_before')}->{g.get('action_after')} "
                f"opposed={g.get('opposed')} size_mult={g.get('size_mult')}")

    out.append("\nNEWS / CROWD PROBABILITIES (per asset):")
    for s in sigs:
        asset = s.get("asset")
        snap = _load_json(_snap_name(asset), {}) or {}
        hl = [str(h) for h in (snap.get("news_headlines") or [])][:3]
        scope = snap.get("news_scope_note") or ""
        if hl:
            out.append(f"- {asset} ({scope}): " + " | ".join(hl))
        pm = snap.get("polymarket_signals") or {}
        if pm.get("signal"):
            out.append(
                f"    polymarket {pm.get('signal')} conf={pm.get('confidence')} "
                f"mkts={pm.get('market_count')}: "
                f"{str(pm.get('interpretation') or '')[:180]}")

    for s in sigs:
        snap = _load_json(_snap_name(s.get("asset")), {}) or {}
        fg = snap.get("fear_greed")
        if fg:
            out.append(f"\nFEAR & GREED: {fg.get('value')} "
                       f"({fg.get('classification')}) @ {fg.get('date')}")
            break

    kt = _load_json("kronos_track.json", []) or []
    resolved = [r for r in kt if r.get("hit") is not None]
    if resolved:
        hits = sum(1 for r in resolved if r.get("hit"))
        out.append(f"\nKRONOS TRACK: {hits}/{len(resolved)} resolved forecasts "
                   f"correct ({100 * hits / len(resolved):.0f}%)")

    pend = [p for p in (_load_json("pending_orders.json", []) or [])
            if p.get("status") == "pending"]
    log = _load_json("trade_log.json", []) or []
    openp = [t for t in log if t.get("status") == "filled" and not t.get("closed_at")]
    out.append(f"\nTRADER ({os.environ.get('TRADING_MODE', 'paper')}): "
               f"{len(pend)} pending, {len(openp)} open positions")
    for p in pend:
        out.append(f"  pending {p.get('asset')} {p.get('side')} qty {p.get('qty')} "
                   f"@ {p.get('entry')} SL {p.get('stop_loss')} "
                   f"TP1 {p.get('take_profit_1')} expires {p.get('expires_at')}")
    for t in openp[-6:]:
        out.append(f"  open {t.get('asset')} {t.get('side')} @ {t.get('entry')}")

    dossier = "\n".join(out)
    if len(dossier) > 16000:
        dossier = dossier[:15990] + "\n…[truncated]"
    return dossier


def _parse_responses(data):
    out = []
    for item in data.get("output", []) or []:
        if item.get("type") == "message":
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    out.append(c["text"])
    return "\n".join(out).strip() or data.get("output_text") or "(empty response)"


def llm_reply(text):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not key or not model:
        return None
    base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    style = os.environ.get("OPENAI_API_STYLE", "chat").lower()
    headers = {"Authorization": f"Bearer {key}"}

    system = (
        "You are the JAFAR INTELLIGENCE trading analyst, answering "
        "questions sent over Telegram. Use ONLY the live Jafar context below "
        "as the source of truth. If something isn't covered, say so rather "
        "than guessing. Be direct, concise and concrete; cite the numbers. "
        "Not financial advice.\n\n" + _jafar_dossier()
    )
    try:
        if style == "responses":
            r = requests.post(
                f"{base}/responses", headers=headers,
                json={"model": model, "instructions": system,
                      "input": text, "max_output_tokens": 1000},
                timeout=90,
            )
            r.raise_for_status()
            return _parse_responses(r.json())
        user_text = text + ("\n/no_think" if model.startswith("qwen3") else "")
        r = requests.post(
            f"{base}/chat/completions", headers=headers,
            json={"model": model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user_text}],
                  "max_tokens": 1000},
            timeout=180,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(LLM unavailable: {e})\n\n{HELP}"


# ── dispatch + main loop ────────────────────────────────────

def _send(chat_id, text):
    tok = alerts.token()
    if not tok:
        print(f"  [bot] would reply: {text.splitlines()[0]}")
        return
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    try:
        requests.post(API.format(token=tok) + "/sendMessage",
                      json={"chat_id": chat_id, "text": text,
                            "disable_web_page_preview": True},
                      timeout=15)
    except requests.RequestException as e:
        print(f"  [bot] reply failed: {alerts.redact(e)}")


def _send_kb(chat_id, text, keyboard, parse_mode=None):
    tok = alerts.token()
    if not tok:
        return
    payload = {"chat_id": chat_id, "text": text[:4000],
               "reply_markup": {"inline_keyboard": keyboard},
               "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(API.format(token=tok) + "/sendMessage",
                      json=payload, timeout=15)
    except requests.RequestException as e:
        print(f"  [bot] kb send failed: {alerts.redact(e)}")


def _api_post(method, payload):
    tok = alerts.token()
    if not tok:
        return
    try:
        requests.post(API.format(token=tok) + "/" + method, json=payload, timeout=15)
    except requests.RequestException as e:
        print(f"  [bot] {method} failed: {alerts.redact(e)}")


# ── trade proposals: approve/reject inline buttons ──────────

_ASSET_EMOJI = {
    "BTCUSDT": "🪙", "BTC": "🪙", "BITCOIN": "🪙",
    "ETHUSDT": "💎", "ETH": "💎", "ETHEREUM": "💎",
    "SOLUSDT": "☀️", "SOL": "☀️", "SOLANA": "☀️",
    "ZECUSDT": "🛡️", "ZEC": "🛡️", "ZCASH": "🛡️",
    "GC=F": "🥇", "GOLD": "🥇",
    "BZ=F": "🛢️", "BRENT": "🛢️", "OIL": "🛢️",
    "DX-Y.NYB": "💵", "DXY": "💵",
    "NVDA": "🖥️", "META": "📘", "AMD": "🔺",
}


def _emoji_for(sym, label):
    for key in (sym, label):
        k = str(key or "").upper()
        if k in _ASSET_EMOJI:
            return _ASSET_EMOJI[k]
        base = k.split()[0] if k else ""
        if base in _ASSET_EMOJI:
            return _ASSET_EMOJI[base]
    return "📊"


def _money(x):
    try:
        return "$" + format(float(x), ",.2f")
    except (TypeError, ValueError):
        return "-"


def _price(x):
    """Compact price for alts whose units are tiny (0.003104 -> $0.003104)."""
    try:
        return "$" + format(float(x), ".6g")
    except (TypeError, ValueError):
        return "-"


def _qty(x):
    try:
        return format(float(x), ",.6f").rstrip("0").rstrip(".") or "0"
    except (TypeError, ValueError):
        return "-"


def _pct(entry, target):
    try:
        return f"{(float(target) - float(entry)) / float(entry) * 100:+.2f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def _snap_name(asset):
    safe = str(asset).replace("=", "_").replace(".", "_").replace("-", "_")
    return "snapshot_" + safe + ".json"


def _detail_html(asset):
    """Factor breakdown + top headlines, looked up from signals/snapshot."""
    if not asset:
        return ""
    sigs = load_signals()
    s = next((x for x in sigs
              if str(x.get("asset", "")).upper() == str(asset).upper()), None)
    snap = _load_json(_snap_name(asset), None)
    blocks = []
    if s:
        fm = s.get("factor_meta") or {}
        flines = []
        for f in s.get("factors") or []:
            name = str(f.get("factor"))
            w = f.get("weight") or 0
            sc = f.get("score") or 0
            m = fm.get(name) or {}
            if name == "fear_greed":
                extra = f"{m.get('value')} {m.get('classification', '')}".strip()
            elif name == "news":
                c = m.get("counts") or {}
                extra = (f"{c.get('positive', 0)}P/{c.get('negative', 0)}N/"
                         f"{c.get('neutral', 0)}Neu {m.get('overall', '')}").strip()
            elif name == "momentum":
                extra = f"RSI {m.get('rsi')} · {m.get('trend', '')}".strip()
            elif name == "polymarket":
                extra = str(m.get("signal", ""))
            else:
                extra = ""
            flines.append(f"  {name:<10} ×{w:.2f}   {sc:+6.1f}   {extra}")
        if flines:
            blocks.append("⚖️ <b>FACTORS</b> (weight · score)\n"
                          f"<pre>{html.escape(chr(10).join(flines))}</pre>")
        km = fm.get("kronos")
        if km:
            blocks.append(
                f"🔮 Kronos {km.get('direction')} "
                f"{km.get('expected_return_pct')}% /{km.get('pred_len')} bars · "
                f"z {km.get('z')} · score {km.get('score')}")
    if snap:
        scope = str(snap.get("news_scope_note") or "").strip()
        if scope:
            blocks.append(f"🗞 <i>{html.escape(scope)}</i>")
        hl = [str(h) for h in (snap.get("news_headlines") or [])][:3]
        if hl:
            blocks.append("📰 <b>HEADLINES</b>\n" +
                          "\n".join("• " + html.escape(h) for h in hl))
        pm = snap.get("polymarket_signals") or {}
        if pm.get("signal"):
            q = str((pm.get("top_market") or {}).get("question") or "").strip()
            blocks.append(
                f"🎲 <b>POLYMARKET</b> {html.escape(str(pm.get('signal')))} · "
                f"{pm.get('market_count')} mkts · conf "
                f"{html.escape(str(pm.get('confidence', '?')))}"
                + (f"\n• {html.escape(q[:100])}" if q else ""))
        mac = snap.get("macro_context")
        if isinstance(mac, dict) and mac:
            body = "\n".join(
                f"• {html.escape(str(k))}: {html.escape(str(v))[:70]}"
                for k, v in list(mac.items())[:4])
            blocks.append("🌍 <b>MACRO</b>\n" + body)
    return ("\n\n" + "\n\n".join(blocks)) if blocks else ""


def _proposal_html(p):
    """Rich emoji + punctuated proposal card (Telegram HTML parse mode)."""
    side = str(p.get("side") or "")
    action = str(p.get("action") or "")
    side_emoji = "📈" if side.upper() == "BUY" else "📉"
    asset_emoji = _emoji_for(p.get("symbol"), p.get("label"))
    label = html.escape(str(p.get("label") or p.get("symbol") or "?"))
    sym = html.escape(str(p.get("symbol") or ""))
    entry = p.get("entry")
    score = p.get("composite")
    score_txt = f"{score:+.1f}" if isinstance(score, (int, float)) else "-"
    head = (f"<b>{html.escape(side)}</b> · {html.escape(action)}"
            if action.upper() != side.upper() else f"<b>{html.escape(action)}</b>")
    lines = [
        f"{asset_emoji} <b>{label}</b> · <code>{sym}</code>",
        f"{side_emoji} {head}   ·   score <b>{score_txt}</b>",
        "",
        f"💰 Entry   <b>{_money(entry)}</b>",
        f"🛑 Stop    {_money(p.get('stop_loss'))}  <i>({_pct(entry, p.get('stop_loss'))})</i>",
        f"🎯 TP1     {_money(p.get('take_profit_1'))}  <i>({_pct(entry, p.get('take_profit_1'))})</i>",
        f"🎯 TP2     {_money(p.get('take_profit_2'))}  <i>({_pct(entry, p.get('take_profit_2'))})</i>",
        f"📦 Qty     {_qty(p.get('qty'))}",
        f"💵 Size    {_money(p.get('notional_usdt'))} · {p.get('leverage')}× · "
        f"<b>{html.escape(str(p.get('mode')).upper())}</b>",
        f"⏳ Expires  {html.escape(str(p.get('expires_at'))[11:16])} UTC",
        f"🆔 <code>{html.escape(str(p.get('id')))}</code>",
    ]
    text = "\n".join(lines)
    why = str(p.get("rationale") or "").strip()
    if why:
        if len(why) > 1800:
            why = why[:1790] + "\n…"
        text += ("\n\n🧠 <b>WHY - Jafar output</b>\n"
                 f"<pre>{html.escape(why)}</pre>")
    text += _detail_html(p.get("asset"))
    return text


def notify_proposals(proposals):
    """Called by trader.plan() to present new proposals with buttons."""
    if not alerts.token():
        for p in proposals:
            print(f"  [bot] would propose {p['symbol']} {p['side']} ({p['id']})")
        return
    ids = alerts.recipients()
    for p in proposals:
        text = _proposal_html(p)
        kb = [[
            {"text": "✅ APPROVE", "callback_data": f"approve:{p['id']}"},
            {"text": "❌ REJECT",  "callback_data": f"reject:{p['id']}"},
        ]]
        for cid in ids:
            _send_kb(cid, text, kb, parse_mode="HTML")


def handle_callback(cq):
    data = cq.get("data", "") or ""
    msg = cq.get("message") or {}
    cid = str((msg.get("chat") or {}).get("id", ""))
    mid = msg.get("message_id")
    action, _, oid = data.partition(":")

    import trader
    if action == "approve":
        res = trader.approve(oid, via="telegram")
    elif action == "reject":
        res = trader.reject(oid, via="telegram")
    else:
        res = {"ok": False, "error": "unknown action"}

    if res.get("ok"):
        _api_post("answerCallbackQuery",
                  {"callback_query_id": cq.get("id"),
                   "text": "Approved" if action == "approve" else "Rejected"})
        p = res.get("order") or {}
        verdict = "✅ APPROVED" if action == "approve" else "❌ REJECTED"
        extra = ""
        if action == "approve":
            ex = res.get("execution") or {}
            extra = ("\n💵 paper fill @ " + _money(ex.get("fill_price"))
                     if ex.get("paper")
                     else "\n🏦 exchange order " + str(ex.get("entry_order_id")))
        if mid:
            _api_post("editMessageText", {
                "chat_id": cid, "message_id": mid,
                "parse_mode": "HTML",
                "text": (f"{verdict} - "
                         f"<b>{html.escape(str(p.get('label', oid)))}</b> "
                         f"(<code>{html.escape(str(oid))}</code>){extra}"),
            })
    else:
        _api_post("answerCallbackQuery",
                  {"callback_query_id": cq.get("id"),
                   "text": "Failed: " + str(res.get("error"))[:180]})



def cmd_positions():
    mode = os.environ.get("TRADING_MODE", "paper")
    pend = [p for p in (_load_json("pending_orders.json", []) or [])
            if p.get("status") == "pending"]
    log = _load_json("trade_log.json", []) or []
    openp = [t for t in log if t.get("status") == "filled" and not t.get("closed_at")]
    lines = [f"POSITIONS - mode {mode}",
             f"⏳ {len(pend)} pending · 📌 {len(openp)} open"]
    for p in pend:
        lines.append(
            f"⏳ {p.get('asset')} {p.get('side')} {_qty(p.get('qty'))} @ "
            f"{_money(p.get('entry'))} · id {p.get('id')}")
    for t in openp:
        lines.append(
            f"📌 {t.get('asset')} {t.get('side')} @ {_money(t.get('entry'))}")
    if not pend and not openp:
        lines.append("(nothing pending or open)")
    return "\n".join(lines)


def handle(msg, trigger_run, get_state):
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text    = (msg.get("text") or "").strip()
    if not text:
        return
    alerts.adopt_chat(chat_id)

    parts = text.split()
    cmd   = parts[0].split("@")[0].lstrip("/").lower() if text.startswith("/") else ""
    arg   = " ".join(parts[1:])

    if cmd in ("start", "help", ""):
        reply = HELP if cmd != "" else None
        if reply is None:
            reply = llm_reply(text) or HELP
    elif cmd == "status":
        reply = cmd_status(get_state())
    elif cmd == "calendar":
        reply = cmd_calendar()
    elif cmd == "signals":
        reply = cmd_signals()
    elif cmd == "signal":
        reply = cmd_signal(arg)
    elif cmd == "price":
        reply = cmd_price(arg)
    elif cmd == "news":
        reply = cmd_news(arg)
    elif cmd == "positions":
        reply = cmd_positions()
    elif cmd == "run":
        trigger_run()
        reply = ("Full pipeline kicked off (~2-5 min: news, sentiment, macro, "
                 "markets). I'll post the result here when it's done.")
    else:
        reply = llm_reply(text) or f"Unknown command.\n\n{HELP}"

    if reply:
        _send(chat_id, reply)


def run(trigger_run, get_state):
    """Blocking long-poll loop - call from its own thread."""
    tok = alerts.token()
    if not tok:
        print("  [bot] TELEGRAM_BOT_TOKEN not set - Telegram bot disabled "
              "(pipeline + alerts logging still work)")
        return
    print("  [bot] Telegram bot started (long-poll)")
    offset = 0
    while True:
        try:
            r = requests.get(
                API.format(token=tok) + "/getUpdates",
                params={"timeout": 30, "offset": offset,
                        "allowed_updates": ["message", "callback_query"]},
                timeout=40,
            )
            data = r.json()
            if not data.get("ok"):
                print(f"  [bot] getUpdates error: {alerts.redact(data)}")
                time.sleep(10)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if msg:
                    try:
                        handle(msg, trigger_run, get_state)
                    except Exception as e:
                        print(f"  [bot] handler error: {e}")
                cq = upd.get("callback_query")
                if cq:
                    try:
                        handle_callback(cq)
                    except Exception as e:
                        print(f"  [bot] callback error: {e}")
        except requests.RequestException as e:
            print(f"  [bot] poll failed, retrying in 10s: {alerts.redact(e)}")
            time.sleep(10)

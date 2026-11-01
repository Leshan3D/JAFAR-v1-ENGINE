"""
INTELLIGENCE BRIEF GENERATOR
Reads snapshot_summary.json and generates a clean HTML brief
that reads like a Reuters/Bloomberg morning note.

Run automatically at end of build_snapshot.py, or standalone:
  python generate_brief.py

Output: intelligence_brief.html (open in any browser)
        intelligence_brief_{date}.html (dated archive copy)
"""

import json
import os
from datetime import datetime, timezone


def load_summary(path="snapshot_summary.json"):
    if not os.path.exists(path):
        raise FileNotFoundError(f"No summary file found at {path}. Run build_snapshot.py first.")
    with open(path) as f:
        return json.load(f)


def sentiment_to_english(sentiment, confidence=None):
    conf_str = f" ({round(confidence*100)}% confidence)" if confidence else ""
    mapping = {
        "BULLISH":  f"Bullish{conf_str}",
        "BEARISH":  f"Bearish{conf_str}",
        "NEUTRAL":  f"Neutral{conf_str}",
        "UNKNOWN":  "Insufficient data",
    }
    return mapping.get(sentiment, sentiment)


def poly_to_english(poly):
    if not poly or poly.get("signal") == "UNKNOWN":
        return None
    sig  = poly.get("signal","").replace("_"," ").title()
    prob = poly.get("avg_prob_yes")
    top  = poly.get("top_market",{})
    q    = top.get("question","") if top else ""
    prob_str = f"{round(prob*100)}% bullish-equivalent" if prob is not None else ""
    return {"signal": sig, "prob": prob_str, "question": q}


def macro_to_bullets(macro):
    if not macro:
        return []
    bullets = []
    y10 = macro.get("DGS10")
    y2  = macro.get("DGS2")
    ry  = macro.get("DFII10")
    be  = macro.get("T10YIE")
    ff  = macro.get("FEDFUNDS")
    ycs = macro.get("yield_curve_status")
    ysp = macro.get("yield_curve_spread")
    sig = macro.get("gold_macro_signal","").replace("_"," ").title()
    rsn = macro.get("gold_macro_reasoning","")

    if y10: bullets.append(f"US 10-year Treasury yield at <strong>{y10:.2f}%</strong>")
    if y2:  bullets.append(f"2-year yield at <strong>{y2:.2f}%</strong>")
    if ry:  bullets.append(f"Real yield (inflation-adjusted) at <strong>{ry:.2f}%</strong>")
    if be:  bullets.append(f"10-year inflation expectations at <strong>{be:.2f}%</strong>")
    if ff:  bullets.append(f"Fed Funds Rate at <strong>{ff:.2f}%</strong>")
    if ycs and ysp is not None:
        status = "inverted — recession risk elevated" if ycs == "INVERTED" else "normal"
        bullets.append(f"Yield curve <strong>{status}</strong> (2Y–10Y spread: {ysp:+.3f}%)")
    if sig and rsn:
        bullets.append(f"Gold macro assessment: <strong>{sig}</strong> — {rsn.split('|')[0].strip()}")
    return bullets


def format_price(price, asset):
    if price is None:
        return "N/A"
    if "USDT" in asset or price > 100:
        return f"${price:,.2f}"
    return f"${price:.4f}"


def generate_brief(summary_path="snapshot_summary.json", output_path="intelligence_brief.html"):
    summary = load_summary(summary_path)
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%A, %d %B %Y — %H:%M UTC")
    local_str = datetime.fromtimestamp(
        now_utc.timestamp() + 3*3600
    ).strftime("%H:%M EAT")

    # Overall consensus
    sentiments = [s.get("overall_sentiment","UNKNOWN") for s in summary]
    bear_count = sentiments.count("BEARISH")
    bull_count = sentiments.count("BULLISH")
    if bear_count > bull_count:
        consensus = "RISK-OFF"
        consensus_color = "#C0392B"
        consensus_note = f"{bear_count} of {len(summary)} tracked assets show bearish sentiment signals."
    elif bull_count > bear_count:
        consensus = "RISK-ON"
        consensus_color = "#1A8C4E"
        consensus_note = f"{bull_count} of {len(summary)} tracked assets show bullish sentiment signals."
    else:
        consensus = "MIXED"
        consensus_color = "#C9A84C"
        consensus_note = "No clear directional consensus across tracked assets."

    # Fear & Greed from first crypto asset
    fng = None
    for s in summary:
        if s.get("fear_greed"):
            fng = s["fear_greed"]
            break

    # Build asset sections
    asset_sections = ""
    for s in summary:
        asset    = s.get("asset","")
        label    = s.get("label", asset)
        price    = s.get("current_price")
        candles  = s.get("candle_count",0)
        sent     = s.get("overall_sentiment","UNKNOWN")
        conf     = s.get("avg_confidence")
        sc       = s.get("sentiment_counts",{})
        headlines= s.get("news_headlines",[])
        sources  = s.get("news_sources_hit",[])
        poly     = s.get("polymarket_signals")
        macro    = s.get("macro_context")
        snap_t   = s.get("snapshot_time","")[:16].replace("T"," ")

        sent_color = "#C0392B" if sent=="BEARISH" else "#1A8C4E" if sent=="BULLISH" else "#C9A84C"
        price_str  = format_price(price, asset)

        # Sentiment summary sentence
        pos = sc.get("positive",0)
        neg = sc.get("negative",0)
        neu = sc.get("neutral",0)
        total = max(pos+neg+neu,1)
        sent_sentence = (
            f"Of {total} scored headlines, {neg} were negative ({round(neg/total*100)}%), "
            f"{pos} positive ({round(pos/total*100)}%), "
            f"and {neu} neutral ({round(neu/total*100)}%)."
            if total > 0 else "Insufficient headline data for sentiment analysis."
        )

        # Polymarket block
        poly_html = ""
        poly_data = poly_to_english(poly)
        if poly_data:
            poly_html = f"""
            <div class="poly-block">
                <span class="block-label">CROWD INTELLIGENCE (POLYMARKET)</span>
                <p>Prediction markets signal <strong>{poly_data['signal']}</strong>
                {f'with an average {poly_data["prob"]}' if poly_data['prob'] else ''}.
                {f'Most active market: <em>"{poly_data["question"][:80]}"</em>' if poly_data['question'] else ''}
                </p>
            </div>"""

        # Macro block
        macro_html = ""
        macro_bullets = macro_to_bullets(macro)
        if macro_bullets:
            bullets_html = "".join(f"<li>{b}</li>" for b in macro_bullets)
            macro_html = f"""
            <div class="macro-block">
                <span class="block-label">MACRO CONTEXT</span>
                <ul>{bullets_html}</ul>
            </div>"""

        # Headlines
        headlines_html = ""
        if headlines:
            items = "".join(
                f'<li class="hl-item"><span class="hl-bullet">›</span>{h}</li>'
                for h in headlines[:6]
            )
            src_str = ", ".join(sources[:6]) if sources else "Various"
            headlines_html = f"""
            <div class="headlines-block">
                <span class="block-label">INTELLIGENCE FEED</span>
                <ul class="hl-list">{items}</ul>
                <p class="src-note">Sources: {src_str}</p>
            </div>"""

        asset_sections += f"""
        <div class="asset-section">
            <div class="asset-header">
                <div class="asset-identity">
                    <span class="asset-label">{label}</span>
                    <span class="asset-ticker">{asset}</span>
                </div>
                <div class="asset-price-block">
                    <span class="asset-price">{price_str}</span>
                    <span class="asset-candles">{candles} candles · {snap_t} UTC</span>
                </div>
                <div class="asset-signal" style="background:{'rgba(192,57,43,.12)' if sent=='BEARISH' else 'rgba(26,140,78,.12)' if sent=='BULLISH' else 'rgba(201,168,76,.12)'}; border-color:{sent_color}">
                    <span class="signal-word" style="color:{sent_color}">{sent}</span>
                    <span class="signal-conf">{f'{round(conf*100)}% avg confidence' if conf else ''}</span>
                </div>
            </div>
            <div class="asset-body">
                <div class="sent-summary">
                    <span class="block-label">SENTIMENT ANALYSIS</span>
                    <p>{sent_sentence}</p>
                </div>
                {poly_html}
                {macro_html}
                {headlines_html}
            </div>
        </div>"""

    # FNG section
    fng_html = ""
    if fng:
        fng_val  = fng.get("value", 0)
        fng_cls  = fng.get("classification","Unknown")
        fng_date = fng.get("date","")
        fng_color= "#E74C3C" if fng_val < 25 else "#E67E22" if fng_val < 50 else "#F1C40F" if fng_val < 75 else "#27AE60"
        fng_note = (
            "Markets are in extreme fear — historically a zone where long-term buyers accumulate."
            if fng_val < 25 else
            "Markets are fearful — caution and risk-aversion dominate sentiment."
            if fng_val < 50 else
            "Markets are neutral — no strong directional bias in crowd psychology."
            if fng_val < 75 else
            "Markets are greedy — elevated risk appetite, historically a caution signal."
        )
        fng_html = f"""
        <div class="fng-section">
            <span class="block-label">CRYPTO FEAR &amp; GREED INDEX · {fng_date}</span>
            <div class="fng-row">
                <span class="fng-number" style="color:{fng_color}">{fng_val}</span>
                <div>
                    <div class="fng-class" style="color:{fng_color}">{fng_cls.upper()}</div>
                    <div class="fng-note">{fng_note}</div>
                </div>
            </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JAFAR Intelligence Brief · {now_utc.strftime('%d %b %Y')}</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900&family=Source+Serif+4:ital,wght@0,300;0,400;1,300;1,400&family=JetBrains+Mono:wght@300;400&display=swap" rel="stylesheet">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
    background:#F5F0E8;
    color:#1A1A1A;
    font-family:'Source Serif 4', Georgia, serif;
    font-size:16px;
    line-height:1.7;
}}
.page {{ max-width:820px; margin:0 auto; padding:40px 32px 80px; }}

/* MASTHEAD */
.masthead {{
    border-top:4px solid #1A1A1A;
    border-bottom:1px solid #1A1A1A;
    padding:20px 0 16px;
    margin-bottom:8px;
    text-align:center;
}}
.masthead-name {{
    font-family:'Playfair Display', serif;
    font-size:36px;
    font-weight:900;
    letter-spacing:.08em;
    color:#1A1A1A;
    line-height:1;
}}
.masthead-sub {{
    font-family:'JetBrains Mono', monospace;
    font-size:10px;
    letter-spacing:.3em;
    color:#666;
    margin-top:6px;
    text-transform:uppercase;
}}
.dateline {{
    font-family:'JetBrains Mono', monospace;
    font-size:11px;
    color:#666;
    letter-spacing:.1em;
    text-align:center;
    padding:8px 0;
    border-bottom:3px double #1A1A1A;
    margin-bottom:32px;
}}

/* LEAD SUMMARY */
.lead-summary {{
    margin-bottom:32px;
    padding-bottom:24px;
    border-bottom:1px solid #C9A84C;
}}
.lead-summary h2 {{
    font-family:'Playfair Display', serif;
    font-size:13px;
    font-weight:700;
    letter-spacing:.25em;
    text-transform:uppercase;
    color:#7A6430;
    margin-bottom:12px;
}}
.consensus-line {{
    font-family:'Playfair Display', serif;
    font-size:22px;
    font-weight:700;
    line-height:1.3;
    margin-bottom:8px;
    color:#1A1A1A;
}}
.consensus-line strong {{ color:{consensus_color}; }}
.lead-body {{ font-size:15px; color:#333; }}

/* FNG */
.fng-section {{
    background:#fff;
    border:1px solid #ddd;
    border-left:4px solid #C9A84C;
    padding:16px 20px;
    margin-bottom:32px;
    border-radius:2px;
}}
.fng-row {{ display:flex; align-items:center; gap:20px; margin-top:8px; }}
.fng-number {{ font-family:'Playfair Display',serif; font-size:52px; font-weight:900; line-height:1; }}
.fng-class {{ font-family:'Playfair Display',serif; font-size:16px; font-weight:700; }}
.fng-note {{ font-size:13px; color:#555; margin-top:4px; font-style:italic; }}

/* BLOCK LABEL */
.block-label {{
    font-family:'JetBrains Mono', monospace;
    font-size:9px;
    letter-spacing:.28em;
    color:#888;
    text-transform:uppercase;
    display:block;
    margin-bottom:6px;
}}

/* ASSET SECTION */
.asset-section {{
    margin-bottom:40px;
    padding-bottom:36px;
    border-bottom:1px solid #ddd;
}}
.asset-section:last-child {{ border-bottom:none; }}
.asset-header {{
    display:flex;
    align-items:flex-start;
    justify-content:space-between;
    gap:16px;
    margin-bottom:20px;
    padding-bottom:16px;
    border-bottom:1px solid #eee;
}}
.asset-identity {{ flex:1; }}
.asset-label {{
    font-family:'Playfair Display', serif;
    font-size:22px;
    font-weight:700;
    display:block;
    color:#1A1A1A;
    line-height:1;
}}
.asset-ticker {{
    font-family:'JetBrains Mono', monospace;
    font-size:10px;
    color:#888;
    letter-spacing:.2em;
    margin-top:3px;
    display:block;
}}
.asset-price-block {{ text-align:center; }}
.asset-price {{
    font-family:'JetBrains Mono', monospace;
    font-size:24px;
    font-weight:400;
    color:#1A1A1A;
    display:block;
}}
.asset-candles {{
    font-family:'JetBrains Mono', monospace;
    font-size:9px;
    color:#999;
    display:block;
    margin-top:3px;
}}
.asset-signal {{
    border:1px solid;
    border-radius:2px;
    padding:8px 14px;
    text-align:center;
    min-width:120px;
}}
.signal-word {{
    font-family:'Playfair Display', serif;
    font-size:14px;
    font-weight:700;
    display:block;
    letter-spacing:.08em;
}}
.signal-conf {{
    font-family:'JetBrains Mono', monospace;
    font-size:8px;
    color:#888;
    display:block;
    margin-top:2px;
    letter-spacing:.1em;
}}

/* BODY BLOCKS */
.asset-body {{ display:flex; flex-direction:column; gap:16px; }}
.sent-summary, .poly-block, .macro-block, .headlines-block {{
    padding:14px 16px;
    background:#fff;
    border:1px solid #e8e8e8;
    border-radius:2px;
}}
.sent-summary p, .poly-block p {{ font-size:14px; color:#333; }}
.macro-block ul {{
    list-style:none;
    padding:0;
    margin:0;
}}
.macro-block ul li {{
    font-size:13px;
    color:#333;
    padding:3px 0;
    border-bottom:1px solid #f0f0f0;
}}
.macro-block ul li:last-child {{ border-bottom:none; }}
.macro-block ul li::before {{ content:"→ "; color:#C9A84C; font-weight:700; }}
.hl-list {{ list-style:none; padding:0; }}
.hl-item {{
    display:flex;
    gap:8px;
    padding:5px 0;
    font-size:13px;
    color:#222;
    border-bottom:1px solid #f0f0f0;
    line-height:1.4;
}}
.hl-item:last-child {{ border-bottom:none; }}
.hl-bullet {{ color:#C9A84C; font-weight:700; flex-shrink:0; margin-top:1px; }}
.src-note {{
    font-family:'JetBrains Mono', monospace;
    font-size:9px;
    color:#aaa;
    letter-spacing:.1em;
    margin-top:8px;
    text-transform:uppercase;
}}

/* SECTIONS HEADER */
.section-header {{
    font-family:'Playfair Display', serif;
    font-size:11px;
    font-weight:700;
    letter-spacing:.3em;
    text-transform:uppercase;
    color:#7A6430;
    border-bottom:2px solid #C9A84C;
    padding-bottom:6px;
    margin:36px 0 20px;
}}

/* FOOTER */
.brief-footer {{
    margin-top:48px;
    padding-top:16px;
    border-top:3px double #1A1A1A;
    text-align:center;
}}
.brief-footer p {{
    font-family:'JetBrains Mono', monospace;
    font-size:9px;
    color:#999;
    letter-spacing:.15em;
    line-height:1.9;
    text-transform:uppercase;
}}

@media(max-width:600px) {{
    .asset-header {{ flex-direction:column; }}
    .asset-signal {{ min-width:auto; align-self:flex-start; }}
    .masthead-name {{ font-size:26px; }}
}}
@media print {{
    body {{ background:#fff; }}
    .page {{ padding:20px; }}
}}
</style>
</head>
<body>
<div class="page">

    <!-- MASTHEAD -->
    <div class="masthead">
        <div class="masthead-name">JAFAR Intelligence</div>
        <div class="masthead-sub">Private Market Intelligence Brief · Local Operations</div>
    </div>
    <div class="dateline">
        {now_str} &nbsp;·&nbsp; {local_str} &nbsp;·&nbsp;
        {len(summary)} Assets Tracked &nbsp;·&nbsp;
        {sum(s.get('headline_count',0) for s in summary)} Headlines Analysed
    </div>

    <!-- LEAD SUMMARY -->
    <div class="lead-summary">
        <h2>Executive Summary</h2>
        <div class="consensus-line">
            Market intelligence signals <strong>{consensus}</strong> conditions
            across tracked assets.
        </div>
        <p class="lead-body">
            {consensus_note}
            {f'The Crypto Fear &amp; Greed Index stands at <strong>{fng["value"]} ({fng["classification"]})</strong>, ' if fng else ''}
            indicating {'extreme risk aversion among market participants' if fng and fng['value'] < 25 else 'cautious sentiment' if fng and fng['value'] < 50 else 'neutral positioning' if fng and fng['value'] < 75 else 'elevated risk appetite'}.
            All signals reflect data as of {now_str}.
        </p>
    </div>

    <!-- FEAR & GREED -->
    {fng_html}

    <!-- ASSET INTELLIGENCE -->
    <div class="section-header">Asset Intelligence</div>
    {asset_sections}

    <!-- FOOTER -->
    <div class="brief-footer">
        <p>
            JAFAR Intelligence · Private Research Terminal · Local, Local<br>
            Data: Binance · yfinance · FRED API · GDELT · Polymarket · FinBERT NLP<br>
            Generated: {now_str}<br>
            Not financial advice · All signals require human judgment before action
        </p>
    </div>

</div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Also save dated archive copy
    dated_path = f"intelligence_brief_{now_utc.strftime('%Y%m%d_%H%M')}.html"
    with open(dated_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  Brief saved: {output_path}")
    print(f"  Archive:     {dated_path}")
    return output_path


if __name__ == "__main__":
    print("="*60)
    print("  JAFAR INTELLIGENCE — Brief Generator")
    print("="*60 + "\n")
    path = generate_brief()
    print(f"\n  Open in browser: {os.path.abspath(path)}")
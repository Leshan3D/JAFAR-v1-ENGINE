"""
ENTRY/EXIT SIGNAL ENGINE — JAFAR Intelligence
Multi-factor rules-based scoring that turns a data snapshot into an actionable
BUY / SELL / HOLD / WAIT recommendation with entry, stop-loss and take-profit
levels plus position-size guidance.

Factors (each contributes a weighted score, -100..+100):
  1. Fear & Greed Index   (contrarian)          weight 0.30  [crypto only]
  2. News sentiment       (directional)         weight 0.25
  3. Price momentum       (trend + RSI)         weight 0.30
  4. Macro backdrop       (yield curve/gold)    weight 0.05  [macro-linked assets]
  5. Polymarket crowd     (directional)         weight 0.10

Composite score maps to an action:
  >= +50  STRONG BUY
  >= +20  BUY
  >= +8   ACCUMULATE (add on dips)
  >  -8   HOLD / NEUTRAL
  <= -8   REDUCE (trim)
  <= -20  SELL
  <= -50  STRONG SELL / AVOID

Risk levels are derived from recent price volatility (ATR) so stop-loss and
take-profit scale with market conditions rather than being hard-coded.
"""

import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
WEIGHTS = {
    "fear_greed":  0.30,   # contrarian
    "news":        0.25,   # directional
    "momentum":    0.30,   # trend + RSI
    "macro":       0.05,   # macro-linked assets only
    "polymarket":  0.10,   # directional crowd
    # Positioning context from the perp tape (funding + basis + OI). It is the
    # only crypto factor that says something the headlines cannot: where the
    # leverage is. Weight 0.10 on top of the other 0.95 -> 1.05, which the
    # composite normalises by the weight of the factors actually present.
    "derivatives": 0.10,
    # Kronos is deliberately NOT a weighted voter by default. A raw forecast
    # vote let an unvalidated model flip signals, so it is used as a GATE /
    # size modifier instead (see apply_kronos_gate). Set KRONOS_FACTOR_WEIGHT>0
    # to also blend it into the composite (legacy behaviour).
    "kronos":      float(os.environ.get("KRONOS_FACTOR_WEIGHT", "0.0")),
}

# ── Kronos gate (filter, not vote) ──────────────────────────────
# When Kronos' forecast opposes the direction the rules-based composite chose,
# downgrade the action one step toward HOLD and cut position size. When it is
# merely neutral, do nothing. This lets Kronos veto/temper without being able
# to originate a trade.
KRONOS_GATE        = os.environ.get("KRONOS_GATE", "1").lower() not in ("0", "false", "no")
KRONOS_GATE_THRESH = float(os.environ.get("KRONOS_GATE_THRESH", "50"))
KRONOS_SIZE_FLOOR  = float(os.environ.get("KRONOS_SIZE_FLOOR", "0.5"))
# The gate score is 100*tanh(z), so a score of 50 is only |z| = 0.55 — less
# than one standard deviation, i.e. noise. On top of that the model's own
# hit rate is 52.9% (391 resolved forecasts), which is a coin flip. Letting a
# sub-one-sigma, coin-flip forecast veto a rules-based setup is not a gate, it
# is an outage generator: it held ETH (44%), SOL (44%) and ZEC (51%) at HOLD.
# The forecast must now also be a statistically significant move to veto.
KRONOS_GATE_MIN_Z  = float(os.environ.get("KRONOS_GATE_MIN_Z", "2.0"))

# One step toward HOLD, used when Kronos opposes the rules-based direction.
_DOWNGRADE = {
    "STRONG_BUY": "BUY", "BUY": "ACCUMULATE", "ACCUMULATE": "HOLD",
    "REDUCE": "HOLD", "SELL": "REDUCE", "STRONG_SELL": "SELL",
}
_LONG_ACTIONS  = ("STRONG_BUY", "BUY", "ACCUMULATE")
_SHORT_ACTIONS = ("STRONG_SELL", "SELL", "REDUCE")

# Contrarian zones for Fear & Greed (0-100)
FG_EXTREME_FEAR = 25
FG_FEAR         = 45
FG_GREED        = 65
FG_EXTREME_GREED= 80

RSI_OVERSOLD    = 30
RSI_OVERBOUGHT  = 70

# ── Data-quality gate ────────────────────────────────────────────────
# The composite used to be computed on whatever happened to be in the
# snapshot: three headlines, one dead feed, a stale news pull and a
# crowd read off two contracts all produced a clean-looking score. These
# thresholds are the floor below which a signal is not tradable evidence.
QUALITY_MIN_HEADLINES  = int(os.environ.get("TRADE_MIN_HEADLINES", "5"))
QUALITY_MIN_CONFIDENCE = float(os.environ.get("TRADE_MIN_SENTIMENT_CONF", "0.55"))
QUALITY_MAX_NEWS_AGE_S = int(os.environ.get("TRADE_MAX_NEWS_AGE_S", "5400"))
QUALITY_FEED_DEGRADED  = int(os.environ.get("TRADE_FEED_DEGRADED_COUNT", "3"))

# Flags split by what they mean for a trade.
#   blocking  — the evidence is missing or unusable; the signal must not be
#               traded on this run.
#   advisory  — the evidence exists but is weaker than ideal; the signal can
#               be traded, sized down.
# thin_polymarket is advisory because the crowd factor carries only 0.1
# weight and is already confidence-discounted (x0.3, capped at +/-30) by the
# time it reaches the composite. Blocking a trade because the *auxiliary*
# crowd read is thin would throw away a signal the headline and momentum
# factors earned.
QUALITY_BLOCKING_FLAGS = {
    "no_news", "thin_news", "no_sentiment", "sentiment_confidence_unknown",
    "low_sentiment_confidence", "stale_news", "feed_degradation",
    "no_factors",
}
QUALITY_ADVISORY_FLAGS = {
    "news_age_unknown", "thin_polymarket", "weak_proxy_news",
}

# The FRED macro backdrop is asset-specific: each asset inherits ONLY the macro
# signal calibrated for it (gold keys off real yields / safe-haven demand, oil
# off the dollar / inflation / growth backdrop). A gold headwind is not an oil
# headwind, so an asset must never borrow another's signal — define a new signal
# in fetch_macro.py and register its key here. Keep this list tight.
MACRO_SIGNAL_KEYS = {
    "GC=F": "gold_macro_signal",
    "BZ=F": "oil_macro_signal",
    # Equities inherit the CPI/rates tilt (see fetch_macro.equity_macro_signal)
    # rather than borrowing gold's real-yield read.
    "NVDA": "equity_macro_signal",
    "META": "equity_macro_signal",
    "AMD":  "equity_macro_signal",
}
MACRO_LINKED_ASSETS = set(MACRO_SIGNAL_KEYS)

ACTION_MAP = [
    ( 50, "STRONG_BUY"),
    ( 20, "BUY"),
    (  8, "ACCUMULATE"),
    ( -8, "HOLD"),
    (-20, "REDUCE"),
    (-50, "SELL"),
    (-101, "STRONG_SELL"),
]

# Staleness must scale with the bar size. A daily candle stamped 00:00 UTC is
# still the current bar all day; flagging it stale at 45 minutes marked every
# gold/crude/DXY snapshot as stale every morning. A couple of missed bars is
# the honest signal that a fetch has stopped.
STALE_AFTER_BY_INTERVAL = {
    "5m":  30 * 60,
    "15m": 90 * 60,
    "1h":  150 * 60,
    "4h":  600 * 60,
    "1d":  36 * 3600,
}
DEFAULT_STALE_AFTER = 150 * 60


def stale_after(interval):
    return STALE_AFTER_BY_INTERVAL.get(interval, DEFAULT_STALE_AFTER)


def compute_freshness(data_as_of, interval="1h", now=None):
    """Age a snapshot at READ time so it degrades as it sits on disk."""
    from datetime import datetime as _dt, timezone as _tz
    limit = stale_after(interval)
    if not data_as_of:
        return {"data_as_of": None, "age_seconds": None, "is_stale": None,
                "stale_after_seconds": limit}
    try:
        ts = pd.to_datetime(data_as_of)
        if ts.tzinfo is None:
            ts = ts.tz_localize("utc")
        now = now or _dt.now(_tz.utc)
        age = int((now - ts).total_seconds())
    except Exception:
        return {"data_as_of": data_as_of, "age_seconds": None, "is_stale": None,
                "stale_after_seconds": limit}
    return {"data_as_of": data_as_of, "age_seconds": age,
            "is_stale": bool(age > limit), "stale_after_seconds": limit}

# Position budget. Full-margin mode: the whole allocation is committed as
# margin on every actionable signal — the allocation itself is the risk
# budget, so stop distance no longer scales the fraction down.
MAX_POSITION_FRACTION  = 1.00   # 100% of allocated capital


# ─────────────────────────────────────────────────────────────
# SCORING PRIMITIVES
# ─────────────────────────────────────────────────────────────
# Fear & Greed bands. Each entry is
#   (value_lo, value_hi, score_at_lo, score_at_hi)
# and the score is linearly interpolated inside the band, so there are no
# cliffs at the boundaries.
#
# The bands mirror the index's own published classification (Alternative
# Me's Extreme Fear / Fear / Neutral / Greed / Extreme Greed). The middle of
# the scale carries no contrarian edge: at 63 ("Greed") the old linear map
# still handed the composite -26, which is a -7.8 drag on every crypto
# signal regardless of the asset, the news or the tape. That constant drag is
# what pinned the crypto book to HOLD.
FG_BANDS = [
    (0,   25,  100.0,  70.0),   # extreme fear -> strongly bullish
    (25,  45,   70.0,  30.0),   # fear        -> bullish
    (45,  56,    0.0,   0.0),   # neutral     -> no contrarian edge
    (56,  75,    0.0, -25.0),   # greed       -> bearish
    (75,  100, -25.0, -55.0),   # extreme greed -> strongly bearish
]


def score_fear_greed(fg):
    """
    Contrarian score from the Fear & Greed index, banded instead of linear.

    Extreme Fear => bullish setup (buy fear), Extreme Greed => bearish setup
    (sell greed), and the neutral middle of the scale contributes nothing
    instead of a constant drag. Returns score in [-100, +100] or None.
    """
    if not fg:
        return None
    try:
        value = float(fg["value"])
    except (KeyError, TypeError, ValueError):
        return None

    value = max(0.0, min(100.0, value))
    for lo, hi, s_lo, s_hi in FG_BANDS:
        if lo <= value <= hi:
            if hi <= lo:
                return round(s_lo, 1)
            score = s_lo + (s_hi - s_lo) * (value - lo) / (hi - lo)
            return round(score, 1)
    return round(100.0 - 2 * value, 1)


# Positioning score from the perp tape. Anchored on the annualised funding
# rate, which is the cleanest read on how stretched one side of the book is.
# (annualised funding %, score) anchors in ascending order:
#   <-50%  +60 (crowded short) ... 0 -> 0 ... +50%  -60 (crowded long)
DERIV_FUNDING_BANDS = [
    (-50.0,  60.0),   # crowded short -> bullish tilt
    (-25.0,  30.0),
    (-10.0,  12.0),
    (10.0,  -12.0),
    (25.0,  -30.0),
    (50.0,  -60.0),   # crowded long -> bearish tilt
]


def _interp_bands(value, bands):
    """Linear interpolation through ascending (threshold, score) anchors."""
    if value is None:
        return None
    value = float(value)
    if value <= bands[0][0]:
        return bands[0][1]
    if value >= bands[-1][0]:
        return bands[-1][1]
    for (thr_lo, score_lo), (thr_hi, score_hi) in zip(bands, bands[1:]):
        if thr_lo <= value <= thr_hi:
            if thr_hi == thr_lo:
                return score_hi
            ratio = (value - thr_lo) / (thr_hi - thr_lo)
            return score_lo + (score_hi - score_lo) * ratio
    return 0.0


def score_derivatives(deriv):
    """
    Positioning score from derivatives context (funding + basis).

    Positive funding means longs are paying shorts to stay in the trade: the
    perp is rich and the long side is crowded, which is a contrarian bearish
    tilt. Negative funding is the mirror image. Basis (mark vs index) says the
    same thing across the settlement window, so it is blended in as a second,
    slower confirmation.

    Returns score in [-100, +100] or None when there is no positioning data.
    """
    if not deriv:
        return None

    funding = deriv.get("funding_rate_annualized_pct")
    if funding is None:
        return None
    try:
        funding = float(funding)
    except (TypeError, ValueError):
        return None

    funding_score = _interp_bands(funding, DERIV_FUNDING_BANDS)
    if funding_score is None:
        return None

    basis_pct = deriv.get("basis_pct")
    basis_score = None
    if basis_pct is not None:
        try:
            basis_score = _interp_bands(float(basis_pct) * 12.0, DERIV_FUNDING_BANDS)
        except (TypeError, ValueError):
            basis_score = None

    score = funding_score if basis_score is None else 0.7 * funding_score + 0.3 * basis_score
    return round(max(-100.0, min(100.0, score)), 1)


def score_news(sentiment_counts):
    """
    Directional news score from pos/neg/neu tallies.
    Returns [-100, +100] using a net-score that de-weights neutral.
    """
    if not sentiment_counts:
        return None
    pos = int(sentiment_counts.get("positive", 0))
    neg = int(sentiment_counts.get("negative", 0))
    neu = int(sentiment_counts.get("neutral", 0))
    total = pos + neg + neu
    if total == 0:
        return None
    net = (pos - neg) / total          # -1..+1
    # Mildly discount when the sample is dominated by neutral headlines
    clarity = 1.0 - 0.5 * (neu / total)
    return round(100 * net * clarity, 1)


def compute_atr(df_close, df_high, df_low, period=14):
    """Average True Range — volatility measure for risk sizing."""
    high = np.asarray(df_high, dtype=float)
    low  = np.asarray(df_low,  dtype=float)
    close = np.asarray(df_close, dtype=float)
    if len(close) < period + 1:
        return float(close[-1] * 0.02) if len(close) else None
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low,
         np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return float(np.mean(tr[-period:]))


def compute_rsi(closes, period=14):
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period + 1:
        return 50.0
    delta = np.diff(closes)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gain[-period:])
    avg_loss = np.mean(loss[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def score_momentum(price_data, interval="1h", daily_closes=None):
    """
    Momentum score from price_data. Combines a trend component (SMA cross /
    recent return) and a mean-reversion component (RSI).
    Returns (score, meta) where meta carries rsi, trend, atr for later use.

    TIMEFRAME CONTRACT
    ------------------
    `interval` is the real bar size of price_data. Every window below is
    expressed in BARS and labelled as such in meta, because the previous code
    emitted `ret5_pct` from 5 bars of 1h data and downstream consumers read it
    as a 5-DAY return ("trend UP +1.23% 5d" when it was +1.23% over 5 hours).

    RSI is the second trap. RSI_OVERBOUGHT/OVERSOLD (70/30) are DAILY
    conventions; applying them to 14 hourly bars produced 68.7 on a chart whose
    true daily RSI was 62.1, i.e. a fake overbought reading. When daily_closes
    are supplied we use them for the RSI leg so the threshold means what it
    says, and keep hourly bars for the trend and ATR legs.
    """
    if not price_data:
        return None, None
    closes = [c["close"] for c in price_data]
    highs  = [c["high"]  for c in price_data]
    lows   = [c["low"]   for c in price_data]
    closes = pd.Series(closes).astype(float)
    highs  = np.asarray(highs, dtype=float)
    lows   = np.asarray(lows,  dtype=float)

    # RSI on the timeframe the thresholds are defined for.
    if daily_closes and len(daily_closes) >= 16:
        rsi = compute_rsi(list(daily_closes))
        rsi_source = "1d"
    else:
        rsi = compute_rsi(closes.tolist())
        rsi_source = f"{interval}(thresholds are daily-convention)"

    # Trend: 20 vs 50 period SMA (use what's available)
    n = len(closes)
    if n >= 20:
        sma_fast = float(closes[-20:].mean())
    else:
        sma_fast = float(closes.mean())
    if n >= 50:
        sma_slow = float(closes[-50:].mean())
    else:
        sma_slow = float(closes[:max(1, n//2)].mean())

    trend = 1.0 if closes.iloc[-1] > sma_fast > sma_slow else \
           -1.0 if closes.iloc[-1] < sma_fast < sma_slow else 0.0
    trend_mag = abs((closes.iloc[-1] / sma_slow) - 1) if sma_slow else 0.0

    # Returns, expressed in BARS and labelled honestly. `ret_5bar` is what the
    # old `ret5_pct` actually was; `ret_5d` is the number people meant.
    ret_5bar = (closes.iloc[-1] / closes.iloc[max(0, n-6)]) - 1 if n > 1 else 0.0

    daily = pd.Series(daily_closes, dtype=float) if daily_closes else None
    if daily is not None and len(daily) >= 6:
        ret_5d = (daily.iloc[-1] / daily.iloc[-6]) - 1
        ret_5d_source = "1d"
    else:
        ret_5d = None
        ret_5d_source = None

    # Trend component (-100..+100), scaled by trend magnitude
    trend_score = trend * min(100, trend_mag * 2000)

    # RSI mean-reversion: oversold (bullish setup) / overbought (bearish)
    rsi_score = 0.0
    if rsi <= RSI_OVERSOLD:
        rsi_score = 100 * (RSI_OVERSOLD - rsi) / RSI_OVERSOLD      # +0..+100
    elif rsi >= RSI_OVERBOUGHT:
        rsi_score = -100 * (rsi - RSI_OVERBOUGHT) / (100 - RSI_OVERBOUGHT)  # -0..-100

    score = 0.60 * trend_score + 0.40 * rsi_score
    atr = compute_atr(closes.tolist(), highs, lows)

    meta = {
        "interval":         interval,
        "rsi":              round(rsi, 1),
        "rsi_source":       rsi_source,
        "trend":            ("UP" if trend == 1 else "DOWN" if trend == -1 else "FLAT"),
        # Kept for backwards compatibility with existing consumers/dashboards,
        # but it is 5 BARS not 5 days. New code should read ret_5bar/ret_5d.
        "ret5_pct":         round(ret_5bar * 100, 2),
        "ret5_pct_window":  f"5 bars of {interval}",
        "ret_5bar":         round(ret_5bar * 100, 2),
        "ret_5d":           round(ret_5d * 100, 2) if ret_5d is not None else None,
        "ret_5d_source":    ret_5d_source,
        "atr":              round(atr, 4) if atr else None,
        "close":            float(closes.iloc[-1]),
    }
    return round(np.clip(score, -100, 100), 1), meta


def score_macro(macro, asset=None):
    """
    Macro risk-score for `asset`. Inverted yield curve and high real yields are
    risk-off (negative). The asset-specific signal (gold vs oil, per
    MACRO_SIGNAL_KEYS) adds the calibrated directional tilt: bullish -> +20,
    bearish -> -20. Returns [-100, +100].
    """
    if not macro:
        return None
    score = 0.0
    ycs = macro.get("yield_curve_status")
    if ycs == "INVERTED":
        score -= 50.0
    elif ycs == "FLATTENING":
        score -= 15.0
    elif ycs == "STEEPENING":
        score += 10.0

    signal_key = MACRO_SIGNAL_KEYS.get(asset, "gold_macro_signal")
    asset_sig = (macro.get(signal_key) or "").upper()
    if "BULLISH" in asset_sig:
        score += 20.0
    elif "BEARISH" in asset_sig:
        score -= 20.0
    return round(np.clip(score, -100, 100), 1)


def score_polymarket(poly):
    """
    Directional crowd score from Polymarket.

    Prefers the strike-relative crowd score: the gap between the probability
    the crowd assigns and the probability the strike's distance from spot
    implies. A raw YES probability is not a sentiment reading — "BTC above
    $90k" at 4% YES is a statement about the strike, not about the crowd —
    and averaging raw probabilities across strikes produced a -99
    "STRONGLY_BEARISH" reading off three far-OTM contracts.

    Falls back to the legacy probability mapping for snapshots written before
    the strike-relative score existed.
    Returns [-100, +100] or None.
    """
    if not poly or poly.get("signal") == "UNKNOWN":
        return None

    crowd_score = poly.get("crowd_score")
    if crowd_score is not None:
        try:
            return round(float(crowd_score), 1)
        except (TypeError, ValueError):
            pass

    prob = poly.get("avg_prob_yes")
    if prob is None:
        sig = poly.get("signal", "").upper()
        return {"STRONGLY_BEARISH": -80, "BEARISH": -50,
                "BULLISH": 50, "STRONGLY_BULLISH": 80,
                "NEUTRAL": 0}.get(sig, 0)
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    return round(np.clip((p - 0.5) * 200, -100, 100), 1)


# ─────────────────────────────────────────────────────────────
# POSITION / LEVELS
# ─────────────────────────────────────────────────────────────
def build_levels(action, close, atr, asset_type):
    """
    Derive entry, stop-loss and take-profit from a summary action and ATR.
    Conservative: stop uses ~1.5x ATR, targets scale with risk:reward 2:1 / 3:1.
    Returns dict of levels priced in the asset's units.
    """
    if close is None or atr is None or atr <= 0:
        atr = (close or 1) * 0.02
    stop_width = 1.5 * atr
    if action in ("STRONG_BUY", "BUY", "ACCUMULATE"):
        entry = round(close, 4)
        stop  = round(close - stop_width, 4)
        tp1   = round(close + 2  * stop_width, 4)
        tp2   = round(close + 3  * stop_width, 4)
        bias  = "LONG"
    elif action in ("STRONG_SELL", "SELL", "REDUCE"):
        entry = round(close, 4)
        stop  = round(close + stop_width, 4)
        tp1   = round(close - 2  * stop_width, 4)
        tp2   = round(close - 3  * stop_width, 4)
        bias  = "SHORT"
    else:
        return {
            "bias": "FLAT",
            "note": "No active setup — stand aside.",
        }
    return {
        "bias":       bias,
        "entry":      entry,
        "stop_loss":  stop,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "risk_reward_1": 2.0,
        "risk_reward_2": 3.0,
    }


def position_size(close, stop, max_frac=MAX_POSITION_FRACTION):
    """
    Full-margin sizing: commit max_frac (100%) of the allocated capital as
    margin. Returns None when levels are invalid so HOLD keeps emitting no
    size. Risk is expressed by the stop itself, not by a scaled fraction.
    """
    if not close or not stop or close <= 0:
        return None
    if abs(close - stop) <= 0:
        return None
    return round(max_frac, 4)


# ─────────────────────────────────────────────────────────────
# MASTER SIGNAL EVALUATOR
# ─────────────────────────────────────────────────────────────
def _news_age_seconds(snapshot, now=None):
    """Age of the news layer in seconds, or None when it is unknown."""
    stamp = snapshot.get("news_fetched_at")
    if not stamp:
        return None
    try:
        fetched = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    now = now or datetime.now(timezone.utc)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return (now - fetched).total_seconds()


def assess_data_quality(snapshot, factors_present=None):
    """
    Judge whether the evidence behind a signal is good enough to trade on.

    Returns dict with ok (bool), flags (list), and the inputs the trader
    needs to scale exposure. Missing evidence is reported as a flag rather
    than silently scored as neutral: a factor that is absent is not the
    same as a factor that says "nothing".
    """
    snapshot = snapshot or {}
    flags = []

    headlines = snapshot.get("news_headlines") or []
    n_headlines = len(headlines)
    if n_headlines == 0:
        flags.append("no_news")
    elif n_headlines < QUALITY_MIN_HEADLINES:
        flags.append("thin_news")

    sources_hit = snapshot.get("news_sources_hit") or []
    sources_failed = snapshot.get("news_sources_failed") or []
    if len(sources_failed) >= QUALITY_FEED_DEGRADED:
        flags.append("feed_degradation")

    counts = snapshot.get("sentiment_counts") or {}
    if not counts:
        flags.append("no_sentiment")

    confidence = snapshot.get("avg_confidence")
    if confidence is None:
        flags.append("sentiment_confidence_unknown")
    else:
        try:
            if float(confidence) < QUALITY_MIN_CONFIDENCE:
                flags.append("low_sentiment_confidence")
        except (TypeError, ValueError):
            flags.append("sentiment_confidence_unknown")

    scope = snapshot.get("news_scope")
    if scope == "market_wide":
        flags.append("weak_proxy_news")

    age = _news_age_seconds(snapshot)
    if age is None:
        flags.append("news_age_unknown")
    elif age > QUALITY_MAX_NEWS_AGE_S:
        flags.append("stale_news")

    poly = snapshot.get("polymarket_signals") or {}
    poly_conf = poly.get("confidence")
    if poly.get("signal") not in (None, "UNKNOWN") and poly_conf == "low":
        flags.append("thin_polymarket")

    if not factors_present:
        flags.append("no_factors")

    blocking = [f for f in flags if f in QUALITY_BLOCKING_FLAGS]
    advisory = [f for f in flags if f in QUALITY_ADVISORY_FLAGS]
    unknown = [f for f in flags
               if f not in QUALITY_BLOCKING_FLAGS and f not in QUALITY_ADVISORY_FLAGS]

    return {
        "ok": not blocking,
        "flags": flags,
        "blocking": blocking,
        "advisory": advisory,
        "unknown": unknown,
        "headline_count": n_headlines,
        "sources_hit": len(sources_hit),
        "sources_failed": len(sources_failed),
        "news_scope": scope,
        "avg_confidence": confidence,
        "news_age_seconds": round(age, 1) if age is not None else None,
        "polymarket_confidence": poly_conf,
        "factors_present": list(factors_present or []),
    }


def evaluate_snapshot(snapshot):
    """
    Turn a single snapshot dict into an actionable signal.
    Expects the shape produced by build_snapshot() (see build_snapshot.py).
    """
    asset      = snapshot.get("asset")
    label      = snapshot.get("label", asset)
    asset_type = snapshot.get("asset_type")
    close      = snapshot.get("current_price")
    price_data = snapshot.get("price_data") or []

    if close is None or not price_data:
        return {
            "asset":      asset,
            "label":      label,
            "asset_type": asset_type,
            "composite_score": None,
            "action":     "NO_DATA",
            "factors":    [],
            "factor_meta": {},
            "current_price": close,
            "levels":     {},
            "position_size_frac": None,
            "rationale":  f"{label}: no price data — signal skipped",
            "errors":     snapshot.get("errors", []),
        }

    factors = []
    meta = {}

    # 1. Fear & Greed (contrarian, crypto only)
    fg_score = score_fear_greed(snapshot.get("fear_greed")) if asset_type == "crypto" else None
    if fg_score is not None:
        factors.append(("fear_greed", WEIGHTS["fear_greed"], fg_score))
        fg = snapshot.get("fear_greed") or {}
        meta["fear_greed"] = {
            "value": fg.get("value"),
            "classification": fg.get("classification"),
            "contrarian_score": fg_score,
        }

    # 2. News sentiment (directional)
    news_score = score_news(snapshot.get("sentiment_counts"))
    if news_score is not None:
        factors.append(("news", WEIGHTS["news"], news_score))
        meta["news"] = {
            "counts": snapshot.get("sentiment_counts"),
            "overall": snapshot.get("overall_sentiment"),
            "dir_score": news_score,
            # "asset_specific" means the headlines actually named this asset.
            # "market_wide" means we fell back to a correlated-but-not-direct
            # proxy, which deserves to be visible next to the score.
            "scope": snapshot.get("news_scope"),
            "scope_note": snapshot.get("news_scope_note"),
        }

    # 3. Momentum (trend + RSI)
    mom_score, mom_meta = score_momentum(
        price_data,
        interval=snapshot.get("candle_interval", "1h"),
        daily_closes=snapshot.get("daily_closes"),
    )
    if mom_score is not None:
        factors.append(("momentum", WEIGHTS["momentum"], mom_score))
        meta["momentum"] = mom_meta
        close = mom_meta.get("close", close)

    # 4. Macro backdrop (asset-specific signal — see MACRO_SIGNAL_KEYS)
    if asset in MACRO_LINKED_ASSETS:
        macro_score = score_macro(snapshot.get("macro_context"), asset)
        if macro_score is not None:
            factors.append(("macro", WEIGHTS["macro"], macro_score))
            meta["macro"] = {"score": macro_score}

    # 5. Polymarket crowd
    poly_score = score_polymarket(snapshot.get("polymarket_signals"))
    if poly_score is not None:
        factors.append(("polymarket", WEIGHTS["polymarket"], poly_score))
        meta["polymarket"] = {
            "signal": snapshot.get("polymarket_signals", {}).get("signal"),
            "score": poly_score,
        }

    # 6. Derivatives positioning (funding + basis + open interest).
    # Only present when the venue tape was reachable; a missing funding read
    # drops the factor instead of defaulting it to neutral, so the composite
    # never pretends to have a positioning view it does not have.
    deriv = snapshot.get("derivatives") or {}
    deriv_score = score_derivatives(deriv)
    if deriv_score is not None:
        factors.append(("derivatives", WEIGHTS["derivatives"], deriv_score))
        meta["derivatives"] = {
            "score": deriv_score,
            "venue": deriv.get("venue"),
            "funding_rate_annualized_pct": deriv.get("funding_rate_annualized_pct"),
            "basis_pct": deriv.get("basis_pct"),
            "open_interest_usd": deriv.get("open_interest_usd"),
            "open_interest_change_pct_24h": deriv.get("open_interest_change_pct_24h"),
            "price_change_pct_24h": deriv.get("price_change_pct_24h"),
            "quote_volume_24h": deriv.get("quote_volume_24h"),
            "note": ("positive funding = longs pay shorts = crowded long = "
                     "contrarian bearish tilt"),
        }

    # 7. Kronos foundation-model forecast (isolated layer writes this block).
    # Captured for display + gating regardless of weight; only added to the
    # weighted composite when KRONOS_FACTOR_WEIGHT > 0.
    kf = snapshot.get("kronos_forecast") or {}
    kronos_score = float(kf["score"]) if kf.get("score") is not None else None
    if kronos_score is not None:
        if WEIGHTS.get("kronos"):
            factors.append(("kronos", WEIGHTS["kronos"], kronos_score))
        meta["kronos"] = {
            "model": kf.get("model"),
            "direction": kf.get("direction"),
            "expected_return_pct": kf.get("expected_return_pct"),
            "forecast_close": kf.get("forecast_close"),
            "pred_len": kf.get("pred_len"),
            "z": kf.get("z"),
            "score": kronos_score,
            "weighted": bool(WEIGHTS.get("kronos")),
        }

    # ── BTC context (does NOT feed the composite) ─────────────────
    # SOL tracks BTC hard (measured corr ~0.82, beta ~1.2), so knowing BTC's
    # own read is genuinely useful context for an alt. It is surfaced here and
    # deliberately kept out of `factors` so the composite stays reproducible.
    # BTC's Polymarket read is gated on confidence: with only a couple of
    # usable markets it is noise, and importing it would push SOL around on
    # thin evidence.
    btc_ctx = snapshot.get("btc_context") or {}
    btc_poly = btc_ctx.get("polymarket") or {}
    btc_poly_conf = btc_poly.get("confidence")
    btc_poly_ok = btc_poly_conf in ("high", "medium")
    # Omitted entirely for BTC itself and for non-crypto assets, so the
    # absence of a block means "not applicable" rather than "gate failed".
    if btc_poly or snapshot.get("btc_beta"):
        meta["btc_context"] = {
        "corr": (snapshot.get("btc_beta") or {}).get("corr"),
        "beta": (snapshot.get("btc_beta") or {}).get("beta"),
        "bars": (snapshot.get("btc_beta") or {}).get("bars"),
        "btc_polymarket": btc_poly.get("signal"),
        "btc_polymarket_confidence": btc_poly_conf,
        # Explicitly gated rather than silently omitted.
        "btc_polymarket_informative": btc_poly_ok,
        "btc_polymarket_note": (
            None if not btc_poly else
            (f"BTC crowd read is {btc_poly_conf} confidence "
             f"({btc_poly.get('markets_used', '?')} usable markets); "
             + ("usable as context for this asset" if btc_poly_ok
                else "too thin to lean on")
             if btc_poly_conf != "high" or not btc_poly_ok else "high confidence")
        ),
    }

    # Composite weighted score
    total = 0.0
    weight_sum = 0.0
    for _, w, s in factors:
        total += w * s
        weight_sum += w
    composite = round(total / weight_sum, 1) if weight_sum else 0.0

    # Map to action
    action = "HOLD"
    for threshold, label_ in ACTION_MAP:
        if composite >= threshold:
            action = label_
            break

    # ── Kronos gate (filter, not vote) ─────────────────────────
    # If Kronos strongly opposes the rules-based direction, temper the trade:
    # step the action toward HOLD and cut size. Agreement/neutrality = no change.
    gate = None
    if KRONOS_GATE and kronos_score is not None:
        side = ("LONG" if action in _LONG_ACTIONS
                else "SHORT" if action in _SHORT_ACTIONS else None)
        if side:
            kf_z = kf.get("z")
            try:
                kf_z = float(kf_z) if kf_z is not None else None
            except (TypeError, ValueError):
                kf_z = None
            # The gate score is 100*tanh(z), so the old score threshold of 50
            # fired at |z| = 0.55 — noise. The forecast now has to be a
            # statistically significant move (|z| >= KRONOS_GATE_MIN_Z) before
            # it is allowed to veto a rules-based setup.
            significant = kf_z is not None and abs(kf_z) >= KRONOS_GATE_MIN_Z
            directional = ((side == "LONG" and kronos_score <= -KRONOS_GATE_THRESH)
                           or (side == "SHORT" and kronos_score >= KRONOS_GATE_THRESH))
            opposes = bool(directional and significant)
            gate = {
                "side": side,
                "kronos_score": kronos_score,
                "kronos_z": kf_z,
                "z_significant": significant,
                "min_z": KRONOS_GATE_MIN_Z,
                "directional": directional,
                "opposed": opposes,
                "action_before": action,
                "action_after": _DOWNGRADE[action] if opposes else action,
                "size_mult": KRONOS_SIZE_FLOOR if opposes else 1.0,
            }
            if opposes:
                action = _DOWNGRADE[action]
            elif directional and not significant:
                gate["note"] = (f"opposing forecast ignored: |z|="
                                f"{abs(kf_z):.2f} < {KRONOS_GATE_MIN_Z}")
        if gate:
            meta["kronos"]["gate"] = gate

    # Levels + sizing
    atr = (mom_meta or {}).get("atr")
    levels = build_levels(action, close, atr, asset_type)
    size = None
    if levels.get("bias") in ("LONG", "SHORT"):
        size = position_size(levels["entry"], levels["stop_loss"])
        if gate and gate["opposed"] and size:
            size = round(size * KRONOS_SIZE_FLOOR, 4)

    # Rationale text
    rationale = build_rationale(asset, action, composite, factors, meta, levels, size)

    # MEXC execution-venue reference (gold/Brent): report the basis and shift
    # the futures levels into MEXC price terms so the actionable numbers match
    # where the order actually fills.
    mexc_ref = snapshot.get("mexc_reference") or {}
    mexc_levels = None
    if (mexc_ref.get("basis_vs_futures") is not None
            and levels.get("bias") in ("LONG", "SHORT")):
        shift = mexc_ref["basis_vs_futures"]
        mexc_levels = {"bias": levels.get("bias")}
        for key in ("entry", "stop_loss", "take_profit_1", "take_profit_2"):
            if isinstance(levels.get(key), (int, float)):
                mexc_levels[key] = round(levels[key] + shift, 6)
        mexc_levels["shift"] = round(shift, 4)
        mexc_levels["note"] = "futures levels shifted by MEXC basis (absolute)"

    quality = assess_data_quality(snapshot, [f for f, _, _ in factors])

    return {
        "asset":      asset,
        "label":      label,
        "asset_type": asset_type,
        "composite_score": composite,
        "action":     action,
        "factors":    [{"factor": f, "weight": w, "score": s} for f, w, s in factors],
        "factor_meta": meta,
        "data_quality": quality,
        "kronos_gate": gate,
        "btc_beta":    (snapshot.get("btc_beta") or {}).get("beta"),
        "btc_corr":    (snapshot.get("btc_beta") or {}).get("corr"),
        "current_price": close,
        "levels":     levels,
        "mexc_reference": mexc_ref or None,
        "mexc_levels": mexc_levels,
        "position_size_frac": size,
        "rationale":  rationale,
        # Freshness travels with the signal, recomputed at read time so a
        # snapshot that was fresh when written still reports stale later.
        **compute_freshness(snapshot.get("data_as_of"),
                            interval=snapshot.get("candle_interval", "1h")),
        "weights_used": {
            "factors_present": [f for f, _, _ in factors],
            "factors_missing": [
                k for k in WEIGHTS
                if not any(f == k for f, _, _ in factors)
                and not (k == "fear_greed" and asset_type != "crypto")
                and not (k == "macro" and not snapshot.get("macro_context"))
                # Kronos is intentionally unweighted unless KRONOS_FACTOR_WEIGHT>0.
                and not (k == "kronos" and not WEIGHTS.get("kronos"))
            ],
            "weight_sum":  round(weight_sum, 4),
            "normalised":  weight_sum != 1.0,
        },
    }


def build_rationale(asset, action, composite, factors, meta, levels, size=None):
    lines = [f"{asset}: {action} (composite {composite:+})"]
    fmap = {f: s for f, _, s in factors}
    if "fear_greed" in fmap:
        fg = meta.get("fear_greed", {})
        lines.append(f"  Fear&Greed {fg.get('value')} ({fg.get('classification')}) "
                     f"-> contrarian {fmap['fear_greed']:+.0f}")
    m_beta = (meta.get("btc_context") or {}).get("beta")
    # Keep this numeric: an earlier string cast here broke the .2f format and
    # made evaluate_snapshot raise for every asset.
    if isinstance(m_beta, (int, float)):
        m_beta_txt = f"{m_beta:+.2f}"
    else:
        m_beta_txt = "n/a"
    if "news" in fmap:
        n = meta.get("news", {})
        c = n.get("counts", {})
        lines.append(f"  News {c.get('positive',0)}P/{c.get('negative',0)}N/{c.get('neutral',0)}Neu "
                     f"-> {n.get('overall')} ({fmap['news']:+.0f})"
                     + ({"market_beta": f" [BTC-BETA {m_beta_txt}]",
                         "market_wide": " [MARKET-WIDE PROXY]"}.get(n.get("scope"), "")))
    if "momentum" in fmap:
        m = meta.get("momentum", {})
        ret5d = m.get("ret_5d")
        ret5d_txt = (f"5d {ret5d:+.2f}%"
                     if ret5d is not None
                     else f"5 bars of {m.get('interval','?')} "
                          f"{m.get('ret_5bar', 0):+.2f}% (no daily bars)")
        lines.append(f"  Momentum {m.get('trend')}: RSI {m.get('rsi')} "
                     f"[{m.get('rsi_source','?')}], {ret5d_txt} "
                     f"(score {fmap['momentum']:+.0f})")
    if "macro" in fmap:
        macro_dir = ("tailwind" if fmap["macro"] > 0 else
                     "headwind" if fmap["macro"] < 0 else "neutral")
        lines.append(f"  Macro {macro_dir} {fmap['macro']:+.0f}")
    if "polymarket" in fmap:
        p = meta.get("polymarket", {})
        lines.append(f"  Polymarket {p.get('signal')} (score {fmap['polymarket']:+.0f})")
    if meta.get("kronos"):
        k = meta.get("kronos", {})
        exp = k.get("expected_return_pct")
        exp_txt = f"{exp:+.2f}%" if isinstance(exp, (int, float)) else "n/a"
        tag = "" if k.get("weighted") else " [not weighted]"
        lines.append(f"  Kronos {k.get('model','?')} {k.get('direction')} "
                     f"{exp_txt} over {k.get('pred_len','?')} bars "
                     f"(score {k.get('score'):+.0f}){tag}")
        g = k.get("gate")
        if g and g.get("opposed"):
            lines.append(f"  Kronos GATE: opposes {g['side']} -> action "
                         f"{g['action_before']}->{g['action_after']}, "
                         f"size x{g['size_mult']}")
        elif g:
            lines.append(f"  Kronos GATE: agrees with {g['side']} — no change")
    size = size if size is not None else None
    if levels.get("bias") in ("LONG", "SHORT"):
        lines.append(f"  {levels['bias']} @ {levels['entry']} | SL {levels['stop_loss']} "
                     f"| TP1 {levels['take_profit_1']} / TP2 {levels['take_profit_2']}"
                     + (f" | size {size*100:.1f}% acct" if size else ""))
    else:
        lines.append(f"  {levels.get('note','Stand aside.')}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# ENTRY/EXIT helpers on top of the raw score
# ─────────────────────────────────────────────────────────────
def actionable_entries(snapshots):
    """
    Convenience wrapper: evaluate a list of snapshots and return only the
    actionable ones (anything stronger than HOLD) plus a short trade plan.
    """
    signals = [evaluate_snapshot(s) for s in snapshots]
    active = [s for s in signals if s["action"] not in ("HOLD",)]
    return active, signals


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    import glob

    print("=" * 70)
    print("  ENTRY/EXIT SIGNAL ENGINE — scan of current snapshots")
    print("=" * 70)

    for f in sorted(glob.glob("snapshot_*.json"), key=lambda x: x):
        if "summary" in f:
            continue
        with open(f) as fh:
            snap = json.load(fh)
        sig = evaluate_snapshot(snap)
        print()
        print(f"  {sig['label']:<14} {sig['action']:<14} "
              f"score {sig['composite_score']:+.1f}  @ {sig['current_price']}")
        for line in sig["rationale"].splitlines()[1:]:
            print("    " + line)
    print("\n" + "=" * 70)

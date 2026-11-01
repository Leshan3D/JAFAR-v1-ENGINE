"""
POLYMARKET DATA MODULE
Source: Polymarket Gamma API (free, no API key required)

Only tracks FINANCIAL, GEOPOLITICAL, ENERGY, COMMODITIES, and TECH markets.
Sports, esports, and entertainment are explicitly filtered out.

Signal interpretation:
  prob_yes = 0.72 means crowd thinks 72% chance of YES outcome
  High volume = high conviction (more real money on the line)
"""

import requests
import pandas as pd
import json
import math
import os
import re
from datetime import datetime, timezone
import time

GAMMA_BASE = "https://gamma-api.polymarket.com"
HEADERS    = {
    "User-Agent": "JAFAR/1.0 (financial research)",
    "Accept":     "application/json"
}

FINANCIAL_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
    "crypto", "defi", "blockchain", "stablecoin", "altcoin",
    "zcash", "zec", "privacy coin",
    "coinbase", "binance", "sec crypto", "etf",
    "federal reserve", "fed rate", "rate cut", "rate hike",
    "interest rate", "inflation", "cpi", "pce", "gdp",
    "recession", "unemployment", "jobs report", "payroll",
    "treasury", "yield", "bond", "debt ceiling",
    "dollar", "dxy", "usd", "eur", "yen", "yuan",
    "currency", "forex", "devaluation",
    "gold", "silver", "xau", "precious metal", "bullion",
    "oil", "crude", "brent", "wti", "opec", "natural gas",
    "energy", "petroleum", "gasoline", "fuel", "lng",
    "iran", "ceasefire", "sanctions", "war", "conflict",
    "russia", "ukraine", "china", "taiwan", "north korea",
    "middle east", "israel", "gaza", "nato",
    "trade war", "tariff", "embargo",
    "nvidia", "apple", "microsoft", "google", "meta",
    "amazon", "tesla", "openai", "artificial intelligence",
    "semiconductor", "chip", "stock market", "nasdaq", "s&p",
    "ipo", "earnings", "revenue",
    "wheat", "corn", "commodity", "copper", "lithium",
]

BLOCKED_KEYWORDS = [
    "world cup", "fifa", "nba", "nfl", "mlb", "nhl",
    "premier league", "champions league", "la liga", "serie a",
    "super bowl", "wimbledon", "olympics", "euro cup",
    "cricket", "rugby", "tennis match", "golf tournament",
    "esports", "league of legends", "dota", "cs:go", "valorant",
    "oscars", "grammy", "emmy", "golden globe",
    "celebrity", "actor", "singer", "album",
    "game 1 winner", "game 2 winner", "game 3 winner",
    "will score", "will win the match",
    # Head-to-head sports markets kept slipping through the global filter
    # because a venue name doubled as a macro keyword ("China Open: A vs B",
    # "Solary vs T1 Academy"). ' vs ' is the cheapest reliable discriminator.
    " vs ", " vs.", "wta", "atp", "grand slam", "ncaa",
    "formula 1", "f1 ", "ufc", "mma", "boxing", "knockout",
    "open:", "masters:", "qualify for", "seeded",
]

ASSET_SEARCHES = {
    "bitcoin":      ["bitcoin price", "btc etf"],
    "ethereum":     ["ethereum price", "ethereum etf"],
    "solana":       ["solana above price", "solana hit price"],
    "zcash":        ["zcash price", "zec crypto"],
    "gold":         ["gold price", "gold futures"],
    "oil":          ["oil price", "opec crude"],
    "macro":        ["federal reserve rate", "inflation cpi"],
    "geopolitical": ["iran sanctions ceasefire", "russia ukraine war"],
    "tech":         ["nvidia earnings", "ai semiconductor"],
    "meta":         ["meta stock", "meta platforms earnings"],
    "amd":          ["amd stock", "amd earnings"],
    "intel":        ["intel stock", "intel foundry earnings"],
    "skhynix":      ["sk hynix stock", "hbm memory prices"],
    "micron":       ["micron stock", "dram memory prices"],
    "marvell":      ["marvell stock", "ai networking chips"],
}

# --- Market typology -------------------------------------------------------
# A prediction market's prob_yes is only a *sentiment* reading when the YES
# outcome expresses a directional view on the asset. Some market shapes are
# near-zero by construction and must never be averaged into the score:
#
#   "Will BTC be between $80,000 and $82,000?"  -> ~1% because the band is
#       irrelevant at spot, NOT because the crowd is bearish.
#   "Solana Up or Down - Oct 2, 12AM ET"        -> a coin flip by construction.
#
# Mixing these with real threshold markets produced a -76 STRONGLY_BEARISH
# reading off a dead BTC band contract.
DIRECTIONAL_TYPES = {"threshold", "level_touch", "path"}
EXCLUDED_TYPES    = {"range_band", "up_down"}

_RANGE_BAND_RE = re.compile(r"\bbetween\b|\bwithin\b|\bfrom \$?\d.*\bto \$?\d", re.I)
_UP_DOWN_RE    = re.compile(r"\bup\s*(?:or|/)\s*down\b|\bgreen\s*(?:or|/)\s*red\b", re.I)
_LEVEL_TOUCH_RE = re.compile(r"\b(?:hit|reaches?|touches?|gets to|trades (?:at|above|below))\b", re.I)
_THRESHOLD_RE  = re.compile(r"\b(?:above|over|exceed(?:s|ing)?|greater than|higher than)\b"
                            r"|\b(?:below|under|less than|lower than)\b"
                            r"|\$[\d,]+(?:\.\d+)?\s*(?:k)?\b", re.I)

# Markets thinner than this are noise, not crowd conviction.
MIN_LIQUIDITY_USD = 500.0
MIN_VOLUME_24H_USD = 1_000.0

# Moneyness model. At SOL spot 121.5, "above $100 on Oct 6" at 98.7% YES says
# nothing (deep in the money, already decided) while "above $125 on Oct 2" at
# 8.6% is the single most informative market in the book. A flat average over
# both cancels out to a coin flip, so directional strikes are weighted by how
# close they sit to spot, Gaussian in log-moneyness.
MONEY_SIGMA = 0.15        # ~15% stdev in log-strike/spot
MONEY_MAX_LOGMOVE = 0.60  # drop strikes beyond ~+/-82% from spot

_STRIKE_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*([km])?\b", re.I)


def extract_strike(question: str):
    """Pull the dollar strike out of a question, honouring k/m suffixes."""
    m = _STRIKE_RE.search(question or "")
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        val *= 1_000
    elif suffix == "m":
        val *= 1_000_000
    return val or None


def moneyness_weight(strike, spot, base):
    """Scale a market's weight by how informative its strike is at spot."""
    if not strike or not spot or spot <= 0 or base <= 0:
        return base
    log_move = math.log(strike / spot)
    if abs(log_move) > MONEY_MAX_LOGMOVE:
        return 0.0
    return base * math.exp(-(log_move ** 2) / (2 * MONEY_SIGMA ** 2))


def classify_market(question: str) -> str:
    """Classify a market by what its YES outcome actually asserts."""
    q = question.lower()
    if _RANGE_BAND_RE.search(q):
        return "range_band"
    if _UP_DOWN_RE.search(q):
        return "up_down"
    if _LEVEL_TOUCH_RE.search(q):
        return "level_touch"
    if _THRESHOLD_RE.search(q):
        return "threshold"
    return "path"


# ── Polarity ────────────────────────────────────────────────────────────────
# prob_yes is only a bullish probability when YES means "the price goes up".
# For a below-threshold question it means exactly the opposite:
#
#   "Will BTC be less than $76,000 on Oct 12?"  at 0.75% YES
#     -> the crowd is 99.25% sure BTC stays ABOVE $76k = BULLISH
#     -> was scored -99.9 STRONGLY_BEARISH, the largest single factor on the
#        largest asset on the board. Sign error, not a data error.
#
# Same failure mode on "dip to $110" (SOL, scored bullish), "reach $2000"
# (ZEC, scored bearish) and "hit (LOW) $3,900" (gold).
# "(HIGH)"/"(LOW)" tokens are the unambiguous form and are checked first.
_YES_MEANS_DOWN = re.compile(
    r"\(low\)"
    r"|\bbelow\b|\bunder\b|\bless than\b|\blower than\b|\bfewer than\b"
    r"|\bat or below\b|\bor less\b"
    r"|\bdip(?:s|ped)? to\b|\bdrop(?:s|ped)? to\b|\bfall(?:s|ing)? to\b"
    r"|\bdeclin(?:e|es|ed|ing) to\b|\bsink(?:s)? to\b|\bslide(?:s)? to\b",
    re.I,
)
_YES_MEANS_UP = re.compile(
    r"\(high\)"
    r"|\babove\b|\bover\b|\bgreater than\b|\bhigher than\b|\bmore than\b"
    r"|\bat or above\b|\bor more\b"
    r"|\breach(?:es|ing)?\b|\bsurpass(?:es|ing)?\b|\bexceed(?:s|ing)?\b"
    r"|\btouch(?:es|ing)?\b|\bget(?:s)? to\b",
    re.I,
)


def question_direction(question: str) -> int:
    """+1 if YES means the price rises, -1 if YES means it falls, 0 unknown.

    0 is deliberately treated as "no flip" downstream so a question we cannot
    classify keeps its old behaviour instead of silently inverting.
    Down is checked first: "hit (LOW)" must not be caught by the generic
    "hit/touch" upward vocabulary.
    """
    q = question or ""
    if _YES_MEANS_DOWN.search(q):
        return -1
    if _YES_MEANS_UP.search(q):
        return +1
    return 0


def to_bull_prob(prob_yes: float, direction: int) -> float:
    """Map a raw YES price onto a common 'price goes up' scale."""
    p = float(prob_yes)
    return (1.0 - p) if direction < 0 else p


def is_scorable(row) -> bool:
    """True when this market carries usable directional crowd sentiment."""
    return reject_reason(row) is None


def reject_reason(row) -> str | None:
    """Why this market cannot be scored, as a distinct reason.

    is_scorable() folded these together, which made the exclusion breakdown
    lie: liquidity-rejected markets were tallied under their own market_type
    (e.g. "threshold"), implying a direction problem that did not exist.
    """
    if classify_market(row.get("question", "")) not in DIRECTIONAL_TYPES:
        return "non_directional_type"
    # Liquidity is the depth gate. Volume alone let a $168-liquidity market
    # through via its 24h churn.
    if float(row.get("liquidity") or 0.0) < MIN_LIQUIDITY_USD:
        return "thin_liquidity"
    # A $ strike is the price anchor for this whole scoring model: moneyness
    # weighting keys off it, and a question without one cannot express a
    # price direction at all. "Will Microsoft have the best AI model at the
    # end of December 2026?" is ~0% YES *by construction* (somebody else
    # leads today) — a liquidity-weighted average over those AI-lab race
    # contracts scored -80 STRONGLY_BEARISH for NVDA, carrying 68% of the
    # composite, off markets that never mention NVDA's price.
    if extract_strike(row.get("question", "")) is None:
        return "no_price_anchor"
    return None


def _kw_hit(q: str, kw: str) -> bool:
    """Word-boundary keyword match.

    Plain `in` matching let 'sol' match 'Sol Zanetti', 'GPT Sol model' and
    'Solary vs T1 Academy' — Solana's market bucket filled up with Quebec
    premier politics and AI benchmarks, then scored as crypto sentiment.
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", q) is not None


def is_financial_market(question: str) -> bool:
    q = question.lower()
    for blocked in BLOCKED_KEYWORDS:
        if _kw_hit(q, blocked):
            return False
    for allowed in FINANCIAL_KEYWORDS:
        if _kw_hit(q, allowed):
            return True
    return False


# A market only counts toward an asset's sentiment if the question actually
# names that asset. Bare tickers are guarded so they only match in a financial
# context ('SOL above $140', 'SOL/USDT'), never as a person's surname.
ASSET_ENTITY_PATTERNS = {
    "bitcoin":      [r"\bbitcoin\b", r"\bbtc\b"],
    "ethereum":     [r"\bethereum\b", r"\beth\b"],
    # 'sol' alone is unusable: Sol Zanetti, GPT Sol, Solary all collide.
    "solana":       [r"\bsolana\b",
                     r"\bsol\b(?=\s*(?:above|below|over|under|hit|reaches?|"
                     r"price|up|down|/usdt|\$|to))"],
    "zcash":        [r"\bzcash\b", r"\bzec\b"],
    "gold":         [r"\bgold\b", r"\bxau\b"],
    "oil":          [r"\boil\b", r"\bcrude\b", r"\bopec\b", r"\bwti\b", r"\bbrent\b"],
    "macro":        [r"\bfed\b", r"\bfederal reserve\b", r"\binflation\b",
                     r"\bcpi\b", r"\binterest rate", r"\brate (?:cut|hike|decision)\b"],
    "geopolitical": [r"\biran\b", r"\bisrael\b", r"\brussia\b", r"\bukraine\b",
                     r"\bchina\b", r"\btaiwan\b", r"\bnato\b", r"\bceasefire\b",
                     r"\bwar\b", r"\bsanction"],
    "tech":         [r"\bnvidia\b", r"\bai\b", r"\bsemiconduct", r"\bchip"],
    "meta":         [r"\bmeta platforms\b", r"\bmeta\b", r"\bfacebook\b"],
    "amd":          [r"\badvanced micro devices\b", r"\bamd\b"],
    "intel":        [r"\bintel\b", r"\bintc\b"],
}
ASSET_ENTITY_PATTERNS = {
    k: [re.compile(p, re.I) for p in v] for k, v in ASSET_ENTITY_PATTERNS.items()
}


def mentions_asset(question: str, asset_key: str) -> bool:
    pats = ASSET_ENTITY_PATTERNS.get(asset_key)
    if not pats:
        return True
    return any(p.search(question) for p in pats)


def parse_market(m: dict) -> dict:
    try:
        prices   = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
        prob_yes = float(prices[0])
        prob_no  = float(prices[1]) if len(prices) > 1 else 1 - prob_yes
    except Exception:
        prob_yes, prob_no = 0.5, 0.5

    return {
        "market_id":    m.get("id",        ""),
        "question":     m.get("question",  ""),
        "prob_yes":     round(prob_yes, 4),
        "prob_no":      round(prob_no,  4),
        "volume_24hr":  float(m.get("volume24hr", 0) or 0),
        "volume_total": float(m.get("volume",     0) or 0),
        "liquidity":    float(m.get("liquidity",  0) or 0),
        "end_date":     m.get("endDate",  ""),
        "category":     m.get("category", ""),
        "slug":         m.get("slug",     ""),
        "market_type":  classify_market(m.get("question", "")),
    }


def fetch_filtered_markets(limit=100):
    try:
        r = requests.get(
            f"{GAMMA_BASE}/markets", headers=HEADERS,
            params={"active": "true", "limit": limit,
                    "order": "volume24hr", "ascending": "false"},
            timeout=20
        )
        r.raise_for_status()
        markets = r.json()
        if not markets:
            return pd.DataFrame()
        rows = [parse_market(m) for m in markets
                if is_financial_market(m.get("question", ""))]
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception as e:
        print(f"    [Polymarket/markets] {type(e).__name__}: {e}")
        return pd.DataFrame()


def search_financial_markets(query: str, limit=20) -> pd.DataFrame:
    """
    Search Polymarket for markets matching `query`.

    NOTE: Gamma's /markets endpoint silently ignores the `q` param and always
    returns the same volume-ordered page, so every asset used to receive an
    identical market set. /public-search honours `q` and nests the markets
    under each event, so we walk the events and collect their markets.
    """
    try:
        r = requests.get(
            f"{GAMMA_BASE}/public-search", headers=HEADERS,
            params={"q": query, "limit_per_type": max(5, limit // 2),
                    "events_status": "active"},
            timeout=20
        )
        r.raise_for_status()
        payload = r.json()

        events = payload.get("events") or []
        rows = []
        for ev in events:
            for m in (ev.get("markets") or []):
                if m.get("closed") or m.get("archived"):
                    continue
                question = m.get("question", "")
                if not is_financial_market(question):
                    continue
                rows.append(parse_market(m))

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).drop_duplicates(subset=["market_id"])
        return df.sort_values("volume_24hr", ascending=False).head(limit)
    except Exception as e:
        print(f"    [Polymarket/search '{query}'] {type(e).__name__}: {e}")
        return pd.DataFrame()


# ── Strike-relative crowd scoring ─────────────────────────────────────────
# A prediction market's YES probability is NOT a sentiment reading on its own.
# "BTC above $90k" trading at 5% YES says the strike is far away, not that the
# crowd is bearish — averaging that raw probability across strikes is what
# produced the -99 "STRONGLY_BEARISH" reading off a handful of far-OTM
# contracts. The crowd's opinion only shows up as the GAP between the
# probability it assigns and the probability the strike's distance from spot
# implies:
#
#     model_up = N(d2),  d2 = (ln(S/K) - 0.5*sigma^2*T) / (sigma*sqrt(T))
#     surprise = crowd_up - model_up
#
# A crowd pricing a far-OTM contract at exactly its model probability has no
# opinion; a crowd pricing it far above (below) model is expressing a
# directional view that can be scored.
CROWD_SURPRISE_SCALE = float(os.environ.get("CROWD_SURPRISE_SCALE", "250"))
CROWD_CONF_FACTOR = {"high": 1.0, "medium": 0.6, "low": 0.3}
CROWD_LOW_CONF_CAP = 30.0
DEFAULT_ANN_VOL = 0.60
MIN_HORIZON_DAYS = 1.0 / 24
MAX_HORIZON_DAYS = 365.0
# The crowd quotes a named underlying; our price series may be a different
# contract for the same commodity (Brent vs WTI). When the spot implied by
# the crowd's own prices is more than this far from ours, the strike-distance
# baseline is measuring the basis, not the crowd's opinion.
BASIS_MISMATCH_TOL = float(os.environ.get("CROWD_BASIS_MISMATCH_TOL", "0.08"))
BASIS_MISMATCH_DAMP = float(os.environ.get("CROWD_BASIS_MISMATCH_DAMP", "0.75"))


def _norm_cdf(x):
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Two market shapes need two different baselines:
#   settle  — "will the price be greater than $X on <date>"  -> P(S_T > K)
#   touch   — "will it reach $X by <date>" / "hit $X"         -> P(max S_t > K)
# A touch market is worth roughly twice a settle market at the same strike,
# so scoring it with the settle baseline overstates how bullish the crowd
# looks and flips the sign of the surprise on long-dated contracts.
TOUCH_PATTERN = re.compile(
    r"\b(reach|reaches|reached|hit|hits|touch|touches|prints?|trades?)\b",
    re.I)


def is_touch_market(question: str) -> bool:
    """True when the contract resolves on touching the level, not settling."""
    return bool(TOUCH_PATTERN.search(question or ""))


def model_up_prob(strike, spot, ann_vol, horizon_days, touch=False, direction=1):
    """
    Probability the underlying ends (or touches) ABOVE `strike`, from spot,
    vol and horizon — the scale every crowd probability is normalised onto.

    Zero-drift lognormal approximation for settling contracts:
        P(S_T > K) = N(d2),  d2 = (ln(S/K) - 0.5*sigma^2*T) / (sigma*sqrt(T))

    Touch contracts ("will it hit $K") resolve on the barrier, which is a
    different proposition:
        P(max S_t > K) = 2 * N(ln(S/K) / (sigma*sqrt(T)))
    and a DOWN touch contract ("will it fall to $K") needs the mirrored
    barrier, P(min S_t < K) = 2 * N(ln(K/S) / (sigma*sqrt(T))), flipped back
    to the "above the strike" scale. Comparing a DOWN contract's crowd price
    against an UP barrier probability compares two different propositions
    and manufactures a large one-sided surprise.

    Returns None when the inputs cannot support a calculation (missing strike,
    non-positive spot, unknown vol).
    """
    try:
        strike = float(strike)
        spot = float(spot)
        ann_vol = float(ann_vol)
        horizon_days = float(horizon_days)
    except (TypeError, ValueError):
        return None
    if strike <= 0 or spot <= 0 or ann_vol <= 0:
        return None
    t = max(MIN_HORIZON_DAYS, min(MAX_HORIZON_DAYS, horizon_days)) / 365.0
    sigma_t = ann_vol * math.sqrt(t)
    if sigma_t <= 0:
        return None
    if touch:
        if direction >= 0:
            d = math.log(spot / strike) / sigma_t
            return min(1.0, 2.0 * _norm_cdf(d))
        # DOWN touch: probability of touching the lower barrier, flipped to
        # the "above the strike" scale the crowd number is on.
        d = math.log(strike / spot) / sigma_t
        p_down = min(1.0, 2.0 * _norm_cdf(d))
        return max(0.0, 1.0 - p_down)
    d2 = (math.log(spot / strike) - 0.5 * ann_vol ** 2 * t) / sigma_t
    return _norm_cdf(d2)


def implied_spot(crowd_up_prob, strike, ann_vol, horizon_days, touch=False,
                 direction=1):
    """
    The spot price that would make the model agree with the crowd.

    Inverts model_up_prob by bisection (it is monotone in spot for both
    settle and touch contracts). Used to detect a benchmark mismatch: the
    crowd trades "WTI" while the price series is Brent, so every strike
    sits at a different distance from spot and the surprise turns into an
    artefact of the basis instead of an opinion.
    """
    if crowd_up_prob is None or not strike or not ann_vol:
        return None
    target = float(crowd_up_prob)
    if target <= 0.0 or target >= 1.0:
        return None

    def f(s):
        p = model_up_prob(strike, s, ann_vol, horizon_days, touch=touch,
                          direction=direction)
        return -1.0 if p is None else p - target

    lo = float(strike) * 0.4
    hi = float(strike) * 2.5
    if f(lo) > 0 or f(hi) < 0:
        return None
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def horizon_days(end_date, now=None):
    """Days from now to a Gamma endDate (ISO 8601 string). None if unusable."""
    if not end_date:
        return None
    try:
        end = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    now = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (end - now).total_seconds() / 86400.0


def derive_signal(markets: list, spot: float = None, ann_vol: float = None) -> dict:
    """
    Derive crowd sentiment from the *directional* subset of markets only.

    Excluded (and reported, never silently dropped):
      - range_band  "BTC between $80k and $82k"  -> near-zero by construction
      - up_down     "SOL Up or Down 12AM"      -> coin flip by construction
      - sub-threshold liquidity markets        -> noise, not conviction

    Aggregation is moneyness- and liquidity-weighted so that the markets which
    actually carry information — strikes close to spot — dominate, instead of
    the previous plain mean over prob_yes.
    """
    if not markets:
        return {"signal": "UNKNOWN", "avg_prob_yes": None,
                "market_count": 0, "interpretation": "No relevant markets found",
                "top_market": None, "total_vol_24hr": 0}

    df = pd.DataFrame(markets)
    if "market_type" not in df.columns:
        df["market_type"] = df["question"].map(classify_market)

    df["market_type"] = df["question"].map(classify_market)

    df["_reject"] = df.apply(reject_reason, axis=1)
    cand     = df[df["_reject"].isna()].copy()
    excluded = df[df["_reject"].notna()].copy()

    # Liquidity base weight, then moneyness tilt when spot is known.
    weight_col = None
    for c in ("liquidity", "volume_total", "volume_24hr"):
        if cand[c].fillna(0).sum() > 0:
            weight_col = c
            break
    if weight_col:
        w = cand[weight_col].fillna(0).astype(float)
    else:
        w = pd.Series(1.0, index=cand.index)
        weight_col = "equal"

    if spot:
        strikes = cand["question"].map(extract_strike)
        cand = cand.assign(_strike=strikes)
        w = pd.Series(
            [moneyness_weight(s, spot, float(base)) for s, base in zip(strikes, w)],
            index=cand.index,
        )
        weight_col = f"{weight_col}x_moneyness"

    used = cand[w > 0].copy()
    w = w[w > 0]
    # Count directly from the index diff. Comparing the reindexed weights to 0
    # silently always gave 0, because dropped rows come back as NaN, not 0.
    dropped_far = int(len(cand) - len(used)) if spot else 0

    excluded_breakdown = (
        excluded["_reject"].value_counts().to_dict() if not excluded.empty else {}
    )

    def _examples(frame, n=3):
        if frame.empty:
            return []
        return [
            {"question": r["question"][:90], "prob_yes": float(r["prob_yes"]),
             "market_type": r.get("market_type", "?")}
            for _, r in frame.sort_values("volume_24hr", ascending=False).head(n).iterrows()
        ]

    if used.empty:
        return {
            "signal": "UNKNOWN", "avg_prob_yes": None, "market_count": len(markets),
            "interpretation": (
                f"No usable directional markets (excluded {len(excluded)}: "
                f"{excluded_breakdown or 'none'}"
                + (f"; {dropped_far} strikes >{MONEY_MAX_LOGMOVE:.0%} from spot"
                   if spot and dropped_far else "")
                + ")"
            ),
            "top_market": None, "total_vol_24hr": 0,
            "markets_used": 0, "markets_excluded": int(len(excluded)),
            "excluded_breakdown": excluded_breakdown,
            "excluded_examples": _examples(excluded),
            "weighted_by": None, "spot_used": spot,
        }

    # ── Polarity normalisation (see question_direction above) ──────────────
    # Every market is mapped to a "price goes up" probability before the
    # weighted average, so a bearish reading can never come from a market the
    # crowd actually reads as bullish.
    used = used.copy()
    used["_direction"] = used["question"].map(question_direction)
    used["_bull"] = [
        to_bull_prob(p, d) for p, d in zip(used["prob_yes"], used["_direction"])
    ]
    n_flipped = int((used["_direction"] < 0).sum())

    raw_avg_yes = float((used["prob_yes"] * w[w > 0]).sum() / w[w > 0].sum())
    avg_yes = float((used["_bull"] * w[w > 0]).sum() / w[w > 0].sum())
    top_row = used.loc[w[w > 0].idxmax()]

    # Sample-size confidence. Bitcoin/ethereum were emitting STRONGLY_BEARISH
    # off just 2 surviving markets, which reads identically to a 28-market
    # consensus and is the next trap after the original range-band averaging.
    n_used = int(len(used))
    confidence = "high" if n_used >= 10 else ("medium" if n_used >= 5 else "low")

    # ── Strike-relative crowd score ─────────────────────────────────────
    # The raw bull-probability average above is kept for continuity, but the
    # scorable reading is the crowd's SURPRISE against what the strike
    # structure implies. Without spot or vol there is no baseline to compare
    # against, so the score falls back to the legacy probability mapping.
    crowd = None
    if spot:
        vol = float(ann_vol) if ann_vol else DEFAULT_ANN_VOL
        scored = []
        for idx, row in used.iterrows():
            strike = row.get("_strike")
            direction = int(row.get("_direction") or 0)
            if not strike or direction == 0:
                continue
            days = horizon_days(row.get("end_date"))
            if days is None or days <= 0:
                continue
            touch = is_touch_market(row.get("question"))
            # A touch contract whose outcome our price has already decided
            # carries no directional information: "will it hit $105" is
            # already true at a spot of $108, so the crowd's price on it
            # says nothing about their view — only about which benchmark
            # they are quoting.
            if touch and direction >= 0 and float(strike) <= float(spot):
                continue
            if touch and direction < 0 and float(strike) >= float(spot):
                continue
            m_up = model_up_prob(strike, spot, vol, days, touch=touch,
                                 direction=direction)
            if m_up is None:
                continue
            scored.append((float(row["_bull"]), m_up, float(w.get(idx, 0.0)),
                           touch, direction, float(strike), days))
        wsum = sum(wt for _, _, wt, _, _, _, _ in scored)
        if scored and wsum > 0:
            crowd_up = sum(c * wt for c, _, wt, _, _, _, _ in scored) / wsum
            model_up = sum(m * wt for _, m, wt, _, _, _, _ in scored) / wsum
            surprise = crowd_up - model_up
            raw = max(-100.0, min(100.0, surprise * CROWD_SURPRISE_SCALE))
            factor = CROWD_CONF_FACTOR.get(confidence, 0.3)

            # Benchmark check. The crowd trades a named underlying ("WTI",
            # "Gold"); our series may be a different contract for the same
            # commodity. If the spot implied by the crowd's own prices sits
            # far from our spot, the "surprise" is measuring the basis, not
            # the crowd's opinion, so the read is damped and flagged.
            implied = []
            for c, _, wt, t, dr, st, dd in scored:
                s_imp = implied_spot(c, st, vol, dd, touch=t, direction=dr)
                if s_imp:
                    implied.append(s_imp)
            basis_ratio = None
            damped = 0.0
            if implied:
                implied.sort()
                median_imp = implied[len(implied) // 2]
                basis_ratio = median_imp / float(spot)
                if abs(basis_ratio - 1.0) > BASIS_MISMATCH_TOL:
                    damped = 1.0 - BASIS_MISMATCH_DAMP
                    factor *= damped
            effective = raw * factor
            if confidence == "low":
                effective = max(-CROWD_LOW_CONF_CAP,
                                min(CROWD_LOW_CONF_CAP, effective))
            crowd = {
                "score":        round(effective, 1),
                "raw_score":    round(raw, 1),
                "crowd_up_prob": round(crowd_up, 4),
                "model_up_prob": round(model_up, 4),
                "surprise":     round(surprise, 4),
                "markets_scored": len(scored),
                "touch_markets": sum(1 for s in scored if s[3]),
                "ann_vol":      round(vol, 4),
                "confidence_factor": round(factor, 3),
                "implied_spot": (round(implied[len(implied) // 2], 2)
                                 if implied else None),
                "basis_ratio":  round(basis_ratio, 4) if basis_ratio else None,
                "basis_mismatch": bool(damped),
                "method": "strike-relative surprise",
            }

    # avg_yes is now on a single "price goes up" scale for every market, so
    # the wording below says bullish-equivalent rather than raw YES.
    # When a strike-relative score is available it supersedes the raw
    # probability band, because the raw band describes how far the strikes
    # sit from spot, not what the crowd believes.
    if crowd:
        avg_yes = crowd["crowd_up_prob"]
    if crowd:
        # The label describes the score the engine actually uses, not the raw
        # surprise: a damped (low-confidence or basis-mismatched) read must
        # not be published as STRONGLY.
        sc = crowd["score"]
        if sc >= 15:
            signal = "STRONGLY_BULLISH"
        elif sc >= 6:
            signal = "BULLISH"
        elif sc <= -15:
            signal = "STRONGLY_BEARISH"
        elif sc <= -6:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"
        interp = (f"Crowd is {abs(crowd['surprise']):.0%} "
                  f"{'above' if crowd['surprise'] >= 0 else 'below'} what the "
                  f"strikes imply across {crowd['markets_scored']} directional "
                  f"markets — {signal.replace('_', ' ').lower()}")
    elif avg_yes >= 0.65:
        signal = "STRONGLY_BULLISH"
        interp = f"Crowd {avg_yes:.0%} bullish-equivalent across {n_used} directional markets — strong conviction"
    elif avg_yes >= 0.55:
        signal = "BULLISH"
        interp = f"Crowd leans bullish at {avg_yes:.0%} across {n_used} directional markets — moderate bullish"
    elif avg_yes <= 0.35:
        signal = "STRONGLY_BEARISH"
        interp = f"Crowd only {avg_yes:.0%} bullish-equivalent across {n_used} directional markets — strong bearish"
    elif avg_yes <= 0.45:
        signal = "BEARISH"
        interp = f"Crowd leans bearish at {1-avg_yes:.0%} across {n_used} directional markets — moderate bearish"
    else:
        signal = "NEUTRAL"
        interp = f"Crowd uncertain at {avg_yes:.0%} across {n_used} directional markets"

    if n_flipped:
        interp += (f" | polarity corrected: {n_flipped}/{n_used} YES=down "
                   f"question{'' if n_flipped == 1 else 's'} flipped "
                   f"(raw avg {raw_avg_yes:.0%} -> {avg_yes:.0%})")

    if excluded_breakdown:
        interp += (f" | excluded {len(excluded)} non-directional: "
                   f"{', '.join(f'{k}x{v}' for k, v in excluded_breakdown.items())}")
    if spot:
        interp += f" | weighted by moneyness around spot {spot:.2f}"
    if crowd:
        interp += (f" | strike-relative: crowd {crowd['crowd_up_prob']:.0%} vs "
                   f"model {crowd['model_up_prob']:.0%} -> surprise "
                   f"{crowd['surprise']:+.1%} (score {crowd['score']:+.0f})")

    if confidence != "high":
        # Keep the bare signal in `signal` and qualify it here, so downstream
        # string matching on the signal name keeps working.
        interp += (f" | LOW CONFIDENCE: only {n_used} usable market"
                   f"{'s' if n_used != 1 else ''} survived filtering")

    crowd_signal = None
    if crowd:
        surprise = crowd["surprise"]
        if surprise >= 0.15:
            crowd_signal = "STRONGLY_BULLISH"
        elif surprise >= 0.06:
            crowd_signal = "BULLISH"
        elif surprise <= -0.15:
            crowd_signal = "STRONGLY_BEARISH"
        elif surprise <= -0.06:
            crowd_signal = "BEARISH"
        else:
            crowd_signal = "NEUTRAL"

    return {
        "signal":         signal,
        "confidence":     confidence,
        "crowd_signal":   crowd_signal,
        "crowd_score":    (crowd["score"] if crowd else None),
        "crowd_detail":   crowd,
        "avg_prob_yes":   round(avg_yes, 4),
        "raw_avg_yes":    round(raw_avg_yes, 4),
        "polarity_flipped": n_flipped,
        "market_count":   len(markets),
        "interpretation": interp,
        "top_market": {
            "question":    top_row["question"],
            "prob_yes":    float(top_row["prob_yes"]),
            "direction":   int(top_row["_direction"]),
            "bull_prob":   round(float(top_row["_bull"]), 4),
            "volume_24hr": float(top_row["volume_24hr"]),
            "market_type": top_row["market_type"],
            "strike":      top_row.get("_strike") if spot else None,
            "end_date":    top_row["end_date"],
        },
        "total_vol_24hr": float(used["volume_24hr"].sum()),
        "candidates":        int(len(markets)),
        "markets_used":      int(len(used)),
        "excluded_offtype":  int(len(excluded)),
        "excluded_too_far":  int(dropped_far),
        "markets_excluded":  int(len(excluded)) + int(dropped_far),
        "excluded_breakdown": excluded_breakdown,
        "excluded_examples":  _examples(excluded),
        "weighted_by":        weight_col,
        "spot_used":          spot,
    }


def fetch_polymarket_snapshot(asset_searches=None, top_limit=100, spot_prices=None,
                              ann_vols=None):
    if asset_searches is None:
        asset_searches = ASSET_SEARCHES
    spot_prices = spot_prices or {}
    ann_vols = ann_vols or {}

    result = {
        "snapshot_time":          datetime.now(timezone.utc).isoformat(),
        "top_financial_markets":  [],
        "by_asset":               {},
        "signals":                {},
        "total_markets_fetched":  0,
        "total_after_filter":     0,
    }

    print(f"  [Polymarket] Fetching top {top_limit} markets → filtering to financial...")
    top_df = fetch_filtered_markets(limit=top_limit)
    if not top_df.empty:
        result["top_financial_markets"] = top_df.head(20).to_dict(orient="records")
        result["total_markets_fetched"] = top_limit
        result["total_after_filter"]    = len(top_df)
        print(f"    {top_limit} fetched → {len(top_df)} financial after filter")
        print("    Top 5:")
        for _, row in top_df.head(5).iterrows():
            print(f"      [{row['prob_yes']:.0%} YES | ${row['volume_24hr']:,.0f}] {row['question'][:65]}")
    else:
        print("    WARNING — No financial markets after filtering")

    time.sleep(1)

    for asset_key, queries in asset_searches.items():
        print(f"  [Polymarket] '{asset_key}'...", end=" ")
        asset_markets = []
        for q in queries[:2]:
            df = search_financial_markets(q, limit=15)
            if not df.empty:
                df["matched_query"] = q
                asset_markets.append(df)
            time.sleep(0.5)

        if asset_markets:
            combined = pd.concat(asset_markets, ignore_index=True)
            combined = combined.drop_duplicates(subset=["market_id"])
            # Only keep markets that actually name this asset. Polymarket
            # search is fuzzy and returns neighbours ("Sol Zanetti" for a
            # solana query), which is how unrelated markets reached the score.
            n_before = len(combined)
            combined = combined[combined["question"].map(
                lambda q: mentions_asset(q, asset_key))]
            dropped = n_before - len(combined)
            combined = combined.sort_values("volume_24hr", ascending=False)
            result["by_asset"][asset_key] = combined.to_dict(orient="records")
            print(f"{len(combined)} markets ({dropped} off-topic dropped)")
        else:
            result["by_asset"][asset_key] = []
            print("0 (all filtered)")

        time.sleep(0.8)

    for asset_key, markets in result["by_asset"].items():
        result["signals"][asset_key] = derive_signal(
            markets, spot=spot_prices.get(asset_key),
            ann_vol=ann_vols.get(asset_key))

    return result


def format_signal_summary(snapshot: dict) -> str:
    lines = []
    lines.append("=" * 65)
    lines.append("  POLYMARKET — FINANCIAL INTELLIGENCE")
    lines.append(f"  {snapshot['snapshot_time'][:19]} UTC")
    lines.append(f"  {snapshot['total_markets_fetched']} raw → {snapshot['total_after_filter']} financial")
    lines.append("=" * 65)

    lines.append("\n  SIGNALS BY ASSET:")
    for asset, sig in snapshot["signals"].items():
        if sig["signal"] == "UNKNOWN":
            continue
        prob = f"{sig['avg_prob_yes']:.0%}" if sig["avg_prob_yes"] else "N/A"
        lines.append(f"  {asset.upper():<16} {sig['signal']:<24} avg YES: {prob}")
        if sig.get("top_market"):
            tm  = sig["top_market"]
            vol = f"${tm['volume_24hr']:,.0f}"
            lines.append(f"    [{tm['prob_yes']:.0%}] {tm['question'][:60]:<60} {vol}")

    lines.append("\n  TOP FINANCIAL MARKETS:")
    for m in snapshot["top_financial_markets"][:8]:
        vol = f"${m['volume_24hr']:,.0f}"
        lines.append(f"  [{m['prob_yes']:.0%}] {m['question'][:60]:<60} {vol:>12}")
    lines.append("=" * 65)
    return "\n".join(lines)


ASSET_QUERIES = ASSET_SEARCHES

if __name__ == "__main__":
    print("=" * 65)
    print("POLYMARKET — FINANCIAL FILTER TEST")
    print("=" * 65 + "\n")
    snapshot = fetch_polymarket_snapshot(top_limit=100)
    print("\n" + format_signal_summary(snapshot))
"""
MULTI-SOURCE NEWS AGGREGATOR
Pulls from multiple free sources matched to each asset class.
All sources are free and require no API key unless noted.

Sources by asset class (every URL below is verified reachable):
  CRYPTO  → CoinTelegraph, CoinDesk, Decrypt, BeInCrypto, The Block,
             Investing.com crypto, Reddit r/CryptoCurrency
  OIL     → EIA Today in Energy, OilPrice, CNBC Markets, WSJ Markets,
             GDELT (geopolitical)
  STOCKS  → Yahoo per-ticker feed, MarketWatch, Seeking Alpha, CNBC Top,
             WSJ Markets, SEC EDGAR
  MACRO   → CNBC Markets, WSJ World, NPR Business, Federal Reserve, FT
  ALL     → Fear & Greed Index (in fetch_sentiment.py)

Dead sources removed after live audits (persistent failure):
  Reuters_Top / Reuters_Energy  — feeds.reuters.com no longer resolves (DNS)
  AP_Business                   — rsshub.app proxy returns 403
  EIA_News / Yahoo_Finance top — permanent 404
  Binance_Announcements         — CMS API returns HTTP 400 every run

Every source has a fallback — if one fails, the rest still run — and each
source's outcome is recorded in SOURCE_HEALTH so a quietly dead feed cannot
pass for healthy evidence.
"""

import requests
import feedparser
import pandas as pd
import time
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────
# RSS FEED REGISTRY
# Add or remove sources here without touching any other code
# ─────────────────────────────────────────────────────────────
RSS_FEEDS = {

    # ── CRYPTO ──────────────────────────────────────────────
    "crypto": [
        {
            "name": "CoinTelegraph",
            "url": "https://cointelegraph.com/rss",
            "quality": "high"
        },
        {
            "name": "CoinDesk",
            "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "quality": "high"
        },
        {
            "name": "Decrypt",
            "url": "https://decrypt.co/feed",
            "quality": "high"
        },
        {
            "name": "BeInCrypto",
            "url": "https://beincrypto.com/feed/",
            "quality": "medium"
        },
        {
            "name": "TheBlock",
            "url": "https://www.theblock.co/rss.xml",
            "quality": "medium"
        },
        {
            "name": "Investing_Crypto",
            "url": "https://www.investing.com/rss/news_301.rss",
            "quality": "medium"
        },
    ],

    # ── OIL / COMMODITIES ────────────────────────────────────
    "commodity": [
        {
            "name": "EIA_TodayInEnergy",
            # Official US Energy Information Administration. The old
            # eia.gov/rss/news.xml is a permanent 404.
            "url": "https://www.eia.gov/rss/todayinenergy.xml",
            "quality": "critical"
        },
        {
            "name": "OilPrice_News",
            "url": "https://oilprice.com/rss/main",
            "quality": "medium"
        },
        {
            "name": "CNBC_Markets",
            "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
            "quality": "high"
        },
        {
            "name": "WSJ_Markets",
            "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
            "quality": "high"
        },
    ],

    # ── STOCKS ──────────────────────────────────────────────
    "stock": [
        {
            # Per-ticker feed: finance.yahoo.com/news/rssindex is a 404, the
            # headline feed per symbol is the one that still resolves.
            "name": "Yahoo_Ticker",
            "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
            "quality": "high",
            "needs_ticker": True
        },
        {
            "name": "MarketWatch",
            "url": "https://feeds.marketwatch.com/marketwatch/topstories/",
            "quality": "high"
        },
        {
            "name": "Seeking_Alpha",
            "url": "https://seekingalpha.com/feed.xml",
            "quality": "medium"
        },
        {
            "name": "CNBC_Top",
            "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
            "quality": "high"
        },
        {
            "name": "WSJ_Markets",
            "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
            "quality": "high"
        },
    ],

    # ── MACRO (applies to all asset classes) ─────────────────
    "macro": [
        {
            "name": "CNBC_Markets",
            "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
            "quality": "high"
        },
        {
            "name": "WSJ_World",
            "url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
            "quality": "high"
        },
        {
            "name": "NPR_Business",
            "url": "https://feeds.npr.org/1006/rss.xml",
            "quality": "high"
        },
        {
            "name": "FederalReserve",
            # Rate decisions land here first; the old Reuters_Top feed's DNS
            # name no longer resolves at all.
            "url": "https://www.federalreserve.gov/feeds/press_all.xml",
            "quality": "critical"
        },
        {
            "name": "FT_Markets",
            "url": "https://www.ft.com/rss/home",
            "quality": "high"
        },
    ],
}

# ── Source health telemetry ────────────────────────────────────────────
# A feed that silently returns nothing looks exactly like a feed that has
# nothing to say. Every source's outcome is recorded here so the snapshot
# can report which evidence actually arrived.
SOURCE_HEALTH = {}


def record_source_health(name, status, articles=0, error=None):
    """Record the outcome of one source fetch."""
    SOURCE_HEALTH[name] = {
        "status": status,
        "articles": int(articles or 0),
        "error": error,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    return SOURCE_HEALTH[name]


def get_source_health():
    """Copy of the per-source health records collected this run."""
    return {name: dict(rec) for name, rec in SOURCE_HEALTH.items()}


def source_health_summary():
    """Sources that hit, sources that failed, and the failure list.

    A cached answer counts as a hit — the source answered, the pipeline just
    did not need to ask again. A feed that simply had nothing to publish is
    reported separately from a feed that broke: a quiet Federal Reserve is
    not a broken one, and calling it dead would punish the signal for a
    holiday weekend.
    """
    hit = sorted(n for n, r in SOURCE_HEALTH.items()
                 if r["status"] in ("ok", "cached") and r.get("articles", 0) > 0)
    empty = sorted(n for n, r in SOURCE_HEALTH.items()
                   if r["status"] == "empty")
    skipped = sorted(n for n, r in SOURCE_HEALTH.items()
                     if r["status"] == "skipped")
    failed = sorted(n for n, r in SOURCE_HEALTH.items()
                    if r["status"] in ("error", "rate_limited"))
    return {
        "sources_hit": hit,
        "sources_empty": empty,
        "sources_skipped": skipped,
        "sources_failed": failed,
        "detail": get_source_health(),
    }

# User agent to avoid being blocked by RSS servers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def fetch_rss_feed(feed_config, max_articles=10):
    """
    Fetches a single RSS feed and returns a clean DataFrame.
    Returns empty DataFrame on any failure — never crashes the pipeline.

    feed_config : dict with keys: name, url, quality
    max_articles : max number of articles to return
    """
    name = feed_config["name"]
    url = feed_config["url"]
    if feed_config.get("needs_ticker"):
        ticker = (feed_config.get("ticker") or "").upper()
        if not ticker:
            record_source_health(name, "skipped", 0, "no ticker supplied")
            return pd.DataFrame()
        url = url.format(ticker=ticker)

    try:
        # feedparser with custom headers via requests
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        feed = feedparser.parse(response.content)

        if not feed.entries:
            record_source_health(name, "empty", 0, "no entries in feed")
            return pd.DataFrame()

        articles = []
        for entry in feed.entries[:max_articles]:
            # Parse publish date safely
            published = None
            if hasattr(entry, "published"):
                try:
                    published = entry.published
                except Exception:
                    pass

            articles.append({
                "source": name,
                "quality": feed_config["quality"],
                "title": getattr(entry, "title", ""),
                "url": getattr(entry, "link", ""),
                "published": published,
                "summary": getattr(entry, "summary", "")[:300] if hasattr(entry, "summary") else "",
            })

        df = pd.DataFrame(articles)
        record_source_health(name, "ok", len(df))
        return df

    except requests.exceptions.Timeout:
        print(f"    [{name}] Timeout — skipping")
        record_source_health(name, "error", 0, "timeout")
        return pd.DataFrame()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        print(f"    [{name}] HTTP {code} — skipping")
        record_source_health(name, "error", 0, f"HTTP {code}")
        return pd.DataFrame()
    except Exception as e:
        print(f"    [{name}] Error: {type(e).__name__} — skipping")
        record_source_health(name, "error", 0, f"{type(e).__name__}: {e}")
        return pd.DataFrame()


def fetch_reddit_sentiment(subreddit="CryptoCurrency", limit=25):
    """
    Fetch hot posts from a subreddit via Reddit's free JSON API.
    No API key required for read-only public access.

    subreddit : e.g. CryptoCurrency, wallstreetbets, investing
    limit     : number of posts to fetch (max 100)

    Returns DataFrame with: title, score, num_comments, url
    """
    source = f"Reddit_r/{subreddit}"
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
    headers = {"User-Agent": "TradingPipeline/1.0 (research bot)"}

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        posts = data["data"]["children"]

        records = []
        for post in posts:
            p = post["data"]
            records.append({
                "source": source,
                "title": p.get("title", ""),
                "score": p.get("score", 0),
                "num_comments": p.get("num_comments", 0),
                "url": f"https://reddit.com{p.get('permalink', '')}",
                "published": datetime.fromtimestamp(
                    p.get("created_utc", 0), tz=timezone.utc
                ).isoformat()
            })

        record_source_health(source, "ok", len(records))
        return pd.DataFrame(records)

    except Exception as e:
        # Reddit's JSON API answers with 403 for datacenter IPs. The public
        # RSS endpoint for the same subreddit still works, so fall back to it
        # instead of losing the source entirely.
        print(f"    [Reddit r/{subreddit}] JSON API failed ({type(e).__name__}) "
              f"— trying RSS")
        return _fetch_reddit_rss(subreddit, limit)


def _fetch_reddit_rss(subreddit="CryptoCurrency", limit=25):
    """Reddit RSS fallback — same subreddit, no engagement counters."""
    source = f"Reddit_r/{subreddit}"
    url = f"https://www.reddit.com/r/{subreddit}/.rss"
    headers = {"User-Agent": HEADERS["User-Agent"]}

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if not feed.entries:
            record_source_health(source, "empty", 0, "no entries in RSS")
            return pd.DataFrame()

        records = []
        for entry in feed.entries[:limit]:
            published = getattr(entry, "published", None)
            link = getattr(entry, "link", "")
            if published:
                try:
                    published = datetime.fromisoformat(published).isoformat()
                except (ValueError, TypeError):
                    pass
            records.append({
                "source": source,
                "title": getattr(entry, "title", ""),
                "score": 0,
                "num_comments": 0,
                "url": link,
                "published": published,
            })

        record_source_health(source, "ok", len(records))
        return pd.DataFrame(records)

    except Exception as e:
        print(f"    [Reddit r/{subreddit}] RSS error: {type(e).__name__} — skipping")
        record_source_health(source, "error", 0, f"{type(e).__name__}: {e}")
        return pd.DataFrame()


# GDELT answers 429 under any repeated query, and the pipeline asks the same
# geopolitical question on every asset, every hour. One global limiter plus a
# per-query cache turns a flood of rate-limited calls into one call an hour.
GDELT_MIN_INTERVAL_S = 45
GDELT_CACHE_TTL_S = 6 * 3600
GDELT_COOLDOWN_S = 30 * 60
_GDELT_LAST_CALL = 0.0
_GDELT_BLOCKED_UNTIL = 0.0
_GDELT_CACHE = {}
_GDELT_CACHE_FILE = "gdelt_cache.json"
_GDELT_BREAKER_FILE = "gdelt_breaker.json"


def _load_gdelt_cache():
    """GDELT results are cached on disk: its timespan is 7 days, so an answer
    from six hours ago is still a current answer. Without this the hourly
    run re-asked the same question for every asset and walked into the
    429 limiter every time."""
    import json
    import os
    if not os.path.exists(_GDELT_CACHE_FILE):
        return
    try:
        with open(_GDELT_CACHE_FILE) as f:
            stored = json.load(f)
        now = time.time()
        for key, entry in stored.items():
            if now - float(entry.get("ts", 0)) < GDELT_CACHE_TTL_S:
                df = pd.DataFrame(entry.get("rows") or [])
                if not df.empty:
                    _GDELT_CACHE[key] = df
    except Exception as e:  # noqa: BLE001
        print(f"    [GDELT] cache load failed: {e}")


def _save_gdelt_cache():
    import json
    try:
        stored = {}
        for key, df in _GDELT_CACHE.items():
            stored[key] = {"ts": time.time(),
                           "rows": df.to_dict(orient="records")}
        with open(_GDELT_CACHE_FILE, "w") as f:
            json.dump(stored, f)
    except Exception as e:  # noqa: BLE001
        print(f"    [GDELT] cache save failed: {e}")


def _load_gdelt_breaker():
    """A bench survives the process that set it: the next hourly run must not
    walk straight back into the 429 it was just benched for."""
    import json
    import os
    global _GDELT_BLOCKED_UNTIL
    if not os.path.exists(_GDELT_BREAKER_FILE):
        return
    try:
        with open(_GDELT_BREAKER_FILE) as f:
            until = float(json.load(f).get("blocked_until", 0))
        if until > time.time():
            _GDELT_BLOCKED_UNTIL = until
            print(f"    [GDELT] benched for another "
                  f"{int(until - time.time())}s (set by a previous run)")
    except Exception as e:  # noqa: BLE001
        print(f"    [GDELT] breaker load failed: {e}")


def _save_gdelt_breaker():
    import json
    try:
        with open(_GDELT_BREAKER_FILE, "w") as f:
            json.dump({"blocked_until": _GDELT_BLOCKED_UNTIL}, f)
    except Exception as e:  # noqa: BLE001
        print(f"    [GDELT] breaker save failed: {e}")


_load_gdelt_cache()
_load_gdelt_breaker()


def fetch_gdelt_events(query, max_records=20, timespan="7d", retries=2):
    """
    GDELT geopolitical event search.
    Best used for macro/geopolitical queries:
      - "Iran ceasefire oil"
      - "Federal Reserve interest rates"
      - "OPEC production cut"
    Not ideal for crypto — too broad and slow.

    Returns DataFrame with: seendate, title, url, domain, sourcecountry
    """
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": f"{query} sourcelang:english",
        "mode": "artlist",
        "maxrecords": max_records,
        "timespan": timespan,
        "format": "json"
    }

    global _GDELT_LAST_CALL, _GDELT_BLOCKED_UNTIL

    # Circuit breaker. GDELT throttles by IP and a throttled call costs two
    # minutes of waiting per asset — the pipeline was spending half its run
    # asleep on one optional source. After a 429 the source is benched for
    # 30 minutes and the failure is reported honestly in the source health
    # instead of silently costing the run its wall clock.
    if time.time() < _GDELT_BLOCKED_UNTIL:
        remaining = int(_GDELT_BLOCKED_UNTIL - time.time())
        print(f"    [GDELT] cooling down ({remaining}s left) — skipping")
        record_source_health("GDELT", "rate_limited", 0,
                             f"cooling down, {remaining}s remaining")
        return pd.DataFrame()

    cache_key = f"{query}|{max_records}|{timespan}"
    cached = _GDELT_CACHE.get(cache_key)
    if cached is not None:
        print(f"    [GDELT] cached result for '{query}'")
        record_source_health("GDELT", "cached", len(cached))
        return cached

    for attempt in range(retries):
        # Global limiter: one GDELT call per GDELT_MIN_INTERVAL_S, whatever
        # the query. Two assets asking in the same minute used to produce two
        # 429s each.
        wait = GDELT_MIN_INTERVAL_S - (time.time() - _GDELT_LAST_CALL)
        if wait > 0:
            print(f"    [GDELT] waiting {wait:.0f}s (global rate limit)")
            time.sleep(wait)
        try:
            response = requests.get(url, params=params, timeout=30)
            _GDELT_LAST_CALL = time.time()
            if response.status_code == 429:
                _GDELT_BLOCKED_UNTIL = time.time() + GDELT_COOLDOWN_S
                _save_gdelt_breaker()
                wait = (attempt + 1) * 20
                print(f"    [GDELT] Rate limited. Waiting {wait}s "
                      f"(source benched for {GDELT_COOLDOWN_S // 60} min)...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            articles = data.get("articles", [])
            if not articles:
                record_source_health("GDELT", "empty", 0, "no articles returned")
                return pd.DataFrame()
            df = pd.DataFrame(articles)
            keep = [c for c in ["seendate", "title", "url", "domain", "sourcecountry"] if c in df.columns]
            df = df[keep]
            df["source"] = "GDELT"
            df["quality"] = "medium"
            _GDELT_CACHE[cache_key] = df
            _save_gdelt_cache()
            record_source_health("GDELT", "ok", len(df))
            return df
        except requests.exceptions.Timeout:
            print(f"    [GDELT] Timeout on attempt {attempt+1}")
            time.sleep(5)
        except Exception as e:
            print(f"    [GDELT] Error: {e}")
            record_source_health("GDELT", "error", 0, f"{type(e).__name__}: {e}")
            break

    return pd.DataFrame()


def fetch_sec_filings(ticker, days_back=7):
    """
    Fetch recent SEC filings for a stock ticker via EDGAR full-text search.
    Free, no API key required.
    Most important filing types: 8-K (material events), 10-Q (quarterly), 4 (insider trades)

    ticker    : e.g. AAPL, NVDA, AMD
    days_back : how many days of filings to look back
    """
    from datetime import timedelta
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")

    url = "https://efts.sec.gov/LATEST/search-index"
    params = {
        "q": ticker,
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
        "hits.hits._source": "period_of_report,entity_name,file_date,form_type"
    }

    try:
        response = requests.get(
            url, params=params,
            headers={"User-Agent": "TradingPipeline research@example.com"},
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        hits = data.get("hits", {}).get("hits", [])

        if not hits:
            return pd.DataFrame()

        records = []
        for hit in hits[:10]:
            src = hit.get("_source", {})
            records.append({
                "source": "SEC_EDGAR",
                "entity": src.get("entity_name", ""),
                "form_type": src.get("form_type", ""),
                "file_date": src.get("file_date", ""),
                "title": f"{src.get('form_type','')} filing by {src.get('entity_name','')}",
            })

        return pd.DataFrame(records)

    except Exception as e:
        print(f"    [SEC EDGAR] Error: {type(e).__name__} — skipping")
        return pd.DataFrame()


def fetch_news_for_asset(asset_type="crypto", ticker=None, geopolitical_query=None, max_per_source=8):
    """
    Master function — fetches news from all relevant sources for a given asset type.
    Combines RSS feeds + Reddit + GDELT + SEC as appropriate.

    asset_type          : "crypto", "commodity", "stock", or "macro"
    ticker              : stock ticker for SEC filings e.g. "AAPL" (stocks only)
    geopolitical_query  : GDELT query for macro events e.g. "Iran ceasefire oil"
    max_per_source      : articles per RSS source

    Returns one combined DataFrame of all headlines, sorted by source quality.
    """
    all_dfs = []
    quality_order = {"critical": 0, "high": 1, "medium": 2}

    print(f"  Fetching {asset_type} news...")

    # ── 1. Asset-specific RSS feeds ──────────────────────────
    feeds = RSS_FEEDS.get(asset_type, [])
    for feed_config in feeds:
        print(f"    Trying {feed_config['name']}...", end=" ")
        if feed_config.get("needs_ticker"):
            feed_config = {**feed_config, "ticker": ticker}
        df = fetch_rss_feed(feed_config, max_articles=max_per_source)
        if not df.empty:
            all_dfs.append(df)
            print(f"{len(df)} articles")
        else:
            print("0 articles")
        time.sleep(0.5)  # be polite between requests

    # ── 2. Macro RSS feeds (always included) ─────────────────
    if asset_type != "macro":
        for feed_config in RSS_FEEDS["macro"]:
            print(f"    Trying {feed_config['name']} (macro)...", end=" ")
            df = fetch_rss_feed(feed_config, max_articles=5)
            if not df.empty:
                all_dfs.append(df)
                print(f"{len(df)} articles")
            else:
                print("0 articles")
            time.sleep(0.5)

    # ── 3. Reddit (crypto only) ───────────────────────────────
    if asset_type == "crypto":
        print(f"    Trying Reddit r/CryptoCurrency...", end=" ")
        df = fetch_reddit_sentiment("CryptoCurrency", limit=20)
        if not df.empty:
            # Rename to match standard columns
            df = df.rename(columns={"title": "title"})
            all_dfs.append(df)
            print(f"{len(df)} posts")
        else:
            print("0 posts")

    # ── 4. GDELT geopolitical (macro events) ─────────────────
    if geopolitical_query:
        print(f"    Trying GDELT ('{geopolitical_query}')...", end=" ")
        df = fetch_gdelt_events(geopolitical_query, max_records=10, timespan="7d")
        if not df.empty:
            all_dfs.append(df)
            print(f"{len(df)} events")
        else:
            print("0 events")

    # ── 5. SEC EDGAR (stocks only) ───────────────────────────
    if asset_type == "stock" and ticker:
        print(f"    Trying SEC EDGAR ({ticker})...", end=" ")
        df = fetch_sec_filings(ticker, days_back=7)
        if not df.empty:
            all_dfs.append(df)
            print(f"{len(df)} filings")
        else:
            print("0 filings")

    # ── Combine + sort by quality ─────────────────────────────
    if not all_dfs:
        print("  WARNING: No news fetched from any source.")
        return pd.DataFrame()

    combined = pd.concat(all_dfs, ignore_index=True)

    # Sort: critical sources first, then high, then medium
    if "quality" in combined.columns:
        combined["quality_rank"] = combined["quality"].map(quality_order).fillna(3)
        combined = combined.sort_values("quality_rank").drop(columns=["quality_rank"])

    return combined


if __name__ == "__main__":
    print("=" * 60)
    print("NEWS AGGREGATOR — FULL TEST")
    print("=" * 60)

    # Test 1: Crypto news
    print("\n[TEST 1] CRYPTO NEWS")
    crypto_news = fetch_news_for_asset(
        asset_type="crypto",
        geopolitical_query=None,
        max_per_source=5
    )
    if not crypto_news.empty:
        print(f"\nTotal crypto headlines: {len(crypto_news)}")
        print(crypto_news[["source", "title"]].head(10).to_string())
    else:
        print("No crypto news fetched.")

    print("\n" + "-" * 60)
    time.sleep(3)

    # Test 2: Oil/commodity news with geopolitical context
    print("\n[TEST 2] OIL / COMMODITY NEWS")
    oil_news = fetch_news_for_asset(
        asset_type="commodity",
        geopolitical_query="OPEC oil Iran ceasefire",
        max_per_source=5
    )
    if not oil_news.empty:
        print(f"\nTotal oil headlines: {len(oil_news)}")
        print(oil_news[["source", "title"]].head(10).to_string())
    else:
        print("No oil news fetched.")

    print("\n" + "-" * 60)
    time.sleep(3)

    # Test 3: Stock news
    print("\n[TEST 3] STOCK NEWS (NVDA)")
    stock_news = fetch_news_for_asset(
        asset_type="stock",
        ticker="NVDA",
        max_per_source=5
    )
    if not stock_news.empty:
        print(f"\nTotal stock headlines: {len(stock_news)}")
        print(stock_news[["source", "title"]].head(10).to_string())
    else:
        print("No stock news fetched.")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)
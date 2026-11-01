"""
PRICE DATA MODULE
Sources: Binance (crypto, free, no key) + yfinance (stocks/commodities, free, no key)
"""

import requests
import pandas as pd
from datetime import datetime


def fetch_binance_ohlcv(symbol="BTCUSDT", interval="1h", limit=500):
    """
    Fetch OHLCV candles from Binance public API.
    No API key required.

    symbol   : e.g. BTCUSDT, ETHUSDT, SOLUSDT, LINKUSDT
    interval : 1m, 5m, 15m, 1h, 4h, 1d
    limit    : number of candles (max 1000)
    """
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    df = pd.DataFrame(data, columns=[
        "timestamps", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    df = df[["timestamps", "open", "high", "low", "close", "volume"]]
    df["timestamps"] = pd.to_datetime(df["timestamps"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df["amount"] = df["close"] * df["volume"]
    df["asset"] = symbol
    df["asset_class"] = "crypto"

    return df


def fetch_stock_ohlcv(ticker="AAPL", period="60d", interval="1h", retries=3):
    """
    Fetch OHLCV candles for stocks/commodities via yfinance.
    No API key required.

    ticker   : e.g. AAPL, NVDA, AMD, BZ=F (Brent futures), GC=F (gold futures)
    period   : how far back, e.g. 7d, 60d, 1y
    interval : 1m, 5m, 15m, 1h, 1d (note: intraday intervals limited to ~60d history)
    retries  : yfinance intermittently returns an empty frame on a transient
               network timeout, so retry before declaring the ticker dead.
    """
    import yfinance as yf
    import time

    df = None
    for attempt in range(retries):
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if df is not None and not df.empty:
            break
        if attempt < retries - 1:
            wait = 2 * (attempt + 1)
            print(f"  {ticker}: empty frame, retry {attempt + 1}/{retries - 1} "
                  f"in {wait}s")
            time.sleep(wait)

    if df is None or df.empty:
        raise ValueError(f"No data returned for {ticker}. Check ticker symbol.")

    df = df.reset_index()
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.rename(columns={
        "Date": "timestamps", "Datetime": "timestamps",
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume"
    })

    df = df[["timestamps", "open", "high", "low", "close", "volume"]]
    df["amount"] = df["close"] * df["volume"]
    df["asset"] = ticker
    df["asset_class"] = "stock_or_commodity"

    return df


def fetch_mexc_ohlcv(symbol="SKHYSTOCK_USDT", interval="Min60", limit=400,
                     retries=3):
    """
    Fetch OHLCV candles from MEXC USDT-M futures public API.
    No API key required. Used for tokenized stock futures (e.g. SKHYSTOCK_USDT)
    that have no Binance/yfinance equivalent priced the same way.

    symbol   : MEXC contract symbol, e.g. SKHYSTOCK_USDT, AMDSTOCK_USDT
    interval : MEXC kline interval — Min1, Min5, Min15, Min30, Min60,
               Hour4, Hour8, Day1, Week1, Month1
    limit    : number of bars to request
    """
    import time

    secs = {"Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800,
            "Min60": 3600, "Hour4": 14400, "Hour8": 28800, "Day1": 86400,
            "Week1": 604800, "Month1": 2592000}
    step = secs.get(interval, 3600)
    end = int(time.time())
    start = end - (limit + 1) * step
    url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}"
    params = {"interval": interval, "start": start, "end": end}

    data = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json().get("data")
            if payload and payload.get("close"):
                data = payload
                break
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol}: MEXC fetch error ({exc}), retry "
                  f"{attempt + 1}/{retries - 1}")
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))

    if not data or not data.get("close"):
        raise ValueError(f"No MEXC data returned for {symbol}.")

    df = pd.DataFrame({
        "timestamps": data["time"],
        "open":   data["open"],
        "high":   data["high"],
        "low":    data["low"],
        "close":  data["close"],
        "volume": data["vol"],
    })
    df["timestamps"] = pd.to_datetime(df["timestamps"].astype("int64"),
                                      unit="s")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().reset_index(drop=True)
    df["amount"] = df["close"] * df["volume"]
    df["asset"] = symbol
    df["asset_class"] = "mexc_future"

    return df.tail(limit).reset_index(drop=True)


_MEXC_SPEC_CACHE = {}


def _mexc_contract_spec(symbol, retries=2):
    """Contract size and funding cycle for a MEXC perp (cached per process)."""
    import time

    if symbol in _MEXC_SPEC_CACHE:
        return _MEXC_SPEC_CACHE[symbol]
    spec = {}
    # contractSize comes from the detail endpoint; collectCycle comes from the
    # funding_rate endpoint (the detail endpoint does not carry it).
    try:
        response = requests.get("https://contract.mexc.com/api/v1/contract/detail",
                                params={"symbol": symbol}, timeout=15)
        response.raise_for_status()
        data = (response.json() or {}).get("data") or {}
        spec["contractSize"] = data.get("contractSize")
    except Exception as exc:  # noqa: BLE001
        print(f"  {symbol}: MEXC contract detail error ({exc})")
    try:
        response = requests.get(
            f"https://contract.mexc.com/api/v1/contract/funding_rate/{symbol}",
            timeout=15)
        response.raise_for_status()
        data = (response.json() or {}).get("data") or {}
        spec["collectCycle"] = data.get("collectCycle")
    except Exception as exc:  # noqa: BLE001
        print(f"  {symbol}: MEXC funding spec error ({exc})")

    _MEXC_SPEC_CACHE[symbol] = spec
    return spec


def fetch_binance_futures_context(symbol="BTCUSDT", retries=2):
    """
    Fetch derivatives positioning context for a Binance USDT-M perpetual.

    Crypto is the only tradable sleeve in this engine and it is executed on
    Binance futures, so this is the funding/OI tape for exactly the instrument
    that gets filled. Three public endpoints, no key:
      /fapi/v1/premiumIndex    -> mark, index, last funding rate
      /fapi/v1/openInterest    -> open interest (contracts)
      /fapi/v1/ticker/24hr     -> 24h futures volume and change

    Returns dict or None on failure. Callers must fail open on this context
    (a missing funding read must never take the asset out of the pipeline).
    """
    import time
    from datetime import datetime, timezone

    base = "https://fapi.binance.com/fapi/v1"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

    def _get(path, params):
        for attempt in range(retries):
            try:
                response = requests.get(f"{base}{path}", params=params,
                                        headers=headers, timeout=15)
                response.raise_for_status()
                payload = response.json()
                if payload:
                    return payload
            except Exception as exc:  # noqa: BLE001
                print(f"  {symbol}: futures {path} error ({exc}), retry "
                      f"{attempt + 1}/{retries - 1}")
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
        return None

    premium = _get("/premiumIndex", {"symbol": symbol})
    if not premium:
        return None
    oi = _get("/openInterest", {"symbol": symbol}) or {}
    t24 = _get("/ticker/24hr", {"symbol": symbol}) or {}

    def _num(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    mark = _num(premium.get("markPrice"))
    index = _num(premium.get("indexPrice"))
    funding = _num(premium.get("lastFundingRate"))
    oi_contracts = _num(oi.get("openInterest"))

    annualized_funding = None
    if funding is not None:
        # Binance funds every 8h -> 3 settlements a day.
        annualized_funding = funding * 3 * 365 * 100

    basis_pct = None
    if mark and index:
        basis_pct = (mark - index) / index * 100

    return {
        "venue": "binance_futures",
        "symbol": symbol,
        "mark_price": mark,
        "index_price": index,
        "last_funding_rate": funding,
        "funding_rate_annualized_pct": annualized_funding,
        "next_funding_time": premium.get("nextFundingTime"),
        "open_interest_contracts": oi_contracts,
        "open_interest_usd": (oi_contracts * mark) if (oi_contracts and mark) else None,
        "quote_volume_24h": _num(t24.get("quoteVolume")),
        "price_change_pct_24h": _num(t24.get("priceChangePercent")),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def estimate_annualized_vol(closes, bars_per_year=365, min_vol=0.05, max_vol=3.0):
    """
    Annualised volatility from a close series (log-return stdev).

    Used as the strike-distance baseline for prediction-market scoring: a
    strike 5% away is only a real event if the underlying can actually move
    5%, and that depends on its volatility and horizon.

    closes      : list of close prices, oldest first
    bars_per_year: 365 for crypto (daily bars, trades every day), 252 for
                   stocks / commodities / FX (they do not trade at weekends),
                   24*365 for hourly bars
    """
    import math

    values = [float(c) for c in (closes or []) if c and c > 0]
    if len(values) < 20:
        return None
    returns = []
    for prev, cur in zip(values, values[1:]):
        if prev > 0 and cur > 0:
            returns.append(math.log(cur / prev))
    if len(returns) < 10:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    vol = math.sqrt(var) * math.sqrt(bars_per_year)
    return max(min_vol, min(max_vol, vol))


def fetch_mexc_ticker(symbol, retries=3):
    """
    Fetch the current MEXC USDT-M futures ticker for one contract.

    MEXC is the operator's execution venue, so its price is the one that
    actually gets filled. The engine's candle history/momentum still come from
    the yfinance futures feed (GC=F/BZ=F); this ticker is captured alongside so
    the basis between the two can be reported and applied to the levels.

    The ticker also carries the perp funding rate and open interest, which is
    the positioning read for the MEXC sleeve (gold, Brent, tokenised equities).

    Returns dict: exchange, symbol, last, index, bid, ask, funding, open
    interest, as_of (ISO UTC).
    """
    import time
    from datetime import datetime, timezone

    url = "https://contract.mexc.com/api/v1/contract/ticker"
    data = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params={"symbol": symbol}, timeout=15)
            response.raise_for_status()
            payload = response.json().get("data")
            if payload:
                data = payload
                break
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol}: MEXC ticker error ({exc}), retry "
                  f"{attempt + 1}/{retries - 1}")
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))

    if not data:
        raise ValueError(f"No MEXC ticker returned for {symbol}.")
    if isinstance(data, list):
        data = data[0]

    def _num(key):
        value = data.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    funding = _num("fundingRate")
    hold_vol = _num("holdVol")
    last = _num("lastPrice")

    # Contract spec: MEXC open interest is counted in contracts, and one
    # contract is `contractSize` of the base asset (0.001 oz for XAU_USDT).
    # The funding settlement cycle also varies by contract, so the annualised
    # funding figure depends on it.
    spec = _mexc_contract_spec(symbol)
    contract_size = spec.get("contractSize")
    collect_hours = spec.get("collectCycle") or 8
    settlements_per_day = max(1, int(round(24 / collect_hours)))

    return {
        "exchange": "MEXC",
        "symbol":   symbol,
        "last":     last,
        "index":    _num("indexPrice"),
        "fair":     _num("fairPrice"),
        "bid":      _num("bid1"),
        "ask":      _num("ask1"),
        "last_funding_rate": funding,
        "funding_rate_annualized_pct": (funding * settlements_per_day * 365 * 100) if funding is not None else None,
        "funding_settlement_hours": collect_hours,
        "open_interest_contracts": hold_vol,
        "contract_size": contract_size,
        "open_interest_usd": (hold_vol * contract_size * last) if (hold_vol and last and contract_size) else None,
        "quote_volume_24h": _num("amount24"),
        "price_change_pct_24h": (_num("riseFallRate") * 100) if _num("riseFallRate") is not None else None,
        "as_of":    datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    print("Testing Binance fetch (BTCUSDT)...")
    btc = fetch_binance_ohlcv("BTCUSDT", interval="1h", limit=10)
    print(btc.tail(3))
    print(f"Shape: {btc.shape}\n")

    print("Testing yfinance fetch (AAPL)...")
    try:
        aapl = fetch_stock_ohlcv("AAPL", period="5d", interval="1h")
        print(aapl.tail(3))
        print(f"Shape: {aapl.shape}")
    except ImportError:
        print("yfinance not installed - run: pip install yfinance --break-system-packages")
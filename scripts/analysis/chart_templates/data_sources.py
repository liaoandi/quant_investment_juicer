"""
Shared data fetching utilities for chart templates.
Provides options chain data (yfinance primary, NASDAQ fallback) and price data.
"""

import time
from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

_NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


# ── yfinance (primary) ────────────────────────────────────────


def fetch_yf_options(ticker: str, expiry: str) -> pd.DataFrame:
    """Fetch options chain via yfinance.

    Returns DataFrame with columns:
        strike, c_oi, c_volume, c_last, c_bid, c_ask, p_oi, p_volume, p_last, p_bid, p_ask
    """
    t = yf.Ticker(ticker)
    chain = t.option_chain(expiry)

    calls = chain.calls[["strike", "openInterest", "volume", "lastPrice", "bid", "ask"]].copy()
    calls.columns = ["strike", "c_oi", "c_volume", "c_last", "c_bid", "c_ask"]

    puts = chain.puts[["strike", "openInterest", "volume", "lastPrice", "bid", "ask"]].copy()
    puts.columns = ["strike", "p_oi", "p_volume", "p_last", "p_bid", "p_ask"]

    df = pd.merge(calls, puts, on="strike", how="outer").fillna(0)
    for col in ["c_oi", "c_volume", "p_oi", "p_volume"]:
        df[col] = df[col].astype(int)
    return df.sort_values("strike").reset_index(drop=True)


def list_yf_expirations(ticker: str) -> list[str]:
    """List available option expiration dates via yfinance."""
    t = yf.Ticker(ticker)
    return list(t.options)


def get_nearest_expiry_with_oi(ticker: str, asset_class: str = "etf", min_date: str = "") -> str:
    """Find the nearest expiration date that has meaningful OI.

    Tries yfinance first, falls back to NASDAQ.
    """
    if not min_date:
        min_date = datetime.now().strftime("%Y-%m-%d")

    # yfinance
    try:
        expirations = list_yf_expirations(ticker)
        for exp in expirations:
            if exp < min_date:
                continue
            try:
                df = fetch_yf_options(ticker, exp)
                total_oi = df["c_oi"].sum() + df["p_oi"].sum()
                if total_oi > 100:
                    return exp
            except Exception:
                continue
    except Exception:
        pass

    # NASDAQ fallback
    try:
        expirations = list_nasdaq_expirations(ticker, asset_class)
        for exp in expirations:
            if exp < min_date:
                continue
            try:
                df = fetch_nasdaq_options(ticker, exp, asset_class)
                total_oi = df["c_oi"].sum() + df["p_oi"].sum()
                if total_oi > 100:
                    return exp
            except Exception:
                continue
    except Exception:
        pass

    raise ValueError(f"No expiration with sufficient OI for {ticker}")


def fetch_options(ticker: str, expiry: str, asset_class: str = "etf") -> pd.DataFrame:
    """Fetch options chain — yfinance first, NASDAQ fallback.

    Returns DataFrame with columns:
        strike, c_oi, c_volume, c_last, c_bid, c_ask, p_oi, p_volume, p_last, p_bid, p_ask
    """
    try:
        return fetch_yf_options(ticker, expiry)
    except Exception:
        return fetch_nasdaq_options(ticker, expiry, asset_class)


# ── NASDAQ (fallback) ─────────────────────────────────────────


def fetch_nasdaq_options(ticker: str, expiry: str, asset_class: str = "etf") -> pd.DataFrame:
    """Fetch complete options chain from NASDAQ (free, no auth).

    Returns DataFrame with columns:
        strike, c_oi, c_volume, c_last, c_bid, c_ask, p_oi, p_volume, p_last, p_bid, p_ask
    """
    url = f"https://api.nasdaq.com/api/quote/{ticker}/option-chain"
    all_rows = []
    offset = 0

    while True:
        params = {
            "assetclass": asset_class,
            "limit": 200,
            "fromdate": expiry,
            "todate": expiry,
            "callput": "callput",
            "money": "all",
            "type": "all",
            "offset": offset,
        }
        r = requests.get(url, params=params, headers=_NASDAQ_HEADERS, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"NASDAQ API error {r.status_code}: {r.text[:200]}")
        rows = (r.json().get("data") or {}).get("table", {}).get("rows", [])
        real = [row for row in rows if row.get("strike") and row["strike"] != "--"]
        if not real:
            break
        all_rows.extend(real)
        if len(rows) < 200:
            break
        offset += 200
        time.sleep(0.5)  # gentle rate limiting

    if not all_rows:
        raise ValueError(f"No options data for {ticker} expiry {expiry}")

    def _parse_num(val):
        if not val or val == "--":
            return 0.0
        return float(str(val).replace(",", ""))

    records = []
    for row in all_rows:
        records.append({
            "strike": _parse_num(row.get("strike")),
            "c_oi": int(_parse_num(row.get("c_Openinterest"))),
            "c_volume": int(_parse_num(row.get("c_Volume"))),
            "c_last": _parse_num(row.get("c_Last")),
            "c_bid": _parse_num(row.get("c_Bid")),
            "c_ask": _parse_num(row.get("c_Ask")),
            "p_oi": int(_parse_num(row.get("p_Openinterest"))),
            "p_volume": int(_parse_num(row.get("p_Volume"))),
            "p_last": _parse_num(row.get("p_Last")),
            "p_bid": _parse_num(row.get("p_Bid")),
            "p_ask": _parse_num(row.get("p_Ask")),
        })

    return pd.DataFrame(records)


def list_nasdaq_expirations(ticker: str, asset_class: str = "etf") -> list[str]:
    """List available option expiration dates from NASDAQ."""
    url = f"https://api.nasdaq.com/api/quote/{ticker}/option-chain"
    params = {"assetclass": asset_class, "limit": 1, "money": "all", "type": "all"}
    r = requests.get(url, params=params, headers=_NASDAQ_HEADERS, timeout=15)
    data = r.json().get("data") or {}

    # Extract from expirygroup field or month list
    rows = (data.get("table") or {}).get("rows", [])
    groups = set()
    for row in rows:
        eg = row.get("expirygroup", "")
        if eg:
            groups.add(eg)

    # Also check if there's a filterData section
    months = (data.get("filterData") or {}).get("expirationMonths", [])
    dates = []
    for m in months:
        val = m.get("value", "")
        if val:
            dates.append(val)

    return sorted(dates) if dates else sorted(groups)

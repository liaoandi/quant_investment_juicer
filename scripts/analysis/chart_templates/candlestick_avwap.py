#!/usr/bin/env python3
"""
Template A: Candlestick + AVWAP + Moving Averages + Support/Resistance
Covers: candlestick, moving_averages, support_resistance (21 charts)

Usage:
    python scripts/chart_templates/candlestick_avwap.py --ticker GLD --period 6mo
    python scripts/chart_templates/candlestick_avwap.py --ticker 000932.SS --source eastmoney --secid 1.000932
    python scripts/chart_templates/candlestick_avwap.py --ticker GLD --avwap-date 2025-11-04 --ma 20 50 --support 440 --resistance 475
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

# Add parent for shared data fetching
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def fetch_ohlcv(ticker: str, period: str = "6mo", source: str = "auto", secid: str = "") -> pd.DataFrame:
    """Fetch OHLCV data. source: auto, yahoo, eastmoney."""
    if source == "eastmoney" or (source == "auto" and (".SS" in ticker or ".SZ" in ticker or secid)):
        return _fetch_eastmoney(secid or ticker, period)
    return _fetch_yahoo(ticker, period)


def _fetch_yahoo(ticker: str, period: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(ticker, period=period, progress=False)
    if df.empty:
        raise ValueError(f"No data for {ticker}")
    # Flatten multi-level columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    return df


def _fetch_eastmoney(secid: str, period: str) -> pd.DataFrame:
    import requests
    # Map period to days
    days_map = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730}
    days = days_map.get(period, 180)
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "klt": "101", "fqt": "1", "beg": start, "end": end,
    }
    r = requests.get(url, params=params, timeout=15)
    data = r.json().get("data", {})
    klines = data.get("klines", [])
    if not klines:
        raise ValueError(f"No data for {secid}")
    rows = [k.split(",") for k in klines]
    df = pd.DataFrame(rows, columns=["Date", "Open", "Close", "High", "Low", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def compute_avwap(df: pd.DataFrame, anchor_date: str) -> pd.Series:
    """Anchored VWAP from a specific date."""
    anchor = pd.to_datetime(anchor_date)
    mask = df.index >= anchor
    subset = df[mask].copy()
    typical_price = (subset["High"] + subset["Low"] + subset["Close"]) / 3
    cum_tp_vol = (typical_price * subset["Volume"]).cumsum()
    cum_vol = subset["Volume"].cumsum()
    avwap = cum_tp_vol / cum_vol
    return avwap


def plot(df, ticker, ma_periods=None, avwap_date=None, support=None, resistance=None, output=None):
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), height_ratios=[3, 1], sharex=True)
    ax, ax_vol = axes

    # Candlestick
    dates = df.index
    opens, highs, lows, closes = df["Open"], df["High"], df["Low"], df["Close"]
    colors = ["#c75050" if c < o else "#4ca64c" for o, c in zip(opens, closes)]

    width = 0.6
    ax.bar(dates, closes - opens, bottom=np.minimum(opens, closes), width=width, color=colors, edgecolor=colors)
    ax.vlines(dates, lows, highs, colors=colors, linewidth=0.8)

    # Moving averages
    ma_colors = ["#e8a87c", "#85cdca", "#d5a6bd"]
    for i, p in enumerate(ma_periods or []):
        ma = df["Close"].rolling(p).mean()
        ax.plot(dates, ma, label=f"MA{p}", color=ma_colors[i % len(ma_colors)], linewidth=1.5)

    # AVWAP
    if avwap_date:
        avwap = compute_avwap(df, avwap_date)
        ax.plot(avwap.index, avwap, label=f"AVWAP({avwap_date})", color="#6b7b8d", linewidth=2, linestyle="--")

    # Support / Resistance
    if support:
        for s in support:
            ax.axhline(s, color="#4ca64c", linestyle="--", linewidth=1.2, alpha=0.8)
            ax.text(dates[-1], s, f" Support {s}", va="bottom", fontsize=9, color="#4ca64c")
    if resistance:
        for r in resistance:
            ax.axhline(r, color="#c75050", linestyle="--", linewidth=1.2, alpha=0.8)
            ax.text(dates[-1], r, f" Resistance {r}", va="top", fontsize=9, color="#c75050")

    # Volume
    vol_colors = ["#c75050" if c < o else "#4ca64c" for o, c in zip(opens, closes)]
    ax_vol.bar(dates, df["Volume"], width=width, color=vol_colors, alpha=0.6)
    ax_vol.set_ylabel("Volume")

    # Formatting
    ax.set_title(f"{ticker} Daily Chart", fontsize=14)
    ax.set_ylabel("Price")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    plt.tight_layout()

    # Key findings
    latest = df.iloc[-1]
    print(f"\n--- {ticker} Key Findings ---")
    print(f"Latest Close: {latest['Close']:.2f} ({df.index[-1].strftime('%Y-%m-%d')})")
    if ma_periods:
        for p in ma_periods:
            ma_val = df["Close"].rolling(p).mean().iloc[-1]
            pos = "above" if latest["Close"] > ma_val else "below"
            print(f"  MA{p}: {ma_val:.2f} (price is {pos})")
    if avwap_date:
        avwap = compute_avwap(df, avwap_date)
        if not avwap.empty:
            print(f"  AVWAP({avwap_date}): {avwap.iloc[-1]:.2f}")

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
        print(f"Chart saved to {output}")
    else:
        plt.show()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--period", default="6mo")
    parser.add_argument("--source", default="auto", choices=["auto", "yahoo", "eastmoney"])
    parser.add_argument("--secid", default="")
    parser.add_argument("--ma", type=int, nargs="*", default=[20, 50])
    parser.add_argument("--avwap-date", default=None)
    parser.add_argument("--support", type=float, nargs="*")
    parser.add_argument("--resistance", type=float, nargs="*")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    df = fetch_ohlcv(args.ticker, args.period, args.source, args.secid)
    plot(df, args.ticker, args.ma, args.avwap_date, args.support, args.resistance, args.output)


if __name__ == "__main__":
    main()

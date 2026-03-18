#!/usr/bin/env python3
"""
Template E: Bollinger Bands + MACD
Covers: bollinger_bands (4 charts)

Usage:
    python scripts/chart_templates/bollinger_macd.py --ticker GLD --period 1y
    python scripts/chart_templates/bollinger_macd.py --ticker Au99.99 --source eastmoney --secid 113.Au99.99 --period 1y
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd


def fetch_price(ticker, period="1y", source="auto", secid=""):
    if source == "eastmoney" or (source == "auto" and secid):
        return _fetch_eastmoney(secid, period)
    import yfinance as yf
    df = yf.download(ticker, period=period, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _fetch_eastmoney(secid, period):
    import requests
    days_map = {"3mo": 90, "6mo": 180, "1y": 365, "2y": 730}
    days = days_map.get(period, 365)
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
              "fields2": "f51,f52,f53,f54,f55,f56", "klt": "101", "fqt": "1",
              "beg": start, "end": end}
    r = requests.get(url, params=params, timeout=15)
    klines = r.json().get("data", {}).get("klines", [])
    rows = [k.split(",") for k in klines]
    df = pd.DataFrame(rows, columns=["Date", "Open", "Close", "High", "Low", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def plot(df, ticker, bb_period=20, bb_std=2, output=None):
    close = df["Close"]
    dates = df.index

    # Bollinger Bands
    sma = close.rolling(bb_period).mean()
    std = close.rolling(bb_period).std()
    upper = sma + bb_std * std
    lower = sma - bb_std * std

    # MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    hist = macd - signal

    # Plot
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), height_ratios=[2.5, 1], sharex=True)

    # Price + Bollinger
    ax1.plot(dates, close, color="#4a6fa5", linewidth=1.2, label="Close")
    ax1.plot(dates, sma, color="#e8a87c", linewidth=1.5, label=f"SMA({bb_period})")
    ax1.plot(dates, upper, color="#8a7cb8", linewidth=1, linestyle="--", label=f"Upper BB ({bb_std}\u03c3)")
    ax1.plot(dates, lower, color="#8a7cb8", linewidth=1, linestyle="--", label=f"Lower BB ({bb_std}\u03c3)")
    ax1.fill_between(dates, lower, upper, color="#8a7cb8", alpha=0.1)

    ax1.set_title(f"{ticker} Bollinger Bands + MACD", fontsize=14)
    ax1.set_ylabel("Price")
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(True, alpha=0.3)

    # MACD
    colors = ["#4ca64c" if h >= 0 else "#c75050" for h in hist]
    ax2.bar(dates, hist, color=colors, alpha=0.6, width=1)
    ax2.plot(dates, macd, color="#4a6fa5", linewidth=1.2, label="MACD")
    ax2.plot(dates, signal, color="#e8a87c", linewidth=1.2, label="Signal")
    ax2.axhline(0, color="gray", linewidth=0.5)
    ax2.set_ylabel("MACD")
    ax2.legend(loc="upper left", fontsize=9)
    ax2.grid(True, alpha=0.3)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    plt.tight_layout()

    # Key findings
    latest = close.iloc[-1]
    latest_upper = upper.iloc[-1]
    latest_lower = lower.iloc[-1]
    latest_macd = macd.iloc[-1]
    latest_signal = signal.iloc[-1]
    bb_pct = (latest - latest_lower) / (latest_upper - latest_lower) * 100

    print(f"\n--- {ticker} BB+MACD Key Findings ---")
    print(f"Latest Close: {latest:.2f}")
    print(f"BB Upper: {latest_upper:.2f}, Lower: {latest_lower:.2f}")
    print(f"BB %B: {bb_pct:.1f}% (0=lower, 100=upper)")
    print(f"MACD: {latest_macd:.4f}, Signal: {latest_signal:.4f}")
    if latest_macd > latest_signal:
        print("  -> MACD above signal: bullish")
    else:
        print("  -> MACD below signal: bearish")

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
        print(f"Chart saved to {output}")
    else:
        plt.show()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--period", default="1y")
    parser.add_argument("--source", default="auto")
    parser.add_argument("--secid", default="")
    parser.add_argument("--bb-period", type=int, default=20)
    parser.add_argument("--bb-std", type=float, default=2)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    df = fetch_price(args.ticker, args.period, args.source, args.secid)
    plot(df, args.ticker, args.bb_period, args.bb_std, args.output)


if __name__ == "__main__":
    main()

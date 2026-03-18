#!/usr/bin/env python3
"""
Template C: Exponential Curve Fitting + sigma bands
Covers: exponential_fit, price_trend (13 charts)

Usage:
    python scripts/analysis/chart_templates/exponential_fit.py --ticker ^NDX --period 20y
    python scripts/analysis/chart_templates/exponential_fit.py --ticker ^GSPC --period 20y
    python scripts/analysis/chart_templates/exponential_fit.py --ticker 512890.SS --period 5y --source eastmoney --secid 1.512890
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from scipy.optimize import curve_fit


def fetch_price(ticker: str, period: str = "20y", source: str = "auto", secid: str = "") -> pd.DataFrame:
    if source == "eastmoney" or (source == "auto" and secid):
        return _fetch_eastmoney(secid, period)
    import yfinance as yf
    df = yf.download(ticker, period=period, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _fetch_eastmoney(secid, period):
    import requests
    days_map = {"1y": 365, "2y": 730, "3y": 1095, "5y": 1825, "10y": 3650, "20y": 7300}
    days = days_map.get(period, 7300)
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


def exp_func(x, a, b):
    return a * np.exp(b * x)


def plot(df, ticker, output=None):
    close = df["Close"].dropna()
    dates = close.index

    # Convert dates to numeric (days since start)
    x_days = (dates - dates[0]).days.values.astype(float)
    y = close.values

    # Fit exponential curve
    try:
        popt, _ = curve_fit(exp_func, x_days, y, p0=[y[0], 1e-4], maxfev=10000)
    except Exception as e:
        print(f"Curve fit failed: {e}")
        return

    fitted = exp_func(x_days, *popt)

    # Calculate residuals for sigma bands
    log_resid = np.log(y) - np.log(fitted)
    sigma = np.std(log_resid)

    upper_1s = fitted * np.exp(sigma)
    lower_1s = fitted * np.exp(-sigma)
    upper_2s = fitted * np.exp(2 * sigma)
    lower_2s = fitted * np.exp(-2 * sigma)

    # Plot
    fig, ax = plt.subplots(figsize=(14, 8))

    ax.scatter(dates, y, s=3, color="#4a6fa5", alpha=0.5, label="Daily Stock Close Price", zorder=2)
    ax.plot(dates, fitted, color="#c75050", linewidth=2, label="Fitted Curve", zorder=3)
    ax.plot(dates, upper_1s, color="#8a7cb8", linewidth=1.2, linestyle="--", label="Upper 1\u03c3")
    ax.plot(dates, lower_1s, color="#8a7cb8", linewidth=1.2, linestyle="--", label="Lower 1\u03c3")
    ax.plot(dates, upper_2s, color="#d4a574", linewidth=1.2, linestyle="--", label="Upper 2\u03c3")
    ax.plot(dates, lower_2s, color="#d4a574", linewidth=1.2, linestyle="--", label="Lower 2\u03c3")

    # Fill bands
    ax.fill_between(dates, lower_1s, upper_1s, color="#8a7cb8", alpha=0.1)
    ax.fill_between(dates, lower_2s, upper_2s, color="#d4a574", alpha=0.05)

    ax.set_title(f"Exponential Curve Fitting for {ticker}", fontsize=14)
    ax.set_xlabel("Date")
    ax.set_ylabel("Price")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    plt.tight_layout()

    # Key findings
    latest_price = y[-1]
    latest_fitted = fitted[-1]
    z_score = log_resid[-1] / sigma

    print(f"\n--- {ticker} Exponential Fit Key Findings ---")
    print(f"Latest Price: {latest_price:.2f}")
    print(f"Fitted Value: {latest_fitted:.2f}")
    print(f"Z-Score: {z_score:.2f}")
    if z_score > 2:
        print(f"  -> Price is ABOVE 2 sigma (significantly overvalued)")
    elif z_score > 1:
        print(f"  -> Price is above 1 sigma (moderately overvalued)")
    elif z_score < -2:
        print(f"  -> Price is BELOW 2 sigma (significantly undervalued)")
    elif z_score < -1:
        print(f"  -> Price is below 1 sigma (moderately undervalued)")
    else:
        print(f"  -> Price is within 1 sigma (fairly valued)")
    print(f"1-sigma band: {lower_1s[-1]:.2f} - {upper_1s[-1]:.2f}")
    print(f"2-sigma band: {lower_2s[-1]:.2f} - {upper_2s[-1]:.2f}")

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
        print(f"Chart saved to {output}")
    else:
        plt.show()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--source", default="auto")
    parser.add_argument("--secid", default="")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    df = fetch_price(args.ticker, args.period, args.source, args.secid)
    plot(df, args.ticker, args.output)


if __name__ == "__main__":
    main()

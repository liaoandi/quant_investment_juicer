#!/usr/bin/env python3
"""
Template D: Gamma Exposure (GEX) Profile
Covers: gamma_exposure (3 charts)
Data source: NASDAQ (free, complete OI) + yfinance (current price)

Usage:
    python scripts/analysis/chart_templates/gamma_exposure.py --ticker GLD --expiry 2026-03-20
    python scripts/analysis/chart_templates/gamma_exposure.py --ticker KWEB
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm
from data_sources import fetch_nasdaq_options


# Default IV assumption when NASDAQ doesn't provide it
DEFAULT_IV = 0.30


def bs_gamma(S, K, T, r, sigma):
    """Black-Scholes Gamma."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    return norm.pdf(d1) / (S * sigma * np.sqrt(T))


def estimate_iv(strike, current_price, base_iv=DEFAULT_IV):
    """Simple IV smile approximation: higher IV for OTM options."""
    moneyness = abs(strike - current_price) / current_price
    return base_iv * (1 + 0.5 * moneyness)


def compute_gex(ticker: str, expiry: str, r: float = 0.05):
    """Compute Gamma Exposure profile using NASDAQ options data."""
    current_price = yf.Ticker(ticker).history(period="1d")["Close"].iloc[-1]

    if not expiry:
        from data_sources import get_nearest_expiry_with_oi
        expiry = get_nearest_expiry_with_oi(ticker)
        print(f"Using expiry: {expiry}")

    # Fetch from NASDAQ
    df = fetch_nasdaq_options(ticker, expiry)
    print(f"NASDAQ data: {len(df)} strikes, Call OI: {df['c_oi'].sum():,}, Put OI: {df['p_oi'].sum():,}")

    # Time to expiry
    exp_date = pd.to_datetime(expiry)
    T = max((exp_date - pd.Timestamp.today().normalize()).days / 365.0, 1 / 365)

    # Filter to meaningful OI
    calls = df[df["c_oi"] > 0][["strike", "c_oi"]].copy()
    puts = df[df["p_oi"] > 0][["strike", "p_oi"]].copy()

    # Spot price range: +/- 20%
    spots = np.linspace(current_price * 0.8, current_price * 1.2, 200)
    gex = np.zeros_like(spots)

    # Precompute IV for each strike
    calls["iv"] = calls["strike"].apply(lambda k: estimate_iv(k, current_price))
    puts["iv"] = puts["strike"].apply(lambda k: estimate_iv(k, current_price))

    call_data = calls[["strike", "c_oi", "iv"]].values
    put_data = puts[["strike", "p_oi", "iv"]].values

    for i, S in enumerate(spots):
        total = 0.0
        # Calls: MM long call -> positive gamma
        for K, oi, iv in call_data:
            g = bs_gamma(S, K, T, r, iv)
            total += g * oi * 100 * S * 0.01
        # Puts: MM short put -> negative gamma
        for K, oi, iv in put_data:
            g = bs_gamma(S, K, T, r, iv)
            total -= g * oi * 100 * S * 0.01
        gex[i] = total

    # Scale to billions
    gex_billions = gex / 1e9

    return spots, gex_billions, current_price, expiry


def plot_gex(spots, gex, current_price, ticker, expiry, output=None):
    fig, ax = plt.subplots(figsize=(14, 7))

    ax.plot(spots, gex, color="#2c7bb6", linewidth=2.5, label="Net Gamma Exposure")

    ax.fill_between(spots, gex, 0, where=(gex >= 0), color="#a6dba0", alpha=0.4, label="Positive Gamma (Stabilizing)")
    ax.fill_between(spots, gex, 0, where=(gex < 0), color="#f4a582", alpha=0.4, label="Negative Gamma (Volatile)")

    ax.axhline(0, color="black", linewidth=0.8)

    # Flip point
    sign_changes = np.where(np.diff(np.sign(gex)))[0]
    flip_price = None
    if len(sign_changes) > 0:
        idx = sign_changes[0]
        # Linear interpolation
        flip_price = spots[idx] + (spots[idx + 1] - spots[idx]) * abs(gex[idx]) / (abs(gex[idx]) + abs(gex[idx + 1]))
        ax.plot(flip_price, 0, "o", color="#d7191c", markersize=10, zorder=5, label="Flip Point")
        ax.axvline(flip_price, color="#d7191c", linestyle="--", linewidth=1.5)
        ax.text(flip_price + 0.3, max(gex) * 0.1, f"Flip: ${flip_price:.2f}",
                color="#d7191c", fontweight="bold", fontsize=11)

    # Current price
    ax.axvline(current_price, color="gray", linestyle="--", linewidth=1.5)
    ax.text(current_price + 0.3, max(gex) * 0.8, f"Current: ${current_price:.2f}",
            color="gray", fontweight="bold", fontsize=11)

    ax.set_title(f"{ticker} Total Gamma Exposure Profile\n"
                 f"(Assumes: Market Makers Long Call / Short Put)\n"
                 f"(Options expiry: {expiry})", fontsize=13)
    ax.set_xlabel("Stock Price ($)", fontsize=12)
    ax.set_ylabel("Total Gamma Exposure ($ Billion / 1% move)", fontsize=12)
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    # Key findings
    print(f"\n--- {ticker} GEX Key Findings (Expiry: {expiry}) ---")
    print(f"Current Price: ${current_price:.2f}")
    if flip_price:
        print(f"Flip Point: ${flip_price:.2f}")
        if current_price > flip_price:
            print("  -> Price ABOVE flip: positive gamma environment (stabilizing)")
        else:
            print("  -> Price BELOW flip: negative gamma environment (volatile)")
    else:
        if all(gex >= 0):
            print("  -> Entirely positive gamma (stabilizing)")
        elif all(gex <= 0):
            print("  -> Entirely negative gamma (volatile)")

    # Find GEX minimal (most negative gamma point)
    min_idx = np.argmin(gex)
    if gex[min_idx] < 0:
        print(f"GEX Minimal: ${spots[min_idx]:.2f} (most volatile point)")

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
        print(f"Chart saved to {output}")
    else:
        plt.show()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--expiry", default="")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    spots, gex, price, expiry = compute_gex(args.ticker, args.expiry)
    plot_gex(spots, gex, price, args.ticker, expiry, args.output)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Template B: Options Open Interest Distribution (Put/Call Wall)
Covers: options_oi, put_call_wall (10 charts)
Data source: NASDAQ (free, no auth, complete OI)

Usage:
    python scripts/analysis/chart_templates/options_oi.py --ticker GLD --expiry 2026-03-20
    python scripts/analysis/chart_templates/options_oi.py --ticker KWEB --side both
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import yfinance as yf
from data_sources import fetch_options


def fetch_data(ticker: str, expiry: str) -> tuple:
    """Fetch options OI from NASDAQ + current price from yfinance."""
    current_price = yf.Ticker(ticker).history(period="1d")["Close"].iloc[-1]

    if not expiry:
        # Pick next monthly expiry (3rd Friday pattern)
        from data_sources import get_nearest_expiry_with_oi
        expiry = get_nearest_expiry_with_oi(ticker)
        print(f"Using expiry: {expiry}")

    df = fetch_options(ticker, expiry)
    print(f"NASDAQ data: {len(df)} strikes, Call OI: {df['c_oi'].sum():,}, Put OI: {df['p_oi'].sum():,}")
    return df, current_price, expiry


def plot_oi(df, current_price, ticker, expiry, side="both", output=None):
    fig, ax = plt.subplots(figsize=(14, 7))

    # Filter to strikes near current price (+/- 15%)
    lo = current_price * 0.85
    hi = current_price * 1.15
    near = df[(df["strike"] >= lo) & (df["strike"] <= hi)].copy()

    bar_width = (hi - lo) / max(len(near), 1) * 0.35

    if side in ("both", "put"):
        ax.bar(near["strike"] - bar_width / 2, near["p_oi"],
               width=bar_width, color="#c98a7d", alpha=0.8, label="Put OI")
    if side in ("both", "call"):
        ax.bar(near["strike"] + bar_width / 2, near["c_oi"],
               width=bar_width, color="#7dac98", alpha=0.8, label="Call OI")

    # Current price line
    ax.axvline(current_price, color="#4a5568", linestyle="--", linewidth=2,
               label=f"Current: ${current_price:.2f}")

    # Find and annotate Put Wall
    puts_with_oi = near[near["p_oi"] > 0]
    if not puts_with_oi.empty and side in ("both", "put"):
        pw = puts_with_oi.loc[puts_with_oi["p_oi"].idxmax()]
        ax.annotate(f"${pw['strike']:.0f} Put Wall\n(OI: {pw['p_oi']:,.0f})",
                    xy=(pw["strike"], pw["p_oi"]),
                    xytext=(-80, 30), textcoords="offset points",
                    fontsize=10, color="#c75050", fontweight="bold",
                    arrowprops=dict(arrowstyle="->", color="#c75050"),
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="#fff3cd", edgecolor="#c75050"))

    # Find and annotate Call Wall
    calls_with_oi = near[near["c_oi"] > 0]
    if not calls_with_oi.empty and side in ("both", "call"):
        cw = calls_with_oi.loc[calls_with_oi["c_oi"].idxmax()]
        ax.annotate(f"${cw['strike']:.0f} Call Wall\n(OI: {cw['c_oi']:,.0f})",
                    xy=(cw["strike"], cw["c_oi"]),
                    xytext=(30, 30), textcoords="offset points",
                    fontsize=10, color="#2d6a4f", fontweight="bold",
                    arrowprops=dict(arrowstyle="->", color="#2d6a4f"),
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="#d4edda", edgecolor="#2d6a4f"))

    ax.set_title(f"{ticker} Options OI Distribution (Expiry: {expiry})", fontsize=14)
    ax.set_xlabel("Strike Price ($)")
    ax.set_ylabel("Open Interest (OI)")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3, axis="y")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    plt.tight_layout()

    # Key findings
    print(f"\n--- {ticker} Options OI Key Findings (Expiry: {expiry}) ---")
    print(f"Current Price: ${current_price:.2f}")
    if not puts_with_oi.empty:
        pw = puts_with_oi.loc[puts_with_oi["p_oi"].idxmax()]
        print(f"Put Wall: ${pw['strike']:.0f} (OI: {pw['p_oi']:,.0f}) — support")
    if not calls_with_oi.empty:
        cw = calls_with_oi.loc[calls_with_oi["c_oi"].idxmax()]
        print(f"Call Wall: ${cw['strike']:.0f} (OI: {cw['c_oi']:,.0f}) — resistance")

    if output:
        plt.savefig(output, dpi=150, bbox_inches="tight")
        print(f"Chart saved to {output}")
    else:
        plt.show()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--expiry", default="")
    parser.add_argument("--side", default="both", choices=["both", "put", "call"])
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    df, price, expiry = fetch_data(args.ticker, args.expiry)
    plot_oi(df, price, args.ticker, expiry, args.side, args.output)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Quant Juicer Automated Pipeline — run all steps and output alert summary.

Steps:
1. Fetch new Weibo posts (incremental, since last run)
2. Fetch latest prices for tracked instruments
3. Run chart templates, generate updated charts
4. Check price alerts (support/resistance/AVWAP/flip points)
5. Output structured summary for OpenClaw -> Feishu relay

Usage:
    python scripts/automation/auto_pipeline.py
    python scripts/automation/auto_pipeline.py --skip-weibo
    python scripts/automation/auto_pipeline.py --skip-charts
    python scripts/automation/auto_pipeline.py --alerts-only
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"
INGESTION_DIR = SCRIPTS_DIR / "ingestion"
TEMPLATES_DIR = SCRIPTS_DIR / "analysis" / "chart_templates"
OUTPUT_DIR = BASE_DIR / "output" / "pipeline"
VENV_PYTHON = str(BASE_DIR / ".venv" / "bin" / "python3")

# Instruments to track — ticker, template, extra args
INSTRUMENTS = [
    # US ETFs — options-enabled
    {"name": "黄金 (GLD)", "ticker": "GLD", "templates": ["candlestick", "options_oi", "gamma_exposure", "bollinger"]},
    {"name": "中概互联 (KWEB)", "ticker": "KWEB", "templates": ["candlestick", "options_oi", "gamma_exposure"]},
    {"name": "纳指 (^NDX)", "ticker": "^NDX", "templates": ["exponential_fit"]},
    {"name": "标普 (^GSPC)", "ticker": "^GSPC", "templates": ["exponential_fit"]},
    # A-share indices via eastmoney
    {"name": "消费 (000932)", "ticker": "000932.SS", "secid": "1.000932", "templates": ["candlestick"]},
    {"name": "红利 (000922)", "ticker": "000922.SS", "secid": "1.000922", "templates": ["candlestick"]},
]

# Alert thresholds
ALERT_PROXIMITY_PCT = 3.0  # alert when price is within 3% of a key level


def run_cmd(cmd: list[str], timeout: int = 120) -> dict:
    """Run a command and capture output."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(BASE_DIR)
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip() if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": f"Timeout ({timeout}s)"}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e)}


# ---------------------------------------------------------------------------
# Step 1: Fetch Weibo
# ---------------------------------------------------------------------------
def step_fetch_weibo() -> dict:
    """Fetch new posts from Weibo via persistent Playwright profile (page-level scraping).

    Requires one-time setup: python scripts/automation/setup_weibo_profile.py
    """
    cmd = [
        VENV_PYTHON, str(INGESTION_DIR / "ingest_weibo_page.py"),
        "--append",
    ]

    result = run_cmd(cmd, timeout=300)

    combined = result["stdout"] + result["stderr"]
    login_expired = "检测到登录墙" in combined
    new_posts = 0
    for line in result["stdout"].split("\n"):
        if "Posts fetched:" in line:
            try:
                new_posts = int(line.split(":")[1].strip())
            except ValueError:
                pass
    return {"new_posts": new_posts, "login_expired": login_expired, **result}


# ---------------------------------------------------------------------------
# Step 2: Fetch prices and check alerts
# ---------------------------------------------------------------------------
def step_price_alerts() -> list[dict]:
    """Fetch current prices and check against key levels."""
    import yfinance as yf

    alerts = []
    for inst in INSTRUMENTS:
        ticker = inst["ticker"]
        name = inst["name"]
        try:
            t = yf.Ticker(ticker if not inst.get("secid") else ticker)
            hist = t.history(period="5d")
            if hist.empty:
                continue
            price = hist["Close"].iloc[-1]
            prev_close = hist["Close"].iloc[-2] if len(hist) > 1 else price
            change_pct = (price - prev_close) / prev_close * 100

            # Check MA levels
            hist_long = t.history(period="6mo")
            if not hist_long.empty and len(hist_long) >= 50:
                ma20 = hist_long["Close"].rolling(20).mean().iloc[-1]
                ma50 = hist_long["Close"].rolling(50).mean().iloc[-1]

                # Alert if price crosses MA
                if abs(price - ma20) / ma20 * 100 < ALERT_PROXIMITY_PCT:
                    alerts.append({
                        "instrument": name,
                        "type": "MA20",
                        "level": round(ma20, 2),
                        "price": round(price, 2),
                        "message": f"{name} 当前价 {price:.2f} 接近 MA20 ({ma20:.2f})",
                    })
                if abs(price - ma50) / ma50 * 100 < ALERT_PROXIMITY_PCT:
                    alerts.append({
                        "instrument": name,
                        "type": "MA50",
                        "level": round(ma50, 2),
                        "price": round(price, 2),
                        "message": f"{name} 当前价 {price:.2f} 接近 MA50 ({ma50:.2f})",
                    })

            # Alert on significant daily move
            if abs(change_pct) > 2.0:
                direction = "大涨" if change_pct > 0 else "大跌"
                alerts.append({
                    "instrument": name,
                    "type": "daily_move",
                    "price": round(price, 2),
                    "change_pct": round(change_pct, 2),
                    "message": f"{name} {direction} {abs(change_pct):.1f}% (当前 {price:.2f})",
                })

        except Exception as e:
            print(f"[warn] Failed to check {name}: {e}")

    return alerts


# ---------------------------------------------------------------------------
# Step 3: Run chart templates
# ---------------------------------------------------------------------------
TEMPLATE_MAP = {
    "candlestick": ("candlestick_avwap.py", lambda inst: [
        "--ticker", inst["ticker"], "--period", "6mo",
        *(["--source", "eastmoney", "--secid", inst["secid"]] if inst.get("secid") else []),
    ]),
    "options_oi": ("options_oi.py", lambda inst: ["--ticker", inst["ticker"]]),
    "gamma_exposure": ("gamma_exposure.py", lambda inst: ["--ticker", inst["ticker"]]),
    "exponential_fit": ("exponential_fit.py", lambda inst: ["--ticker", inst["ticker"], "--period", "20y"]),
    "bollinger": ("bollinger_macd.py", lambda inst: [
        "--ticker", inst["ticker"], "--period", "1y",
        *(["--source", "eastmoney", "--secid", inst["secid"]] if inst.get("secid") else []),
    ]),
}


def step_generate_charts() -> dict:
    """Generate updated charts for all instruments."""
    today = datetime.now().strftime("%Y%m%d")
    chart_dir = OUTPUT_DIR / today
    chart_dir.mkdir(parents=True, exist_ok=True)

    generated = 0
    failed = 0
    findings = []

    for inst in INSTRUMENTS:
        for tmpl_key in inst.get("templates", []):
            if tmpl_key not in TEMPLATE_MAP:
                continue
            script_name, args_fn = TEMPLATE_MAP[tmpl_key]
            chart_name = f"{inst['ticker'].replace('^', '')}_{tmpl_key}.png"
            output_path = str(chart_dir / chart_name)

            cmd = [
                VENV_PYTHON, str(TEMPLATES_DIR / script_name),
                *args_fn(inst),
                "--output", output_path,
            ]

            result = run_cmd(cmd, timeout=60)
            if result["success"]:
                generated += 1
                # Extract key findings from stdout
                if result["stdout"]:
                    for line in result["stdout"].split("\n"):
                        if "->" in line or "Wall:" in line or "Flip" in line or "Z-Score" in line or "BB %" in line:
                            finding = f"[{inst['name']}] {line.strip()}"
                            findings.append(finding)
                            print(f"  {finding}")
            else:
                failed += 1
                print(f"  [warn] {inst['name']} {tmpl_key} failed: {result['stderr'][:100]}")

    return {"generated": generated, "failed": failed, "chart_dir": str(chart_dir), "findings": findings}


# ---------------------------------------------------------------------------
# Step 4: Build summary for Feishu
# ---------------------------------------------------------------------------
def build_summary_md(weibo_result: dict, alerts: list[dict], charts_result: dict, chart_findings: list[str]) -> str:
    """Format pipeline results as Markdown report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Quant Juicer Daily Report ({now})", ""]

    # Alerts section (most important, at the top)
    lines.append("## Price Alerts")
    lines.append("")
    if alerts:
        lines.append(f"共 {len(alerts)} 条预警：")
        lines.append("")
        lines.append("| 品种 | 类型 | 当前价 | 关键位 | 说明 |")
        lines.append("|------|------|--------|--------|------|")
        for a in alerts:
            level = a.get("level", "")
            level_str = f"{level}" if level else ""
            change = a.get("change_pct", "")
            change_str = f"{change:+.1f}%" if change else ""
            lines.append(f"| {a['instrument']} | {a['type']} | {a['price']} | {level_str}{change_str} | {a['message']} |")
    else:
        lines.append("无预警触发。")
    lines.append("")

    # Weibo
    lines.append("## Weibo Updates")
    lines.append("")
    if weibo_result.get("skipped"):
        lines.append(f"跳过: {weibo_result.get('reason', '')}")
    else:
        n = weibo_result.get("new_posts", 0)
        lines.append(f"新增 {n} 条帖子" if n else "无新帖")
    lines.append("")

    # Chart key findings
    if chart_findings:
        lines.append("## Chart Key Findings")
        lines.append("")
        for f in chart_findings:
            lines.append(f"- {f}")
        lines.append("")

    # Charts generated
    if charts_result:
        lines.append("## Charts Generated")
        lines.append("")
        lines.append(f"成功: {charts_result['generated']}, 失败: {charts_result['failed']}")
        lines.append("")
        chart_dir = charts_result.get("chart_dir", "")
        if chart_dir:
            # List chart files
            from pathlib import Path as _P
            for png in sorted(_P(chart_dir).glob("*.png")):
                rel = os.path.relpath(png, OUTPUT_DIR)
                lines.append(f"![{png.stem}]({rel})")
                lines.append("")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Quant Juicer automated pipeline")
    parser.add_argument("--skip-weibo", action="store_true")
    parser.add_argument("--skip-charts", action="store_true")
    parser.add_argument("--alerts-only", action="store_true")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== Quant Juicer Pipeline ({datetime.now().strftime('%Y-%m-%d %H:%M')}) ===\n")

    # Step 1: Weibo
    weibo_result = {}
    if not args.skip_weibo and not args.alerts_only:
        print("[Step 1] Fetching Weibo posts...")
        weibo_result = step_fetch_weibo()
        if weibo_result.get("login_expired"):
            print("[ALERT] Weibo login expired — please re-run setup_weibo_profile.py")
            sys.exit(1)
        if not weibo_result.get("success"):
            print(f"[ALERT] Weibo fetch failed — {weibo_result.get('stderr', '')[:200]}")
            sys.exit(1)
        print(f"  New posts: {weibo_result.get('new_posts', 0)}\n")

    # Step 2: Price alerts
    print("[Step 2] Checking price alerts...")
    alerts = step_price_alerts()
    for a in alerts:
        print(f"  ! {a['message']}")
    if not alerts:
        print("  No alerts")
    print()

    # Step 3: Charts
    charts_result = {}
    if not args.skip_charts and not args.alerts_only:
        print("[Step 3] Generating charts...")
        charts_result = step_generate_charts()
        print(f"\n  Generated: {charts_result['generated']}, Failed: {charts_result['failed']}\n")

    # Step 4: Summary
    chart_findings = charts_result.get("findings", []) if charts_result else []
    summary_md = build_summary_md(weibo_result, alerts, charts_result, chart_findings)

    # Save MD report (dated + latest symlink)
    today = datetime.now().strftime("%Y-%m-%d")
    md_path = OUTPUT_DIR / f"daily_report_{today}.md"
    md_path.write_text(summary_md)

    latest_path = OUTPUT_DIR / "latest_report.md"
    latest_path.write_text(summary_md)

    print("\n" + "=" * 50)
    print(summary_md)
    print(f"\nReport saved to: {md_path}")

    sys.exit(0)


if __name__ == "__main__":
    main()

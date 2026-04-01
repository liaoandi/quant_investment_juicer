#!/usr/bin/env python3
"""
Page-level Weibo scraper using a persistent Playwright profile.

Requires one-time setup:
    python scripts/automation/setup_weibo_profile.py

Usage:
    python scripts/ingestion/ingest_weibo_page.py
    python scripts/ingestion/ingest_weibo_page.py --since 2026-03-18 --append
    python scripts/ingestion/ingest_weibo_page.py --scrolls 6 --max-posts 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

DEFAULT_UID = "6937480224"
BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROCESSED_DIR = BASE_DIR / "processed"
DEFAULT_OUTPUT = PROCESSED_DIR / "quant_juicer_weibo_latest.md"
DEFAULT_JSON_OUTPUT = PROCESSED_DIR / "quant_juicer_weibo_page_latest.json"
STATE_FILE = PROCESSED_DIR / ".weibo_fetch_state.json"
PROFILE_DIR = Path.home() / ".weibo_playwright_profile"

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
]

TOPIC_KEYWORDS = {
    "恒生科技": ["恒生科技", "恒科"],
    "中概互联": ["中概互联", "中概股", "KWEB"],
    "创新药": ["创新药", "创新药械"],
    "纳指": ["纳指", "纳斯达克", "QQQ"],
    "美股": ["美股", "标普", "SPY", "SPX", "道指"],
    "黄金": ["黄金", "金价", "GLD", "COMEX"],
    "原油": ["原油", "油价", "油气", "USO", "WTI", "石油"],
    "消费": ["消费", "白酒", "消费50"],
    "红利": ["红利", "红利低波", "高股息"],
    "美元": ["美元", "美元指数", "DXY"],
    "美债": ["美债", "TLT", "VGIT", "国债"],
    "通信": ["通信", "515880"],
    "医疗": ["医疗", "医药", "XLV"],
    "港股": ["港股", "恒指"],
    "A股": ["A股", "大盘", "沪深", "上证"],
}

EXTRACT_POSTS_JS = r"""() => {
  const results = [];
  const seen = new Set();
  // card-wrap is the top-level container for each post
  const cards = Array.from(document.querySelectorAll('div.card-wrap'));

  function imageUrls(root) {
    const urls = [];
    for (const img of root.querySelectorAll('img')) {
      const src = img.currentSrc || img.src || '';
      if (!src) continue;
      // Skip emojis, icons, avatars (crop.*), VIP badges, placeholders
      if (/face\.t\.sinajs|timeline_card_small|n\.sinaimg\.cn\/photo|upload\/2015|h5\.sinaimg|\/crop\.\d/.test(src)) continue;
      if (!/sinaimg\.cn/.test(src)) continue;
      urls.push(src.replace('/orj360/', '/large/').replace('/thumb150/', '/large/'));
    }
    return [...new Set(urls)];
  }

  for (const card of cards) {
    const textEl = card.querySelector('.weibo-text');
    if (!textEl) continue;
    const text = (textEl.innerText || '').trim();
    if (!text || text.length < 8) continue;

    // Time is in span.time inside the card header
    const timeEl = card.querySelector('span.time');
    const timeText = (timeEl?.innerText || '').trim();

    // Post ID from the "全文" link href e.g. /status/5282856878476298
    const fullTextLink = card.querySelector('.weibo-text a[href*="/status/"]');
    const statusMatch = fullTextLink?.href?.match(/\/status\/(\d+)/);
    const id = statusMatch?.[1] || '';

    const key = id || text.slice(0, 80);
    if (seen.has(key)) continue;
    seen.add(key);

    results.push({ id, text, time_text: timeText, images: imageUrls(card) });
  }
  return results;
}"""


def infer_topic(text: str) -> str:
    for display_name, aliases in TOPIC_KEYWORDS.items():
        for alias in aliases:
            if alias in text:
                return display_name
    tags = re.findall(r"#([^#\n]{1,20})#", text)
    if tags:
        return " ".join(tags[:2])
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:20] if first and len(first) <= 20 else "综合"


def parse_time_text(raw: str, now: datetime) -> tuple[datetime | None, str]:
    text = (raw or "").strip()
    if not text:
        return None, "unknown"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        dt = datetime.strptime(text, "%Y-%m-%d")
        return dt, dt.strftime("%Y-%m-%d")
    m = re.fullmatch(r"(\d{1,2})-(\d{2})(?:\s+\d{2}:\d{2})?", text)
    if m:
        try:
            dt = datetime(now.year, int(m.group(1)), int(m.group(2)))
            return dt, dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    if re.fullmatch(r"\d{2}:\d{2}", text):
        dt = now.replace(hour=int(text[:2]), minute=int(text[3:5]), second=0, microsecond=0)
        return dt, dt.strftime("%Y-%m-%d")
    m = re.fullmatch(r"昨天\s*(\d{2}:\d{2})?", text)
    if m:
        base = now - timedelta(days=1)
        if m.group(1):
            hh, mm = m.group(1).split(":")
            base = base.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return base, base.strftime("%Y-%m-%d")
    m = re.fullmatch(r"(\d+)分钟前", text)
    if m:
        dt = now - timedelta(minutes=int(m.group(1)))
        return dt, dt.strftime("%Y-%m-%d")
    m = re.fullmatch(r"(\d+)小时前", text)
    if m:
        dt = now - timedelta(hours=int(m.group(1)))
        return dt, dt.strftime("%Y-%m-%d")
    m = re.fullmatch(r"(\d+)天前", text)
    if m:
        dt = now - timedelta(days=int(m.group(1)))
        return dt, dt.strftime("%Y-%m-%d")
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d")
        return dt, dt.strftime("%Y-%m-%d")
    return None, "unknown"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(posts: list[dict]) -> None:
    if not posts:
        return
    STATE_FILE.write_text(json.dumps({
        "last_post_id": posts[0].get("id", ""),
        "last_date": posts[0].get("date", ""),
        "fetched_at": datetime.now().isoformat(),
        "total_fetched": len(posts),
    }, ensure_ascii=False, indent=2))


def render_markdown(posts: list[dict], uid: str) -> str:
    lines = ["量化投资榨汁机", "", f"https://weibo.com/u/{uid}", ""]
    for post in posts:
        lines += [f"{post['date']} {post['topic']}", "", "原文", "", post["text"], ""]
        for img in post["images"]:
            lines += [f"![图表]({img})", ""]
    return "\n".join(lines).rstrip() + "\n"


async def scrape(args) -> list[dict]:
    if not PROFILE_DIR.exists():
        raise SystemExit(
            f"[error] 未找到 Playwright profile: {PROFILE_DIR}\n"
            "请先运行: python scripts/automation/setup_weibo_profile.py"
        )

    now = datetime.now()
    since_dt = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    target_url = f"https://m.weibo.cn/u/{args.uid}"

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,
            args=LAUNCH_ARGS,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 390, "height": 844},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = await ctx.new_page()
        print(f"[open] {target_url}")
        await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)

        def is_login_wall(html: str) -> bool:
            return "登录注册后查看更多微博" in html or "立即查看" in html

        # Check for login wall on initial load
        content = await page.content()
        if is_login_wall(content):
            await ctx.close()
            raise SystemExit(
                "[error] 检测到登录墙，profile 可能已过期。\n"
                "请重新运行: python scripts/automation/setup_weibo_profile.py"
            )

        posts_by_key: dict[str, dict] = {}
        stale_rounds = 0

        for idx in range(args.scrolls + 1):
            # Re-check login wall mid-scroll
            mid_content = await page.content()
            if is_login_wall(mid_content):
                await ctx.close()
                raise SystemExit(
                    "[error] 检测到登录墙（滚动中途），停止抓取。\n"
                    "请重新运行: python scripts/automation/setup_weibo_profile.py"
                )

            extracted = await page.evaluate(EXTRACT_POSTS_JS)
            before = len(posts_by_key)
            stop = False
            for item in extracted:
                dt, date_str = parse_time_text(item.get("time_text", ""), now)
                if since_dt and dt and dt.date() < since_dt.date():
                    stop = True
                    continue
                key = item.get("id") or item.get("link") or item.get("text", "")[:80]
                if not key or key in posts_by_key:
                    continue
                posts_by_key[key] = {
                    "id": item.get("id", ""),
                    "link": item.get("link", ""),
                    "text": item.get("text", "").strip(),
                    "time_text": item.get("time_text", ""),
                    "date": date_str,
                    "topic": infer_topic(item.get("text", "")),
                    "images": item.get("images", []),
                    "sort_time": dt.isoformat() if dt else "",
                }
            after = len(posts_by_key)
            print(f"[scan] round={idx+1}/{args.scrolls+1} total={after}")

            if stop or idx == args.scrolls:
                break
            if after == before:
                stale_rounds += 1
                if stale_rounds >= 2:
                    print("[stop] 连续两轮无新帖，停止滚动")
                    break
            else:
                stale_rounds = 0

            delay = random.randint(args.delay_min * 1000, args.delay_max * 1000)
            await page.mouse.wheel(0, random.randint(900, 1200))
            await page.wait_for_timeout(delay)

        await ctx.close()

    posts = sorted(posts_by_key.values(), key=lambda x: x["sort_time"], reverse=True)
    return posts[:args.max_posts]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", default=DEFAULT_UID)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_OUTPUT))
    parser.add_argument("--since", default=None, help="只保留此日期之后的帖子 (YYYY-MM-DD)")
    parser.add_argument("--append", action="store_true", help="增量追加到现有文件")
    parser.add_argument("--scrolls", type=int, default=4)
    parser.add_argument("--max-posts", type=int, default=20)
    parser.add_argument("--delay-min", type=int, default=8)
    parser.add_argument("--delay-max", type=int, default=15)
    args = parser.parse_args()

    # Auto-fill --since from state file if not provided
    if not args.since:
        state = load_state()
        last_date = state.get("last_date", "")
        if last_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", last_date):
            args.since = last_date
            print(f"[info] 增量模式，从 {args.since} 开始")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    posts = await scrape(args)

    Path(args.json_output).write_text(json.dumps(posts, ensure_ascii=False, indent=2))

    md = render_markdown(posts, args.uid)
    output = Path(args.output)
    if args.append and output.exists():
        existing = output.read_text()
        match = re.search(r"^\d{4}-\d{2}-\d{2}\s+", existing, re.M)
        if match:
            md = existing[: match.start()] + md.split("\n", 4)[-1] + existing[match.start():]
        else:
            md = existing.rstrip() + "\n" + md
    output.write_text(md)

    save_state(posts)

    print(f"\nPosts fetched: {len(posts)}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
一次性微博登录 — 保存 Playwright 持久化 profile。

Usage:
    python scripts/automation/setup_weibo_profile.py          # 登录并保存
    python scripts/automation/setup_weibo_profile.py --verify # 验证已有 profile（无界面）
"""

import argparse
import sys
from pathlib import Path

PROFILE_DIR = Path.home() / ".weibo_playwright_profile"
MOBILE_URL = "https://m.weibo.cn/u/6937480224"
CARD_SELECTOR = "article, .card, [class*='card'], [class*='weibo-text'], .m-main-inner"
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
]


def login_mode():
    from playwright.sync_api import sync_playwright

    print(f"[info] 打开桌面版微博登录页...")
    print(f"[info] profile 保存到: {PROFILE_DIR}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=LAUNCH_ARGS,
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.new_page()
        page.goto("https://weibo.com/login.php")

        print("[wait] 等待你登录（最多5分钟）...")

        # Wait until URL is no longer the login page (login success)
        try:
            page.wait_for_function(
                "() => !window.location.href.includes('login')",
                timeout=300_000,
            )
        except Exception:
            print("[error] 5分钟内未检测到登录成功，退出")
            ctx.close()
            sys.exit(1)

        print("[ok] 检测到登录成功，跳转到目标页面验证...")
        page.goto(MOBILE_URL)
        page.wait_for_load_state("networkidle", timeout=15000)

        content = page.content()
        login_wall = "登录注册后查看更多微博" in content

        if login_wall:
            print("[warn] m.weibo.cn 还有登录墙，session 可能还没同步，稍等...")
            page.wait_for_timeout(3000)
            page.reload()
            page.wait_for_load_state("networkidle", timeout=10000)
            content = page.content()
            login_wall = "登录注册后查看更多微博" in content

        ctx.close()

    if not login_wall:
        print("[ok] 登录态有效，profile 已保存")
        print("[next] 运行 --verify 确认无界面模式也有效")
    else:
        print("[warn] m.weibo.cn 仍有登录墙，profile 已保存，先跑 --verify 再判断")


def verify_mode():
    from playwright.sync_api import sync_playwright

    if not PROFILE_DIR.exists():
        print(f"[error] profile 不存在: {PROFILE_DIR}")
        print("[tip]  先运行不带参数的版本完成登录")
        sys.exit(1)

    print(f"[info] 无界面模式验证中...")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,
            args=LAUNCH_ARGS,
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.new_page()
        page.goto(MOBILE_URL)
        page.wait_for_load_state("networkidle", timeout=15000)

        content = page.content()
        login_wall = "登录注册后查看更多微博" in content or "立即查看" in content

        try:
            page.wait_for_selector(CARD_SELECTOR, timeout=8000)
        except Exception:
            pass
        count = len(page.query_selector_all(CARD_SELECTOR))
        ctx.close()

    if login_wall:
        print("[fail] 无界面模式有登录墙 — headless 被 Weibo 检测到")
        sys.exit(1)
    elif count > 0:
        print(f"[ok] 无界面模式成功，找到 {count} 个内容元素")
        print("[ready] 可以接入定时任务了")
    else:
        print("[warn] 无登录墙但未找到帖子元素，可能是选择器问题")
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        verify_mode()
    else:
        login_mode()


if __name__ == "__main__":
    main()

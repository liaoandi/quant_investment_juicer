#!/usr/bin/env python3
"""从 Chrome 浏览器提取最新的微博 cookie 并更新 api-keys.env。

前提：用户在 Chrome 中已登录 weibo.com 或 m.weibo.cn。
合并 .weibo.cn + .weibo.com 两个域的 cookie（PC 端登录的 cookie 也能用于移动端 API）。
无需重新登录、无需验证码、无需 Playwright。

Usage:
    python scripts/automation/refresh_weibo_cookie.py          # 提取并更新
    python scripts/automation/refresh_weibo_cookie.py --check   # 仅检查是否有效
"""

import argparse
import os
import re
import sys
from pathlib import Path

API_KEYS_FILE = Path.home() / ".config" / "api-keys.env"
REQUIRED_COOKIES = ["SUB", "SUBP"]
# SSOLoginState triggers 432 anti-crawler on m.weibo.cn (dataabc/weibo-crawler#578)
BLACKLISTED_COOKIES = ["SSOLoginState"]


def extract_weibo_cookies() -> dict[str, str]:
    """从 Chrome cookie 数据库提取微博 cookie（合并 .weibo.cn + .weibo.com）。"""
    try:
        import browser_cookie3
    except ImportError:
        print("[error] browser_cookie3 not installed: pip install browser_cookie3")
        sys.exit(1)

    cookies: dict[str, str] = {}
    for domain in [".weibo.cn", ".weibo.com"]:
        try:
            cj = browser_cookie3.chrome(domain_name=domain)
            for c in cj:
                if "weibo" in c.domain:
                    cookies[c.name] = c.value
        except Exception as e:
            print(f"[warn] Failed to read {domain} cookies: {e}")
    return cookies


def cookies_to_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def validate_cookies(cookies: dict[str, str]) -> bool:
    """检查关键 cookie 是否存在。"""
    for key in REQUIRED_COOKIES:
        if key not in cookies:
            print(f"[warn] Missing required cookie: {key}")
            return False
    return True


def update_api_keys(cookie_str: str) -> None:
    """更新 api-keys.env 中的 WEIBO_COOKIE。"""
    if not API_KEYS_FILE.exists():
        print(f"[error] {API_KEYS_FILE} not found")
        sys.exit(1)

    content = API_KEYS_FILE.read_text()

    # Replace existing WEIBO_COOKIE line
    pattern = r'^WEIBO_COOKIE=.*$'
    replacement = f'WEIBO_COOKIE="{cookie_str}"'

    if re.search(pattern, content, re.MULTILINE):
        new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    else:
        new_content = content.rstrip() + f"\n{replacement}\n"

    API_KEYS_FILE.write_text(new_content)
    print(f"[ok] Updated WEIBO_COOKIE in {API_KEYS_FILE}")


def main():
    parser = argparse.ArgumentParser(description="Refresh Weibo cookie from Chrome")
    parser.add_argument("--check", action="store_true", help="Only check, don't update")
    args = parser.parse_args()

    cookies = extract_weibo_cookies()
    if not cookies:
        print("[error] No weibo cookies found in Chrome. Please login at m.weibo.cn first.")
        sys.exit(1)

    print(f"[info] Found {len(cookies)} weibo cookies: {', '.join(cookies.keys())}")

    if not validate_cookies(cookies):
        print("[error] Cookie validation failed — login may have expired in Chrome too.")
        sys.exit(1)

    # Remove cookies known to trigger 432
    removed = [k for k in BLACKLISTED_COOKIES if k in cookies]
    for k in removed:
        del cookies[k]
    if removed:
        print(f"[info] Removed blacklisted cookies: {', '.join(removed)}")

    cookie_str = cookies_to_string(cookies)
    print(f"[ok] Cookie valid (SUB + SUBP present)")

    if args.check:
        return

    update_api_keys(cookie_str)

    # Also update current process env for immediate use
    os.environ["WEIBO_COOKIE"] = cookie_str
    print("[ok] Cookie refreshed successfully")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Fetch latest posts from a Weibo user and output Markdown compatible with
build_multi_asset_report.py's parse_sections() format.

Usage:
    # Fetch latest 2 pages (default), auto Cookie via Playwright
    python scripts/ingestion/ingest_weibo.py

    # Fetch 5 pages, append to existing file
    python scripts/ingestion/ingest_weibo.py --pages 5 --append

    # Use manual cookie (from browser devtools)
    python scripts/ingestion/ingest_weibo.py --cookie "SUB=xxx; SUBP=yyy"

    # First-time login: opens browser for QR code scan, saves session
    python scripts/ingestion/ingest_weibo.py --login

Output: processed/quant_juicer_weibo_latest.md  (+ images in _assets/)
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests as http_requests

from crawl4weibo import WeiboClient, Post, RateLimitConfig

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_UID = "6937480224"  # 量化榨汁机
BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROCESSED_DIR = BASE_DIR / "processed"
DEFAULT_OUTPUT = PROCESSED_DIR / "quant_juicer_weibo_latest.md"
DEFAULT_IMAGE_DIR = PROCESSED_DIR / "quant_juicer_weibo_latest_assets"
STATE_FILE = PROCESSED_DIR / ".weibo_fetch_state.json"

HEADER_TEMPLATE = """\
量化投资榨汁机

https://weibo.com/u/{uid}

"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean_html(text: str) -> str:
    """Strip HTML tags from Weibo text, keep plain text."""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    return text.strip()



# ---------------------------------------------------------------------------
# LLM topic extraction (Gemini via Vertex AI, same as build_multi_asset_report)
# ---------------------------------------------------------------------------
SA_KEY_PATH = Path(os.getenv(
    "SA_KEY_PATH",
    str(Path.home() / ".config/secrets/liaoandi_vertex_ai_key.json"),
))
DEFAULT_MODEL = "gemini-3.1-pro-preview"
VERTEX_LOCATION = "global"
_VERTEX_CREDS = None


def _detect_project(creds_path: str) -> str:
    try:
        data = json.loads(Path(creds_path).read_text())
        return data.get("project_id", "")
    except Exception:
        return ""


def _get_vertex_creds():
    global _VERTEX_CREDS
    from google.oauth2 import service_account as sa
    from google.auth.transport.requests import Request
    if _VERTEX_CREDS is None:
        _VERTEX_CREDS = sa.Credentials.from_service_account_file(
            str(SA_KEY_PATH), scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _VERTEX_CREDS.token or not getattr(_VERTEX_CREDS, "expiry", None):
        _VERTEX_CREDS.refresh(Request())
    else:
        from datetime import datetime as _dt, timezone as _tz
        expiry = _VERTEX_CREDS.expiry
        now = _dt.now() if getattr(expiry, "tzinfo", None) is None else _dt.now(_tz.utc)
        if (expiry - now).total_seconds() < 120:
            _VERTEX_CREDS.refresh(Request())
    return _VERTEX_CREDS


def llm_extract_topics(text: str) -> str:
    """Use Gemini to extract 1-3 asset/topic keywords from a Weibo post."""
    if not SA_KEY_PATH.exists():
        return ""
    creds = _get_vertex_creds()
    project = _detect_project(str(SA_KEY_PATH))
    if not project:
        return ""
    url = (
        f"https://aiplatform.googleapis.com/v1/"
        f"projects/{project}/locations/{VERTEX_LOCATION}/publishers/google/models/{DEFAULT_MODEL}:generateContent"
    )
    prompt = (
        "你是投资分析助手。请从以下微博帖子中提取1-3个核心投资品种关键词。\n"
        "要求：\n"
        "- 只提取投资品种/资产类别，例如：黄金 美股 原油 恒生科技 中概互联 纳指 红利 消费 美债 美元 创新药 油气 通信 医疗 港股 A股\n"
        "- 多个关键词用空格分隔\n"
        "- 只输出关键词本身，不要标点、不要解释、不要编号\n"
        "- 如果帖子内容太短或无法判断具体品种，输出：综合\n\n"
        f"帖子内容：\n{text[:1500]}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 50},
    }
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    try:
        resp = http_requests.post(url, headers=headers, json=payload, timeout=(10, 30))
        if resp.status_code != 200:
            print(f"[warn] LLM topic extraction failed: HTTP {resp.status_code}")
            return ""
        data = resp.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        result = " ".join(p.get("text", "") for p in parts).strip()
        # Clean: remove punctuation, keep tokens
        result = re.sub(r"[，,、。：:\n\d.]+", " ", result)
        tokens = [t.strip() for t in result.split() if len(t.strip()) >= 2]
        # Normalize truncated names to canonical forms
        CANONICAL = {
            "恒生": "恒生科技", "恒科": "恒生科技",
            "中概": "中概互联", "中概股": "中概互联",
            "纳斯达克": "纳指",
        }
        tokens = [CANONICAL.get(t, t) for t in tokens]
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for t in tokens:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        return " ".join(unique[:3])
    except Exception as e:
        print(f"[warn] LLM topic extraction error: {e}")
        return ""


# Known asset/topic keywords, ordered by priority (most specific first).
# Maps display name -> list of search aliases.
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


def extract_topic(text: str, use_llm: bool = True) -> str:
    """Extract topic keyword(s) from post text.

    Strategy:
    1. LLM extraction (Gemini) — most accurate, supports multiple topics
    2. Known asset keywords found in text (fallback)
    3. Hashtags
    4. Short first line
    """
    # 1. Try LLM
    if use_llm:
        result = llm_extract_topics(text)
        if result:
            return result

    # 2. Keyword fallback — collect all matches
    found = []
    for display_name, aliases in TOPIC_KEYWORDS.items():
        for alias in aliases:
            if alias in text:
                if display_name not in found:
                    found.append(display_name)
                break
    if found:
        return " ".join(found[:3])

    # 3. Try hashtags
    tags = re.findall(r"#([^#\n]{1,20})#", text)
    if tags:
        noise = {"微博正文", "置顶", "超话", "头条文章", "今日看盘", "基金"}
        good = [t for t in tags if t not in noise]
        if good:
            return " ".join(good[:3])

    # 4. First line if short
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines and len(lines[0]) <= 20:
        return lines[0]

    return "综合"


def format_date(dt: datetime) -> str:
    """Format datetime to YYYY-MM-DD."""
    return dt.strftime("%Y-%m-%d")


def load_state() -> dict:
    """Load incremental fetch state (last post ID, last date)."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict):
    """Persist fetch state."""
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def pic_url_to_large(url: str) -> str:
    """Upgrade Weibo image URL to large (original) resolution."""
    # e.g. .../orj360/... -> .../large/...
    return re.sub(r"/(thumb\d+|orj\d+|mw\d+|bmiddle|small|square)/", "/large/", url)


def image_filename_from_url(url: str) -> str:
    """Extract filename from sinaimg URL."""
    from urllib.parse import urlparse
    path = urlparse(url).path
    fname = os.path.basename(path)
    if not fname or "." not in fname:
        fname = f"image_{hash(url) % 1000000}.jpg"
    return fname


def is_reply(post: Post) -> bool:
    """Check if a post is a reply (starts with 回复@)."""
    text = clean_html(post.text)
    return text.startswith("回复@")


def render_images(post: Post, image_dir: Path, image_rel_dir: str, downloaded_map: dict) -> list[str]:
    """Render image markdown lines for a post."""
    lines = []
    post_downloads = downloaded_map.get(post.id, {})
    for url in post.pic_urls:
        local_path = post_downloads.get(url)
        if local_path:
            rel = os.path.relpath(local_path, image_dir.parent)
            lines.append(f"![图表]({rel})")
        else:
            fname = image_filename_from_url(url)
            rel_path = f"{image_rel_dir}/{post.id}/{fname}"
            lines.append(f"![图表]({rel_path})")
        lines.append("")
    return lines


def group_posts(posts: list[Post]) -> list[dict]:
    """Group posts: original posts with their replies attached as Q&A.

    Returns list of dicts: {"original": Post, "replies": [Post, ...]}
    Replies (回复@) are attached to the most recent original post before them.
    """
    groups = []
    for p in posts:
        if is_reply(p):
            # Attach to the last original post
            if groups:
                groups[-1]["replies"].append(p)
            # else: orphan reply before any original — skip
        else:
            groups.append({"original": p, "replies": []})
    return groups


def group_to_markdown(
    group: dict, image_dir: Path, image_rel_dir: str, downloaded_map: dict
) -> str:
    """Convert an original post + its replies to a Markdown section."""
    post = group["original"]
    text = clean_html(post.text)
    topic = extract_topic(text)
    date_str = format_date(post.created_at) if post.created_at else "unknown"

    # Section header: "2026-03-06 黄金"
    lines = [f"{date_str} {topic}", "", "原文", ""]

    # Post body
    lines.append(text)
    lines.append("")

    # Images
    lines.extend(render_images(post, image_dir, image_rel_dir, downloaded_map))

    # Q&A section (replies) — matches Feishu doc format
    if group["replies"]:
        lines.append("评论")
        lines.append("")
        for reply in group["replies"]:
            reply_text = clean_html(reply.text)
            lines.append(reply_text)
            lines.append("")
            # Reply images (if any)
            lines.extend(render_images(reply, image_dir, image_rel_dir, downloaded_map))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Fetch Weibo posts for quant juicer")
    parser.add_argument("--uid", default=DEFAULT_UID, help="Weibo user ID")
    parser.add_argument("--pages", type=int, default=2, help="Number of pages to fetch")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output markdown file")
    parser.add_argument("--image-dir", type=Path, default=DEFAULT_IMAGE_DIR, help="Image download directory")
    parser.add_argument("--cookie", default=None, help="Manual cookie string (SUB=xxx; SUBP=yyy)")
    parser.add_argument("--login", action="store_true", help="Open browser for QR login, save session")
    parser.add_argument("--append", action="store_true", help="Append to existing file (incremental)")
    parser.add_argument("--since", default=None, help="Stop at this date (YYYY-MM-DD), skip posts on or before it")
    parser.add_argument("--no-images", action="store_true", help="Skip image download")
    args = parser.parse_args()

    # Resolve paths
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.image_dir.mkdir(parents=True, exist_ok=True)
    image_rel_dir = os.path.relpath(args.image_dir, args.output.parent)

    # Cookie source: CLI > env var > auto
    cookie = args.cookie or os.environ.get("WEIBO_COOKIE")

    # Build client
    cookie_storage = PROCESSED_DIR / ".weibo_cookie_storage"
    client_kwargs = dict(
        log_level="INFO",
        cookie_storage_path=str(cookie_storage),
        rate_limit_config=RateLimitConfig(base_delay=(8.0, 15.0)),
    )
    if cookie:
        client_kwargs["cookies"] = cookie
        print(f"[info] Using provided cookie")
    elif args.login:
        client_kwargs["login_cookies"] = True
        client_kwargs["browser_headless"] = False
        print(f"[info] Opening browser for login... scan QR code within 120s")
    else:
        # Auto fetch anonymous cookies via Playwright
        client_kwargs["use_browser_cookies"] = True
        client_kwargs["auto_fetch_cookies"] = True
        print(f"[info] Auto-fetching cookies via Playwright")

    client = WeiboClient(**client_kwargs)

    # Load state for incremental mode
    state = load_state() if args.append else {}
    last_id = state.get("last_post_id")

    # Parse --since cutoff date
    since_date = None
    if args.since:
        since_date = datetime.strptime(args.since, "%Y-%m-%d")
        print(f"[info] Will stop at posts on or before {args.since}")

    # Fetch posts
    all_posts: list[Post] = []
    for page in range(1, args.pages + 1):
        print(f"[fetch] Page {page}/{args.pages}...")
        try:
            posts = client.get_user_posts(
                uid=args.uid, page=page, expand=True, use_proxy=False
            )
        except Exception as e:
            print(f"[error] Page {page} failed: {e}")
            break

        if not posts:
            print(f"[info] No more posts at page {page}")
            break

        stop = False
        for p in posts:
            # Stop if we've reached a post already in the existing file
            if last_id and p.id == last_id:
                print(f"[info] Reached last fetched post ({last_id}), stopping")
                stop = True
                break
            # Stop if post date is before --since cutoff (posts on cutoff date are included)
            if since_date and p.created_at and p.created_at.replace(tzinfo=None) < since_date:
                print(f"[info] Reached cutoff date {args.since} (post: {format_date(p.created_at)}), stopping")
                stop = True
                break
            all_posts.append(p)

        if stop:
            break

        print(f"[fetch] Got {len(posts)} posts from page {page}")

        # Delay between pages
        if page < args.pages:
            delay = random.uniform(8, 15)
            print(f"[wait] {delay:.0f}s before next page...")
            time.sleep(delay)

    if not all_posts:
        print("[info] No new posts found.")
        return

    # Sort by date (newest first, matching source format)
    all_posts.sort(key=lambda p: p.created_at or datetime.min, reverse=True)

    total = len(all_posts)
    print(f"\n[result] {total} new posts to process")

    # Download images — collect {post_id: {url: local_path}} map
    downloaded = 0
    downloaded_map: dict[str, dict[str, str]] = {}
    if not args.no_images:
        # Upgrade pic URLs to large resolution before downloading
        for post in all_posts:
            post.pic_urls = [pic_url_to_large(u) for u in post.pic_urls]

        for i, post in enumerate(all_posts):
            if post.pic_urls:
                try:
                    result = client.download_post_images(
                        post, download_dir=str(args.image_dir)
                    )
                    downloaded_map[post.id] = result or {}
                    downloaded += len(post.pic_urls)
                except Exception as e:
                    print(f"[warn] Image download failed for post {post.id}: {e}")

            # Progress every 20%
            if total >= 5 and (i + 1) % max(1, total // 5) == 0:
                print(f"[progress] {i+1}/{total} posts processed")

    # Group original posts with their replies
    groups = group_posts(all_posts)
    print(f"[info] {len(groups)} original posts, {total - len(groups)} replies")

    # Build markdown
    md_parts = []
    if not args.append:
        md_parts.append(HEADER_TEMPLATE.format(uid=args.uid))

    for group in groups:
        section = group_to_markdown(group, args.image_dir, image_rel_dir, downloaded_map)
        md_parts.append(section)

    md_content = "\n".join(md_parts)

    # Write output
    if args.append and args.output.exists():
        existing = args.output.read_text()
        # Insert new sections after header, before existing posts
        # Find first date header in existing content
        match = re.search(r"^\d{4}-\d{2}-\d{2}\s+", existing, re.M)
        if match:
            md_content = existing[: match.start()] + md_content + "\n" + existing[match.start() :]
        else:
            md_content = existing + "\n" + md_content
        args.output.write_text(md_content)
        print(f"[output] Appended to {args.output}")
    else:
        args.output.write_text(md_content)
        print(f"[output] Written to {args.output}")

    # Update state
    if all_posts:
        save_state({
            "last_post_id": all_posts[0].id,
            "last_date": format_date(all_posts[0].created_at) if all_posts[0].created_at else None,
            "fetched_at": datetime.now().isoformat(),
            "total_fetched": total,
        })

    # Summary
    print(f"\n--- Summary ---")
    print(f"Posts fetched: {total}")
    print(f"Images downloaded: {downloaded}")
    print(f"Output: {args.output}")
    print(f"Images: {args.image_dir}")


if __name__ == "__main__":
    main()

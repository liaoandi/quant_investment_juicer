#!/usr/bin/env python3
import argparse
import base64
import datetime as dt
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import shutil
import time
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import requests
from matplotlib import font_manager
from google.auth.transport.requests import Request
from google.oauth2 import service_account

def _path_from_env(var_name: str, default: Path) -> Path:
    raw = os.getenv(var_name, "").strip()
    if not raw:
        return default
    return Path(raw).expanduser()


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SA_KEY_PATH = _path_from_env("SA_KEY_PATH", Path.home() / "Desktop/liaoandi-vertex-ai-key.json")
ENV_PATH = _path_from_env("ENV_PATH", PROJECT_ROOT / ".env")
DEFAULT_MODEL = "gemini-3.1-pro-preview"
VERTEX_LOCATION = "global"
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "120"))
LLM_BATCH_SIZE = max(1, int(os.getenv("LLM_BATCH_SIZE", "3")))
LLM_THINKING_BUDGET = int(os.getenv("LLM_THINKING_BUDGET", "256"))
LLM_FINAL_PROOFREAD = os.getenv("LLM_FINAL_PROOFREAD", "0").strip() == "1"
LLM_INSTRUMENT_VALIDATION = os.getenv("LLM_INSTRUMENT_VALIDATION", "1").strip() == "1"
LLM_SECTION_REFINEMENT = os.getenv("LLM_SECTION_REFINEMENT", "0").strip() == "1"
EMBED_SOURCE_IMAGES = os.getenv("EMBED_SOURCE_IMAGES", "0").strip() == "1"
TOP_CHART_WIDTH = max(480, int(os.getenv("TOP_CHART_WIDTH", "980")))
SOURCE_IMG_WIDTH = max(320, int(os.getenv("SOURCE_IMG_WIDTH", "700")))
SOURCE_IMG_MIN_WIDTH = max(240, int(os.getenv("SOURCE_IMG_MIN_WIDTH", "340")))
SOURCE_IMG_MAX_WIDTH = max(SOURCE_IMG_MIN_WIDTH, int(os.getenv("SOURCE_IMG_MAX_WIDTH", "620")))
SOURCE_IMG_ROW_MAX_WIDTH = max(SOURCE_IMG_MIN_WIDTH, int(os.getenv("SOURCE_IMG_ROW_MAX_WIDTH", "320")))
MIN_SERIES_POINTS = max(1, int(os.getenv("MIN_SERIES_POINTS", "30")))
PRICE_CACHE_DIR = _path_from_env("PRICE_CACHE_DIR", PROJECT_ROOT / "output/price_cache")
_VERTEX_CFG: dict | None = None
_VERTEX_CREDS = None

TOKEN_ALIASES = {
    "黄金": ["黄金", "金价", "GLD", "COMEX", "伦敦金", "期金"],
    "消费": ["消费", "中证消费", "消费50", "消费龙头", "白酒", "000932"],
    "红利": ["红利", "红利低波", "高股息", "000922", "512890", "159928"],
    "美元": ["美元", "美元指数", "DXY", "汇率"],
    "美债": ["美债", "TLT", "VGIT", "长债", "短债", "国债"],
    "通信": ["通信", "全指通信", "515880", "007818"],
    "医疗": ["医疗", "医药", "XLV", "中证医疗"],
    "油气": ["油气", "原油", "USO", "XOP", "WTI", "布油", "能源"],
}

# Allow limited cross-token mentions that are still relevant for the target token.
TOKEN_CROSS_ALLOWED_ALIASES = {
    "黄金": {"美元", "美元指数", "DXY", "汇率"},
    "美债": {"美元", "美元指数", "DXY", "汇率"},
}

CORE_KEYWORDS = [
    "支撑",
    "阻力",
    "压力",
    "止损",
    "止盈",
    "加仓",
    "减仓",
    "突破",
    "跌破",
    "站稳",
    "回调",
    "均线",
    "布林",
    "AVWAP",
    "期权墙",
    "仓位",
]

NOISE_HINTS = ["怎么选", "网页链接", "| --- |", "--- | ---"]

DROP_LINE_HINTS = [
    "微博正文",
    "题外话",
    "小调查",
    "省流版",
    "fun fact",
    "热搜",
]

FILLER_PATTERNS = [
    r"^没什么好.*",
    r"^开个玩笑.*",
    r"^多两句嘴.*",
    r"^另外提一句.*",
    r"^顺带提醒一句.*",
    r"^这等局面.*",
    r"^这玩意儿.*",
]

ACTION_PAT = re.compile(r"建议|可|不宜|不追|止损|加仓|减仓|持有|观察|入场|站稳|跌破|关注|盯紧|暂停")
NUM_PAT = re.compile(r"\$?\d{2,5}(?:\.\d+)?(?:\s*[-~]\s*\$?\d{2,5}(?:\.\d+)?)?")
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "]+",
    flags=re.UNICODE,
)
IMAGE_MD_PAT = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
LABELLED_SEGMENT_PAT = re.compile(r"(核心观点|关键点位|操作要点|对应操作)\s*[:：]")
PUNCT_ONLY_PAT = re.compile(r"^[\s,，。；;:：、\-—（）()]+$")
LEVEL_HINT_PAT = re.compile(r"支撑|阻力|止损|失效|观察|分水岭|Flip|Gamma|Call|Put|均线|布林|AVWAP|上沿|下沿")
ACTION_HINT_PAT = re.compile(r"建议|可|不宜|不追|止损|加仓|减仓|持有|观察|入场|站稳|跌破|关注|盯紧|暂停|等待")


INSTRUMENTS = [
    {
        "id": "gold_gld",
        "name": "黄金（GLD）",
        "token": "黄金",
        "symbol": "GLD",
        "price_basis": "ETF",
        "level_basis": "ETF",
        "levels": [
            {"label": "支撑下沿", "value": 440.0, "kind": "support", "source": "2026-02-11/13"},
            {"label": "支撑上沿", "value": 460.0, "kind": "support", "source": "2026-02-13"},
            {"label": "失效位", "value": 435.0, "kind": "stop", "source": "2026-02-11"},
            {"label": "阻力1", "value": 465.0, "kind": "resistance", "source": "2026-02-11/12"},
            {"label": "阻力2", "value": 475.0, "kind": "resistance", "source": "2026-02-11/13"},
        ],
    },
    {
        "id": "consumption",
        "name": "消费（000932）",
        "token": "消费",
        "symbol": "000932.SS",
        "eastmoney_secid": "1.000932",
        "price_basis": "INDEX",
        "level_basis": "INDEX",
        "levels": [
            {"label": "止损位", "value": 14700.0, "kind": "stop", "source": "2026-02-05/10"},
            {"label": "观察位", "value": 15115.0, "kind": "watch", "source": "2026-02-10"},
            {"label": "阻力1", "value": 15500.0, "kind": "resistance", "source": "2026-02-05/10"},
            {"label": "阻力2", "value": 15730.0, "kind": "resistance", "source": "2026-02-05"},
            {"label": "阻力3", "value": 16600.0, "kind": "resistance", "source": "2026-02-05"},
        ],
    },
    {
        "id": "dividend",
        "name": "红利（000922）",
        "token": "红利",
        "symbol": "000922.SS",
        "eastmoney_secid": "1.000922",
        "price_basis": "INDEX",
        "level_basis": "INDEX",
        "levels": [
            {"label": "强支撑下沿", "value": 4650.0, "kind": "support", "source": "2025-01-22"},
            {"label": "强支撑上沿", "value": 4750.0, "kind": "support", "source": "2025-01-22"},
            {"label": "舒适回补下沿", "value": 5100.0, "kind": "watch", "source": "2025-10-14(评论)"},
            {"label": "舒适回补上沿", "value": 5300.0, "kind": "watch", "source": "2025-10-14(评论)"},
        ],
    },
    {
        "id": "dxy",
        "name": "美元指数（DXY）",
        "token": "美元",
        "symbol": "DX-Y.NYB",
        "price_basis": "INDEX",
        "level_basis": "INDEX",
        "levels": [
            {"label": "关键支撑", "value": 96.6, "kind": "support", "source": "2025-09-03/2026-02-13"},
        ],
    },
    {
        "id": "tlt",
        "name": "美债长债（TLT）",
        "token": "美债",
        "symbol": "TLT",
        "price_basis": "ETF",
        "level_basis": "ETF",
        "levels": [
            {"label": "关键阻力", "value": 91.5, "kind": "resistance", "source": "2025-02-27/03-05"},
        ],
    },
    {
        "id": "vgit",
        "name": "美债中债（VGIT）",
        "token": "美债",
        "symbol": "VGIT",
        "price_basis": "ETF",
        "level_basis": "ETF",
        "levels": [
            {"label": "参考阻力", "value": 59.6, "kind": "resistance", "source": "2025-02-27"},
        ],
    },
    {
        "id": "comm",
        "name": "通信（515880）",
        "token": "通信",
        "symbol": "515880.SS",
        "price_basis": "ETF",
        "level_basis": "ETF",
        "levels": [
            {"label": "买入观察位", "value": 1.05, "kind": "support", "source": "2025-05-09"},
            {"label": "阻力位", "value": 1.25, "kind": "resistance", "source": "2025-05-09"},
        ],
    },
    {
        "id": "xlv",
        "name": "全球医疗（XLV）",
        "token": "医疗",
        "symbol": "XLV",
        "price_basis": "ETF",
        "level_basis": "ETF",
        "levels": [
            {"label": "低吸下沿", "value": 130.0, "kind": "support", "source": "2025-08-08"},
            {"label": "低吸上沿", "value": 132.0, "kind": "support", "source": "2025-08-08"},
        ],
    },
    {
        "id": "oil_gas_uso",
        "name": "油气（USO）",
        "token": "油气",
        "symbol": "USO",
        "price_basis": "ETF",
        "level_basis": "ETF",
        "levels": [],
    },
]

SUMMARY_CATEGORY_ORDER = ["美元与美债", "美股", "港股", "A股", "大宗商品", "其他"]

# Instruments that share a combined detail section heading.
# Key = group heading, value = set of instrument ids in the group.
INSTRUMENT_GROUPS = {
    "美元与美债": {"dxy", "tlt", "vgit"},
}

# Configured instruments demoted to watchlist (empty = all get detail).
WATCHLIST_INSTRUMENTS: set[str] = set()

DISPLAY_NAME_ALIASES = {
    "Mag7（美股七巨头）": "美股七巨头（Mag7）",
    "YINN（三倍做多富时中国ETF）": "三倍做多富时中国ETF（YINN）",
    "消费（中证消费 000932）": "消费（000932）",
    "红利（中证红利 000922）": "红利（000922）",
}


def contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def normalize_instrument_name(name: str, symbol: str = "") -> str:
    raw = compact_spaces(str(name))
    if not raw:
        return ""
    s = raw.replace("(", "（").replace(")", "）")
    s = re.sub(r"\s*（\s*", "（", s)
    s = re.sub(r"\s*）\s*", "）", s)
    s = DISPLAY_NAME_ALIASES.get(s, s)
    m = re.match(r"^([A-Za-z0-9._:+-]+)\s*（\s*([^）]+)\s*）$", s)
    if m and contains_cjk(m.group(2)):
        s = f"{compact_spaces(m.group(2))}（{compact_spaces(m.group(1))}）"
    symbol_text = compact_spaces(str(symbol))
    if symbol_text:
        symbol_text = symbol_text.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
        if "." in symbol_text and re.fullmatch(r"\d{5,6}\.[A-Za-z]{2}", symbol_text):
            symbol_text = symbol_text.split(".", 1)[0]
    if symbol_text and "（" not in s and contains_cjk(s):
        s = f"{s}（{symbol_text}）"
    return s


def normalize_basis_label(label: str) -> str:
    raw = (label or "").strip().upper()
    if raw in {"INDEX", "IDX", "指数"}:
        return "INDEX"
    if raw in {"ETF", "基金"}:
        return "ETF"
    if raw in {"FUTURES", "FUT", "期货"}:
        return "FUTURES"
    return raw or "UNKNOWN"


def validate_instrument_basis(inst: dict) -> tuple[str, str]:
    pb = normalize_basis_label(inst.get("price_basis", "UNKNOWN"))
    lb = normalize_basis_label(inst.get("level_basis", pb))
    if pb != lb:
        raise RuntimeError(
            f"口径不一致: {inst.get('name')} price_basis={pb} level_basis={lb}. "
            "请统一为同一口径（指数对指数、ETF对ETF）。"
        )
    return pb, lb


def classify_summary_category(
    name: str,
    basis: str,
    source: str = "",
    token: str = "",
    symbol: str = "",
) -> str:
    cn = compact_spaces(name)
    upper_text = f"{cn} {token} {symbol} {source} {basis}".upper()

    if any(k in cn for k in ["美元", "汇率"]) or any(k in upper_text for k in ["DXY", "FOREX", "FX"]):
        return "美元与美债"

    if any(k in cn for k in ["美债", "债券", "债"]) or any(k in upper_text for k in ["TLT", "VGIT", "BOND"]):
        return "美元与美债"

    if any(k in cn for k in ["日元"]) or "JPY" in upper_text:
        return "其他"

    # Non-US bonds (e.g. Chinese government bonds) stay separate
    if any(k in cn for k in ["国债"]) and not any(k in cn for k in ["美"]):
        return "A股"

    if any(k in cn for k in ["恒生", "港股"]) or "HSTECH" in upper_text:
        return "港股"

    if any(k in cn for k in ["A股", "中证", "红利", "消费", "通信", "军工", "石化", "工程机械"]):
        return "A股"

    if any(k in cn for k in ["美股", "标普", "纳斯达克", "全球医疗"]) or any(
        k in upper_text for k in ["SPX", "NDX", "MAG7", "XLV", "YINN"]
    ):
        return "美股"

    if any(k in cn for k in ["黄金", "油气", "原油", "有色金属"]) or any(
        k in upper_text for k in ["GLD", "USO", "XOP", "WTI", "COMEX"]
    ):
        return "大宗商品"

    return "其他"


def _extract_iso_date(text: str) -> dt.date | None:
    raw = compact_spaces(str(text))
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", raw)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def summary_row_sort_key(row: dict) -> tuple[int, dt.date, str]:
    d = _extract_iso_date(str(row.get("date", "")))
    # valid date first, then newer date first, then stable by name
    return (1 if d else 0, d or dt.date.min, compact_spaces(str(row.get("name", ""))))


def analyze_source_coverage(md_text: str, sections: list[dict]) -> list[dict]:
    # Extract explicit symbol mentions and short Chinese topic words from source,
    # then compare with configured instruments.
    raw_symbols = re.findall(r"\b[A-Z][A-Z0-9-]{1,6}(?:\.[A-Z]{2,3})?\b", md_text)
    ignore = {
        "ETF",
        "INDEX",
        "COMEX",
        "CALL",
        "PUT",
        "GAMMA",
        "AVWAP",
        "WTI",
        "USD",
        "CNY",
        "A",
        "B",
        "C",
        "Q",
    }
    symbol_hits: dict[str, int] = {}
    for sym in raw_symbols:
        root = sym.split(".", 1)[0]
        if root in ignore or root.isdigit():
            continue
        symbol_hits[root] = symbol_hits.get(root, 0) + 1

    topic_stop = {
        "评论",
        "原文",
        "微博",
        "正文",
        "更新",
        "回复",
        "观点",
        "策略",
        "操作",
        "问答",
        "问题",
        "建议",
    }
    topic_hits: dict[str, int] = {}
    for sec in sections:
        for raw in sec.get("topics", []):
            parts = re.split(r"[、,，/|]+", str(raw))
            for p in parts:
                t = compact_spaces(p).strip("()（）[]【】<>《》")
                if not t or t in topic_stop:
                    continue
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
                    continue
                if len(t) < 2 or len(t) > 6:
                    continue
                if not re.search(r"[\u4e00-\u9fffA-Za-z]", t):
                    continue
                topic_hits[t] = topic_hits.get(t, 0) + 1

    configured_symbols = {str(i.get("symbol", "")).split(".", 1)[0].strip().upper() for i in INSTRUMENTS}
    alias_pool: set[str] = set()
    for inst in INSTRUMENTS:
        token = str(inst.get("token", "")).strip()
        if token:
            alias_pool.add(token)
            for a in TOKEN_ALIASES.get(token, [token]):
                alias_pool.add(str(a).strip())

    def topic_covered(topic: str) -> bool:
        for a in alias_pool:
            if not a:
                continue
            if topic == a:
                return True
            if len(topic) >= 2 and len(a) >= 2 and (topic in a or a in topic):
                return True
        return False

    out: list[dict] = []
    for sym, hit in symbol_hits.items():
        if hit < 2:
            continue
        out.append({"name": sym, "kind": "符号", "hit": hit, "covered": sym in configured_symbols})
    for topic, hit in topic_hits.items():
        if hit < 2:
            continue
        out.append({"name": topic, "kind": "主题", "hit": hit, "covered": topic_covered(topic)})
    out.sort(key=lambda x: (-x["hit"], x["kind"], x["name"]))
    return out


def _fetch_history_yahoo(symbol: str, range_period: str = "1y") -> pd.DataFrame:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range={range_period}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"No result for {symbol}")
    r0 = result[0]
    ts = r0.get("timestamp", [])
    q = r0.get("indicators", {}).get("quote", [{}])[0]
    close = q.get("close", [])
    adj = r0.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose", [])
    if not ts or not close:
        raise RuntimeError(f"No price series for {symbol}")

    rows = []
    for i, (t, c) in enumerate(zip(ts, close)):
        a = adj[i] if i < len(adj) else None
        # Prefer adjclose when available to remove split/distribution jumps.
        px = a if a is not None else c
        if px is None:
            continue
        rows.append((dt.datetime.fromtimestamp(t, dt.UTC), float(px)))
    if not rows:
        raise RuntimeError(f"All closes are null for {symbol}")
    df = pd.DataFrame(rows, columns=["date", "close"])
    return df


def _fetch_history_eastmoney(secid: str, beg: str = "20240101", end: str = "20990101") -> pd.DataFrame:
    params = {
        "secid": secid,
        "klt": "101",
        "fqt": "0",
        "beg": beg,
        "end": end,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
    }
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore"))
    klines = (data.get("data") or {}).get("klines") or []
    rows: list[tuple[dt.datetime, float]] = []
    for row in klines:
        parts = row.split(",")
        if len(parts) < 3:
            continue
        try:
            d = dt.datetime.strptime(parts[0], "%Y-%m-%d").replace(tzinfo=dt.UTC)
            close = float(parts[2])
        except Exception:
            continue
        rows.append((d, close))
    if not rows:
        raise RuntimeError(f"No price series for eastmoney secid={secid}")
    return pd.DataFrame(rows, columns=["date", "close"])


def _cache_path(inst_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", inst_id)
    return PRICE_CACHE_DIR / f"{safe}.csv"


def _save_cache(inst_id: str, df: pd.DataFrame) -> None:
    PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], utc=True).dt.strftime("%Y-%m-%d")
    out.to_csv(_cache_path(inst_id), index=False)


def _load_cache(inst_id: str) -> pd.DataFrame | None:
    p = _cache_path(inst_id)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p)
        if "date" not in df.columns or "close" not in df.columns:
            return None
        df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["date", "close"]).reset_index(drop=True)
        return df if not df.empty else None
    except Exception:
        return None


def fetch_history(inst: dict, range_period: str = "1y") -> tuple[pd.DataFrame, str]:
    symbol = inst["symbol"]
    short_df: pd.DataFrame | None = None
    errors = []

    try:
        df = _fetch_history_yahoo(symbol, range_period)
        if len(df) >= MIN_SERIES_POINTS:
            _save_cache(inst["id"], df)
            return df, f"yahoo:{symbol}"
        short_df = df
        errors.append(f"yahoo:{symbol} only {len(df)} points")
    except Exception as e:
        errors.append(f"yahoo:{symbol} -> {e}")

    secid = inst.get("eastmoney_secid")
    if secid:
        try:
            df = _fetch_history_eastmoney(secid)
            if len(df) >= MIN_SERIES_POINTS:
                _save_cache(inst["id"], df)
                return df, f"eastmoney:{secid}"
            if short_df is None:
                short_df = df
            errors.append(f"eastmoney:{secid} only {len(df)} points")
        except Exception as e:
            errors.append(f"eastmoney:{secid} -> {e}")

    if short_df is not None and not short_df.empty:
        _save_cache(inst["id"], short_df)
        return short_df, f"short:{symbol}({len(short_df)}p)"

    cached = _load_cache(inst["id"])
    if cached is not None:
        return cached, f"cache:{symbol}"

    raise RuntimeError(" | ".join(errors))


def level_color(kind: str) -> str:
    # Morandi palette — muted, dusty tones
    if kind == "support":
        return "#7a9a7e"  # sage green
    if kind == "resistance":
        return "#b5737a"  # dusty rose
    if kind == "stop":
        return "#c2956b"  # warm sand
    if kind == "watch":
        return "#8e7ca6"  # lavender grey
    return "#8c9baa"  # slate blue-grey


def _stable_recent_level(prices: list[float], start: int, lookahead: int = 5) -> bool:
    tail = [x for x in prices[start : min(len(prices), start + lookahead)] if x > 0]
    if len(tail) < 3:
        return True
    s = sorted(tail)
    med = s[len(s) // 2]
    if med <= 0:
        return False
    # median within +/-20% of first post-gap value => level shift likely persisted
    return abs(med - tail[0]) / tail[0] <= 0.2


def back_adjust_split_like_gaps(df: pd.DataFrame) -> tuple[pd.Series, list[dict]]:
    prices = [float(x) for x in df["close"].tolist()]
    # Common split/consolidation ratios.
    candidates = [0.1, 0.125, 0.2, 0.25, 1 / 3, 0.5, 2.0, 3.0, 4.0, 5.0, 8.0, 10.0]
    events: list[dict] = []

    for i in range(1, len(prices)):
        prev = prices[i - 1]
        curr = prices[i]
        if prev <= 0 or curr <= 0:
            continue
        ratio = curr / prev
        if 0.6 <= ratio <= 1.7:
            continue
        nearest = min(candidates, key=lambda c: abs(c - ratio))
        rel_err = abs(ratio - nearest) / max(abs(nearest), 1e-9)
        if rel_err > 0.12:
            continue
        if not _stable_recent_level(prices, i):
            continue
        for j in range(i):
            prices[j] *= nearest
        events.append(
            {
                "date": df["date"].iloc[i].strftime("%Y-%m-%d"),
                "raw_ratio": ratio,
                "applied_factor": nearest,
            }
        )
    return pd.Series(prices), events


def plot_chart(df: pd.DataFrame, inst: dict, out_png: Path) -> float:
    current = float(df["close"].iloc[-1])
    current_date = df["date"].iloc[-1].strftime("%Y-%m-%d")
    plot_close, split_events = back_adjust_split_like_gaps(df)

    fig, ax = plt.subplots(figsize=(12, 5.8), dpi=140)
    ax.plot(df["date"], plot_close, color="#7e9bb5", linewidth=1.8, label=f"{inst['symbol']} close")
    ax.scatter(df["date"].iloc[-1], current, color="#5b7d99", s=24, zorder=5)

    # Key levels as dashed lines with legend entries (no on-chart text labels)
    for lv in inst["levels"]:
        y = lv["value"]
        ax.axhline(
            y, color=level_color(lv["kind"]), linestyle="--", linewidth=1.1, alpha=0.9,
            label=f"{lv['label']} {y:g}",
        )

    ax.set_title(f"{inst['name']} | current={current:.4f} ({current_date})")
    ax.set_xlabel("Date")
    ax.set_ylabel("Price")
    ax.grid(alpha=0.15)
    ax.legend(loc="best", fontsize=7.5, framealpha=0.85, edgecolor="#cccccc")

    if inst["levels"]:
        ymin = min(plot_close.min(), min(lv["value"] for lv in inst["levels"])) * 0.96
        ymax = max(plot_close.max(), max(lv["value"] for lv in inst["levels"])) * 1.04
    else:
        ymin = float(plot_close.min()) * 0.96
        ymax = float(plot_close.max()) * 1.04
    ax.set_ylim(ymin, ymax)

    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)
    if split_events:
        print(
            f"[CHART-ADJUST] {inst['id']} split-like events: "
            + ", ".join([f"{e['date']} factor={e['applied_factor']:.6g}" for e in split_events]),
            flush=True,
        )
    return current


def image_to_data_uri(image_path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(image_path))
    if not mime:
        mime = "image/png"
    payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def render_image(src: str, alt: str, width: int | None = None, style: str | None = None) -> str:
    attrs = [
        f'alt="{html.escape(alt, quote=True)}"',
        f'src="{src}"',
    ]
    if width:
        attrs.append(f'width="{width}"')
    if style:
        attrs.append(f'style="{style}"')
    return f"<img {' '.join(attrs)} />"


def detect_image_size(path: Path | None = None, data_uri: str | None = None) -> tuple[int, int] | None:
    img_bytes = None
    if path and path.exists():
        try:
            img_bytes = path.read_bytes()
        except Exception:
            img_bytes = None
    if img_bytes is None and data_uri and data_uri.startswith("data:image/") and ";base64," in data_uri:
        try:
            b64 = data_uri.split(";base64,", 1)[1]
            img_bytes = base64.b64decode(b64)
        except Exception:
            img_bytes = None
    if img_bytes is None:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(img_bytes)) as im:
            return int(im.width), int(im.height)
    except Exception:
        # PNG IHDR fallback when PIL is unavailable.
        if len(img_bytes) >= 24 and img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            w = int.from_bytes(img_bytes[16:20], "big")
            h = int.from_bytes(img_bytes[20:24], "big")
            if w > 0 and h > 0:
                return w, h
    return None


def choose_source_image_width(size: tuple[int, int] | None, in_row: bool) -> int:
    if not size:
        target = SOURCE_IMG_ROW_MAX_WIDTH if in_row else min(SOURCE_IMG_MAX_WIDTH, SOURCE_IMG_WIDTH)
    else:
        w, h = size
        ratio = w / max(h, 1)
        if in_row:
            if ratio >= 1.8:
                target = 520
            elif ratio >= 1.2:
                target = 480
            elif ratio >= 0.9:
                target = 430
            else:
                target = 380
            target = min(target, SOURCE_IMG_ROW_MAX_WIDTH)
        else:
            if ratio >= 1.8:
                target = 620
            elif ratio >= 1.2:
                target = 560
            elif ratio >= 0.9:
                target = 500
            else:
                target = 420
    return max(SOURCE_IMG_MIN_WIDTH, min(SOURCE_IMG_MAX_WIDTH, int(target)))


def resolve_local_image_path(src: str, input_md: Path) -> Path | None:
    if not src or src.startswith("data:") or "://" in src:
        return None
    p = (input_md.parent / src).resolve()
    return p if p.exists() else None


def render_image_row(items: list[tuple[str, str, int]]) -> list[str]:
    lines = [
        '<div style="display:flex; flex-wrap:nowrap; align-items:flex-start; gap:8px; overflow-x:auto; padding:2px 0 6px;">'
    ]
    for src, alt, each_width in items:
        capped = max(SOURCE_IMG_MIN_WIDTH, min(SOURCE_IMG_ROW_MAX_WIDTH, int(each_width)))
        lines.append(
            "  "
            + f'<a href="{src}" target="_blank" rel="noopener noreferrer">'
            + render_image(
                src,
                alt,
                style=(
                    f"width:{capped}px; height:auto; max-width:{capped}px; "
                    "object-fit:contain; flex:0 0 auto;"
                ),
            )
            + "</a>"
        )
    lines.append("</div>")
    return lines


def configure_matplotlib_fonts() -> None:
    """Pick a Chinese-capable font when available to avoid tofu boxes."""
    preferred = [
        "PingFang SC",
        "Hiragino Sans GB",
        "Songti SC",
        "Heiti TC",
        "Arial Unicode MS",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
    ]
    available = {f.name for f in font_manager.fontManager.ttflist}
    picked = next((name for name in preferred if name in available), None)
    if picked:
        plt.rcParams["font.sans-serif"] = [picked, "DejaVu Sans"]
    else:
        # Fallback to default if no CJK font is found.
        plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def parse_sections(md_text: str) -> list[dict]:
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(.+)$", re.M)
    heads = list(pat.finditer(md_text))
    out = []
    for i, m in enumerate(heads):
        start = m.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md_text)
        block = md_text[start:end]
        out.append(
            {
                "date": m.group(1),
                "topics": [t for t in m.group(2).split() if t],
                "text": block,
            }
        )
    return out


def break_into_paragraphs(text: str) -> str:
    """Clean up text for markdown rendering while preserving original formatting."""
    # Strip hashtag wrappers like #金价# → 金价 to prevent markdown h1 rendering
    text = re.sub(r"#([^#\n]+)#", r"\1", text)
    # Remove emoji but keep the surrounding text intact
    text = EMOJI_RE.sub("", text)
    # Clean up excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_original_part(raw: str) -> str:
    text = raw.strip()
    if "原文" in text:
        text = text.split("原文", 1)[1].strip()
        if "\n评论" in text:
            text = text.split("\n评论", 1)[0].strip()
    elif "评论" in text:
        text = text.split("评论", 1)[1].strip()
    return text


def normalize_text(text: str) -> str:
    # Remove emoji and noisy visual symbols.
    t = EMOJI_RE.sub("", text)
    t = t.replace("➡️", " ").replace("💡", " ").replace("📈", " ").replace("📉", " ")
    t = t.replace("📝", " ").replace("🏠", " ").replace("😱", " ").replace("💪🏻", " ")
    t = IMAGE_MD_PAT.sub(" ", t)
    # Convert hashtag wrappers like #金价# -> 金价.
    t = re.sub(r"#([^#]+)#", r"\1", t)
    # Remove excessive punctuation and whitespace.
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def sentence_chunks(text: str) -> list[str]:
    base = normalize_text(text)
    parts = re.split(r"[。！？\n]+", base)
    out = []
    for p in parts:
        s = p.strip(" -:：;；,.，、")
        if s:
            out.append(s)
    return out


def is_filler_sentence(s: str) -> bool:
    if len(s) < 8:
        return True
    if any(h in s for h in DROP_LINE_HINTS):
        return True
    return any(re.match(pat, s) for pat in FILLER_PATTERNS)


def is_core_sentence(token: str, s: str) -> bool:
    if is_filler_sentence(s):
        return False
    aliases = TOKEN_ALIASES.get(token, [token])
    has_alias = any(a in s for a in aliases)
    has_keyword = any(k in s for k in CORE_KEYWORDS) or any(
        k in s for k in ["期权", "Gamma", "Put Wall", "Call Wall", "Flip Point", "美元", "仓位", "AVWAP"]
    )
    has_num = bool(NUM_PAT.search(s))
    has_action = bool(ACTION_PAT.search(s))
    if has_keyword and (has_alias or has_num or has_action):
        return True
    if has_action and has_num:
        return True
    if has_alias and has_num:
        return True
    return False


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for it in items:
        key = it.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def compact_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def normalize_for_similarity(text: str) -> str:
    t = compact_spaces(text)
    t = re.sub(r"[`*_>#\[\]\(\)\|]", "", t)
    t = re.sub(r"[，。；：、,.;:\-—（）()\s]+", "", t)
    return t


def normalize_point_signature(text: str) -> str:
    t = normalize_for_similarity(text)
    # Keep only key semantic body for de-duplication.
    t = re.sub(r"^(核心观点|关键点位|操作要点|对应操作)", "", t)
    return t[:240]


def split_labelled_segments(text: str) -> dict[str, list[str]]:
    out = {"core": [], "levels": [], "actions": []}
    raw = compact_spaces(text)
    if not raw:
        return out
    matches = list(LABELLED_SEGMENT_PAT.finditer(raw))
    if not matches:
        out["core"].append(raw)
        return out
    for i, m in enumerate(matches):
        label = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end].strip(" ；;。")
        if not body or PUNCT_ONLY_PAT.match(body):
            continue
        points = [p.strip() for p in re.split(r"[；;](?![^()（）]*[)）])", body) if p.strip()]
        if not points:
            points = [body]
        if label == "关键点位":
            out["levels"].extend(points)
        elif label in {"操作要点", "对应操作"}:
            out["actions"].extend(points)
        else:
            out["core"].extend(points)
    return out


def clean_level_or_action_text(text: str) -> str:
    s = compact_spaces(text).strip(" -")
    if not s:
        return ""
    s = re.sub(r"^(核心观点|关键点位|操作要点|对应操作)\s*[:：]?\s*", "", s).strip()
    if s.startswith("{") and ("price" in s or "desc" in s):
        price_match = re.search(r"(?:'price'|\"price\")\s*:\s*['\"]([^'\"]+)['\"]", s)
        desc_match = re.search(r"(?:'desc'|\"desc\")\s*:\s*['\"]([^'\"]+)['\"]", s)
        if price_match and desc_match:
            return f"{price_match.group(1)}（{desc_match.group(1)}）"
        if price_match:
            return price_match.group(1)
    return s


def is_meaningful_level_text(text: str) -> bool:
    s = compact_spaces(text)
    if not s or len(s) < 3 or PUNCT_ONLY_PAT.match(s):
        return False
    if LEVEL_HINT_PAT.search(s):
        return True
    if re.search(r"\d+(?:\.\d+)?\s*[-~]\s*\d+(?:\.\d+)?", s):
        return True
    has_num = bool(NUM_PAT.search(s))
    if not has_num:
        return False
    raw_num = s.replace("$", "").replace("￥", "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", raw_num):
        try:
            v = float(raw_num)
        except Exception:
            return False
        if "." in raw_num and v >= 10:
            return True
        return 100 <= v <= 100000
    return len(s) >= 6


def is_meaningful_action_text(text: str) -> bool:
    s = compact_spaces(text)
    if not s or len(s) < 6 or PUNCT_ONLY_PAT.match(s):
        return False
    if ACTION_HINT_PAT.search(s):
        return True
    return bool(NUM_PAT.search(s) and ("不" in s or "可" in s or "宜" in s))


def numeric_values_for_level(text: str) -> list[float]:
    vals: list[float] = []
    for m in re.findall(r"\d+(?:\.\d+)?", text):
        try:
            v = float(m)
        except Exception:
            continue
        if 1900 <= v <= 2100:
            # likely a year/date token, not a level
            continue
        vals.append(v)
    return vals


def filter_levels_by_price_context(levels: list[str], current: float) -> list[str]:
    out: list[str] = []
    for lv in levels:
        s = compact_spaces(lv)
        if not s:
            continue
        nums = numeric_values_for_level(s)
        if not nums:
            out.append(s)
            continue
        if LEVEL_HINT_PAT.search(s):
            out.append(s)
            continue
        # Drop obvious code/id-like pure numbers.
        if re.fullmatch(r"\d+(?:\.\d+)?", s.replace("$", "").replace("￥", "").strip()):
            raw = s.replace("$", "").replace("￥", "").strip()
            try:
                only_num = float(raw)
            except Exception:
                only_num = 0.0
            if "." not in raw and only_num < 1000:
                continue
            if only_num >= 20000:
                continue
        if current > 0:
            # Keep numeric levels roughly around current price context.
            ratios = [v / current for v in nums if v > 0]
            if ratios and not any(0.35 <= r <= 2.0 for r in ratios):
                continue
        out.append(s)
    return dedupe_keep_order(out)


def post_process_refined_item(refined: dict) -> dict:
    core_raw = [compact_spaces(x) for x in refined.get("core_points", []) if compact_spaces(x)]
    levels_raw = [clean_level_or_action_text(x) for x in refined.get("levels", []) if clean_level_or_action_text(x)]
    actions_raw = [clean_level_or_action_text(x) for x in refined.get("actions", []) if clean_level_or_action_text(x)]

    core: list[str] = []
    levels: list[str] = list(levels_raw)
    actions: list[str] = list(actions_raw)

    for line in core_raw:
        parts = split_labelled_segments(line)
        core.extend(parts["core"])
        levels.extend(parts["levels"])
        actions.extend(parts["actions"])

    core = [
        clean_level_or_action_text(x)
        for x in dedupe_keep_order(core)
        if clean_level_or_action_text(x) and not LABELLED_SEGMENT_PAT.search(x)
    ]
    levels = [
        x
        for x in dedupe_keep_order([clean_level_or_action_text(x) for x in levels if clean_level_or_action_text(x)])
        if is_meaningful_level_text(x)
    ]
    actions = [
        x
        for x in dedupe_keep_order([clean_level_or_action_text(x) for x in actions if clean_level_or_action_text(x)])
        if is_meaningful_action_text(x)
    ]
    return {
        "title": refined.get("title", "").strip() or "核心观点",
        "core_points": core[:6],
        "levels": levels[:6],
        "actions": actions[:4],
    }


def rows_are_near_duplicate(a_text: str, b_text: str) -> bool:
    a = normalize_for_similarity(a_text)
    b = normalize_for_similarity(b_text)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 40 and short in long:
        return True
    ratio = SequenceMatcher(a=a, b=b).ratio()
    return ratio >= 0.9


def dedupe_image_items(images: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for img in images:
        src = str(img.get("src", "")).strip()
        alt = str(img.get("alt", "")).strip() or "图表"
        if not src:
            continue
        key = f"{src}|{alt}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"src": src, "alt": alt})
    return out


def dedupe_rows_by_content(rows: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for row in rows:
        row_text = row.get("text", "")
        row_images = dedupe_image_items(row.get("images", []))
        merged = False
        for prev in kept:
            if rows_are_near_duplicate(row_text, prev.get("text", "")):
                prev["images"] = dedupe_image_items(prev.get("images", []) + row_images)
                merged = True
                break
        if merged:
            continue
        new_row = dict(row)
        new_row["images"] = row_images
        kept.append(new_row)
    return kept


def image_fingerprint(src: str, input_md: Path) -> str:
    path = resolve_local_image_path(src, input_md)
    if path and path.exists():
        try:
            digest = hashlib.md5(path.read_bytes()).hexdigest()
            return f"md5:{digest}"
        except Exception:
            pass
    return f"src:{src}"


def dedupe_new_points(items: list[str], seen_signatures: set[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        sig = normalize_point_signature(it)
        if not sig:
            continue
        is_dup = sig in seen_signatures
        if not is_dup:
            for prev in seen_signatures:
                short, long = (sig, prev) if len(sig) <= len(prev) else (prev, sig)
                if len(short) >= 16 and short in long:
                    is_dup = True
                    break
                if SequenceMatcher(a=sig, b=prev).ratio() >= 0.92:
                    is_dup = True
                    break
        if is_dup:
            continue
        seen_signatures.add(sig)
        out.append(it)
    return out


def infer_focus_title(token: str, topics: str, text: str) -> str:
    t = text
    if "期权墙" in t or ("Gamma" in t and token in ("黄金",)):
        return "期权墙与Gamma结构"
    if "Gamma" in t or "Delta" in t:
        return "衍生品结构与Greeks"
    if "AVWAP" in t or ("均线" in t and "支撑" in t):
        return "均线与成本线共振"
    if "布林" in t:
        return "布林中轨策略纪律"
    if "美元" in t and token == "黄金":
        return "美元联动与黄金波动"
    if "50日" in t:
        return "50日线支撑验证"
    if "仓位" in t or "加仓" in t or "减仓" in t:
        return "仓位与节奏管理"
    if "回调" in t or "二次回踩" in t:
        return "回调区间与入场等待"
    extra_topics = [x for x in topics.split() if x != token]
    if extra_topics:
        return f"{'/'.join(extra_topics[:2])}相关观点"
    return f"{token}核心观点"


def extract_level_snippets(text: str) -> list[str]:
    snippets = []
    for m in re.finditer(r"(支撑|阻力|止损|失效|观察位|分水岭|Flip Point)[^。；\n]*", text):
        s = m.group(0).strip(" -:：;；,.，、")
        if len(s) >= 6:
            snippets.append(s)
    if snippets:
        return dedupe_keep_order(snippets)[:4]

    nums = []
    for n in NUM_PAT.findall(text):
        raw = n.replace("$", "").replace(" ", "")
        if "-" in raw or "~" in raw:
            nums.append(raw)
            continue
        try:
            v = float(raw)
        except Exception:
            continue
        if 2024 <= v <= 2030:
            continue
        if v < 50:
            continue
        nums.append(raw)
    nums = dedupe_keep_order(nums)
    return nums[:6]


def refine_section(token: str, topics: str, raw_text: str) -> dict:
    normalized = normalize_text(raw_text)
    sents = sentence_chunks(normalized)
    core = [s for s in sents if is_core_sentence(token, s)]
    core = dedupe_keep_order(core)
    if not core:
        core = dedupe_keep_order([s for s in sents if not is_filler_sentence(s)])[:4]
    core = core[:6]

    levels = extract_level_snippets("。".join(core) if core else normalized)
    action_points = [s for s in core if ACTION_PAT.search(s)]
    action_points = dedupe_keep_order(action_points)[:3]

    return {
        "title": infer_focus_title(token, topics, normalized),
        "core_points": core,
        "levels": levels,
        "actions": action_points,
    }


def extract_markdown_images(text: str) -> list[dict]:
    out = []
    for m in IMAGE_MD_PAT.finditer(text):
        alt = m.group(1).strip() or "图表"
        src = m.group(2).strip()
        if src:
            out.append({"alt": alt, "src": src})
    return out


def strip_images_and_table_scaffold(text: str) -> str:
    t = IMAGE_MD_PAT.sub("", text)
    lines = []
    for raw_line in t.splitlines():
        line = raw_line.strip()
        # Remove markdown table separators and empty image placeholder tables.
        if line.startswith("|") and line.endswith("|"):
            inner = line.strip("|").strip().replace(" ", "").replace("-", "")
            if not inner:
                continue
            if set(inner) <= {"|"}:
                continue
        lines.append(raw_line)
    return "\n".join(lines).strip()


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def detect_project_from_creds(creds_path: str) -> str | None:
    p = Path(creds_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        pid = data.get("project_id")
        return str(pid).strip() if pid else None
    except Exception:
        return None


def make_vertex_config() -> dict:
    envf = load_env_file(ENV_PATH)
    creds = (
        envf.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or (str(SA_KEY_PATH) if SA_KEY_PATH.exists() else "")
    )
    project = (
        envf.get("GOOGLE_CLOUD_PROJECT")
        or envf.get("GOOGLE_PROJECT")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GOOGLE_PROJECT")
        or detect_project_from_creds(creds)
    )
    if creds:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds
    if not project or not creds:
        return {}
    return {"project": project, "creds": creds, "location": VERTEX_LOCATION}


def _get_vertex_creds(cfg: dict):
    global _VERTEX_CREDS
    if _VERTEX_CREDS is None:
        _VERTEX_CREDS = service_account.Credentials.from_service_account_file(
            cfg["creds"], scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    expiry = getattr(_VERTEX_CREDS, "expiry", None)
    if not _VERTEX_CREDS.token or not expiry:
        _VERTEX_CREDS.refresh(Request())
    else:
        # refresh if token is close to expiry
        if getattr(expiry, "tzinfo", None) is None:
            now = dt.datetime.now()
        else:
            now = dt.datetime.now(dt.timezone.utc).astimezone(expiry.tzinfo)
        if (expiry - now).total_seconds() < 120:
            _VERTEX_CREDS.refresh(Request())
    return _VERTEX_CREDS


def _extract_vertex_text(data: dict) -> str:
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        return ""
    texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
    return "\n".join([t for t in texts if t]).strip()


def vertex_generate_text(cfg: dict, prompt: str, max_output_tokens: int = 4096, temperature: float = 0.1) -> str:
    creds = _get_vertex_creds(cfg)
    url = (
        "https://aiplatform.googleapis.com/v1/"
        f"projects/{cfg['project']}/locations/{cfg['location']}/publishers/google/models/{DEFAULT_MODEL}:generateContent"
    )
    generation_config = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if LLM_THINKING_BUDGET > 0:
        generation_config["thinkingConfig"] = {"thinkingBudget": LLM_THINKING_BUDGET}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }

    last_err = None
    for i in range(4):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=(15, LLM_TIMEOUT_SEC))
            if resp.status_code != 200:
                # 429/5xx are usually transient; retry with backoff.
                body_head = resp.text[:400]
                if (
                    resp.status_code == 400
                    and "thinking_budget" in body_head.lower()
                    and "thinkingConfig" in payload.get("generationConfig", {})
                ):
                    payload["generationConfig"].pop("thinkingConfig", None)
                    continue
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"Vertex transient HTTP {resp.status_code}: {resp.text[:160]}")
                raise RuntimeError(f"Vertex API HTTP {resp.status_code}: {resp.text[:240]}")
            data = resp.json()
            text = _extract_vertex_text(data)
            if not text:
                reason = data.get("candidates", [{}])[0].get("finishReason", "")
                raise RuntimeError(f"Vertex API returned empty text (finishReason={reason})")
            return text
        except Exception as e:
            last_err = e
            # refresh token and retry
            try:
                _VERTEX_CREDS.refresh(Request())
                headers["Authorization"] = f"Bearer {_VERTEX_CREDS.token}"
            except Exception:
                pass
            if i < 3:
                time.sleep(1.5 * (2**i))
            continue
    raise RuntimeError(str(last_err) if last_err else "Vertex API call failed")


def extract_json_text(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end >= start:
        return text[start : end + 1]
    return text


def extract_json_object_text(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        return text[start : end + 1]
    return text


def extract_markdown_text(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown|md)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


_METHODOLOGY_CHUNK_SIZE = 25000


def _methodology_extract_prompt(text: str) -> str:
    return (
        "你是量化投资方法论编辑。请从以下原文片段中，提取所有与技术分析框架、交易方法论相关的要点。\n\n"
        "要求：\n"
        "1) 用简洁的要点列表输出，每条包含具体指标名称、使用方式和原文典型用法\n"
        "2) 覆盖所有提及的技术指标、分析工具、交易规则、买卖信号判断方法，即使只出现一次也要提取\n"
        "3) 不编造原文未提及的方法\n"
        "4) 不要输出代码块包裹，直接输出 Markdown\n\n"
        f"原文片段：\n{text}"
    )


def _methodology_merge_prompt(extracts: str) -> str:
    return (
        "你是量化投资方法论编辑。以下是从多段原文中分别提取的技术分析方法论要点。"
        "请将它们合并、去重，整理为一份完整的方法论总结。\n\n"
        "要求：\n"
        "1) 输出纯 Markdown，用 ### 分主题，每个主题 3-6 条要点\n"
        "2) 覆盖所有原文提及的技术指标、分析工具、交易规则（包括RSI、见顶信号、加仓信号、止损规则等），即使只出现一次也要收录\n"
        "3) 每条要点需引用具体指标名称和使用方式，附上原文中的典型用法\n"
        "4) 不编造原文未提及的方法\n"
        "5) 按重要性排序，最常用的框架排在前面\n"
        "6) 语言精炼，总字数控制在 1000-2000 字\n"
        "7) 不要输出代码块包裹，直接输出 Markdown\n\n"
        f"各批次提取结果：\n{extracts}"
    )


def summarize_methodology_with_llm(vertex_cfg: dict, sections: list[dict]) -> str:
    """Use LLM to dynamically summarize the methodology from source sections."""
    all_text_parts: list[str] = []
    for sec in sections:
        raw = strip_images_and_table_scaffold(str(sec.get("text", ""))).strip()
        if raw:
            all_text_parts.append(raw)
    if not all_text_parts:
        return "> 方法论总结生成失败，请重跑。"
    combined = "\n\n".join(all_text_parts)

    # Split into chunks
    chunks: list[str] = []
    for i in range(0, len(combined), _METHODOLOGY_CHUNK_SIZE):
        chunk = combined[i : i + _METHODOLOGY_CHUNK_SIZE]
        if chunk.strip():
            chunks.append(chunk)

    try:
        if len(chunks) == 1:
            # Single chunk: extract + format in one call
            prompt = _methodology_merge_prompt(chunks[0])
            raw = vertex_generate_text(vertex_cfg, prompt, max_output_tokens=4096, temperature=0.1)
            result = extract_markdown_text(raw)
        else:
            # Multi-chunk: extract from each, then merge
            extracts: list[str] = []
            for idx, chunk in enumerate(chunks):
                print(f"[METHODOLOGY] extracting chunk {idx + 1}/{len(chunks)} ...", flush=True)
                raw = vertex_generate_text(vertex_cfg, _methodology_extract_prompt(chunk), max_output_tokens=2048, temperature=0.1)
                extract = extract_markdown_text(raw)
                if extract:
                    extracts.append(extract)
            if not extracts:
                return "> 方法论总结生成失败，请重跑。"
            merged_input = "\n\n---\n\n".join(extracts)
            print(f"[METHODOLOGY] merging {len(extracts)} extracts ...", flush=True)
            raw = vertex_generate_text(vertex_cfg, _methodology_merge_prompt(merged_input), max_output_tokens=4096, temperature=0.1)
            result = extract_markdown_text(raw)

        if not result:
            return "> 方法论总结生成失败，请重跑。"
        return result
    except Exception as exc:
        print(f"[WARN] methodology LLM failed: {exc}", flush=True)
        return "> 方法论总结生成失败，请重跑。"


def summarize_instrument_with_llm(
    vertex_cfg: dict,
    display_name: str,
    current_price: float | None,
    rows: list[dict],
) -> dict:
    """Use LLM to summarize action, key level, and date from source text for an instrument."""
    fallback = {
        "action": "见正文",
        "core_summary": "见正文",
        "nearest_level": "-",
        "analysis_date": rows[0]["date"] if rows else "-",
    }
    if not vertex_cfg or not rows:
        return fallback

    # Build text payload from all matched rows (newest first)
    parts: list[str] = []
    for row in rows:
        raw = strip_images_and_table_scaffold(str(row.get("text", ""))).strip()
        if raw:
            parts.append(f"【{row['date']}】\n{raw}")
    if not parts:
        return fallback
    combined = "\n\n".join(parts)[:20000]

    price_info = f"当前价格：{current_price:.4f}" if current_price is not None else "当前价格：未知"
    today = dt.date.today().isoformat()

    prompt = (
        f"你是量化投资分析助手。以下是关于「{display_name}」的多篇原文分析，按日期排列。\n\n"
        f"今天日期：{today}\n"
        f"{price_info}\n\n"
        "请基于原文内容，输出一个 JSON 对象（不要输出其他文字）：\n\n"
        "{\n"
        '  "core_summary": "用一句话概括作者对该品种的核心判断/观点（20-40字，侧重分析结论）",\n'
        '  "action": "结合原文作者最新观点和当前价格，给出一句话操作建议（30-60字，侧重具体操作）",\n'
        '  "nearest_level": "原文提到的与当前价格最相关的支撑位或阻力位（如：支撑 440 / 阻力 475）",\n'
        '  "analysis_date": "原文中最近一次分析该品种的日期（YYYY-MM-DD格式）"\n'
        "}\n\n"
        "要求：\n"
        "1) action 必须基于原文作者的实际观点，不要编造\n"
        "2) 如果原文有多次分析，以最新一次的观点为主，但要考虑观点演进\n"
        "3) nearest_level 用原文提到的具体数值，格式如「支撑 440 / 阻力 475」\n"
        "4) 如果原文没提到具体点位，nearest_level 输出 \"-\"\n\n"
        f"原文：\n{combined}"
    )

    try:
        raw = vertex_generate_text(vertex_cfg, prompt, max_output_tokens=512, temperature=0.0)
        parsed = json.loads(extract_json_object_text(raw))
        if not isinstance(parsed, dict):
            return fallback
        core = compact_spaces(str(parsed.get("core_summary", ""))).strip()
        action = compact_spaces(str(parsed.get("action", ""))).strip()
        nearest = compact_spaces(str(parsed.get("nearest_level", ""))).strip()
        adate = compact_spaces(str(parsed.get("analysis_date", ""))).strip()
        return {
            "core_summary": core or fallback["core_summary"],
            "action": action or fallback["action"],
            "nearest_level": nearest or fallback["nearest_level"],
            "analysis_date": adate or fallback["analysis_date"],
        }
    except Exception as exc:
        print(f"[WARN] instrument summary LLM failed for {display_name}: {exc}", flush=True)
        return fallback


def build_refine_prompt(token: str, rows: list[dict]) -> str:
    payload = []
    for idx, row in enumerate(rows):
        payload.append(
            {
                "idx": idx,
                "date": row["date"],
                "topics": row.get("topics", ""),
                "text": strip_images_and_table_scaffold(row["text"])[:1200],
            }
        )
    today = dt.date.today().isoformat()
    return f"""
你是交易研究编辑。请把以下"同一品种的分日期原文"提炼成结构化要点。

今天日期：{today}
品种：{token}

严格要求：
1) 仅输出 JSON 数组，不要输出其他任何文字。
2) 数组长度必须与输入一致，按 idx 对齐。
3) 每项字段固定为：
   - idx: 整数
   - focus_title: 6-18字，必须是内容焦点标题，不能只写"{token}"
   - core_points: 3-6条，去emoji与口语废话，保留可执行结论
   - key_levels: 0-5条，尽量保留数字和含义（支撑/阻力/止损/分水岭）
   - actions: 0-3条，清晰写"当前应做什么/不做什么"
4) 不编造不存在的点位；若没有明确点位可留空数组。
5) 语言简洁，避免废话。
6) 去重：同一项内不得出现语义重复句子，保留信息量更高的一条。
7) 只保留"{token}"本品种内容：若段落主体是其他品种，core_points/key_levels/actions必须输出空数组。
8) 禁止输出其他品种的点位或代码（例如把消费/红利/军工/日经点位写到黄金）。

输入：
    {json.dumps(payload, ensure_ascii=False)}
"""


def build_validation_prompt(payload: dict) -> str:
    today = dt.date.today().isoformat()
    return f"""
你是交易研究质检员。请校验"对应操作"是否与原文判断一致，并与点位关系自洽。

今天日期：{today}

硬性要求：
1) 只基于输入证据判断，不得编造。
2) 仅输出 JSON 对象，不要输出其他任何文字。
3) verdict 只能是：准确 / 部分准确 / 不准确。
4) confidence 只能是：高 / 中 / 低。
5) issues 最多 3 条；无明显问题可输出空数组。
6) validated_action 输出 1 条可执行动作（20-60字）。

输入：
{json.dumps(payload, ensure_ascii=False)}

输出格式：
{{"verdict":"准确|部分准确|不准确","confidence":"高|中|低","issues":["..."],"validated_action":"..."}}
"""


def validate_judgement_with_llm(vertex_cfg: dict, payload: dict) -> dict:
    fallback_action = compact_spaces(str(payload.get("rule_action", ""))) or "见正文"
    fallback = {
        "verdict": "未校验",
        "confidence": "低",
        "issues": [],
        "validated_action": fallback_action,
    }
    if not LLM_INSTRUMENT_VALIDATION:
        fallback["issues"] = ["LLM校验未开启"]
        return fallback
    if not vertex_cfg:
        fallback["issues"] = ["Vertex配置不可用"]
        return fallback
    try:
        raw = vertex_generate_text(
            vertex_cfg,
            build_validation_prompt(payload),
            max_output_tokens=1024,
            temperature=0.0,
        )
        parsed = json.loads(extract_json_object_text(raw))
        if not isinstance(parsed, dict):
            raise RuntimeError("validation output is not object")
        verdict = compact_spaces(str(parsed.get("verdict", "")))
        if verdict not in {"准确", "部分准确", "不准确"}:
            verdict = "部分准确"
        confidence = compact_spaces(str(parsed.get("confidence", "")))
        if confidence not in {"高", "中", "低"}:
            confidence = "中"
        issues_raw = parsed.get("issues", [])
        if not isinstance(issues_raw, list):
            issues_raw = []
        issues = dedupe_keep_order([compact_spaces(str(x)) for x in issues_raw if compact_spaces(str(x))])[:3]
        validated_action = compact_spaces(str(parsed.get("validated_action", ""))) or fallback_action
        return {
            "verdict": verdict,
            "confidence": confidence,
            "issues": issues,
            "validated_action": validated_action,
        }
    except json.JSONDecodeError:
        fallback["verdict"] = "校验未完成"
        fallback["issues"] = ["LLM返回格式异常，无法解析"]
        return fallback
    except Exception as exc:
        err = compact_spaces(str(exc))[:120]
        fallback["verdict"] = "校验未完成"
        fallback["issues"] = [f"LLM校验失败：{err}"]
        return fallback


def build_proofread_prompt(section_md: str) -> str:
    return f"""
你是交易报告终审编辑。请只对下面这一个 Markdown 片段做校对与净化。

硬性要求：
1) 只输出"修订后的完整章节 Markdown"，不要解释，不要代码块包裹。
2) 保留原有 Markdown 结构（标题层级、表格、details 折叠块、图片链接）。
3) 不得新增事实，不得编造点位。
4) 不得改动主表/关键位表中的数值格式；图片链接和HTML结构保持可渲染。
5) 删除明显跨品种污染内容（例如把消费/红利/军工/日经点位写进黄金章节）。
6) 仅做必要的措辞修正、去重和污染剔除，不要大改结构。

待校对章节：
{section_md}
"""


def split_h2_sections(detail_text: str) -> list[str]:
    matches = list(re.finditer(r"(?m)^## .+$", detail_text))
    if not matches:
        return [detail_text]
    sections: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(detail_text)
        sections.append(detail_text[start:end].strip("\n"))
    return sections


def split_section_for_proofread(section_text: str, max_chars: int = 5000) -> list[str]:
    s = section_text.strip("\n")
    if len(s) <= max_chars:
        return [s]
    lines = s.splitlines()
    if not lines or not lines[0].startswith("## "):
        # Fallback: hard chunk by size.
        return [s[i : i + max_chars] for i in range(0, len(s), max_chars)]
    h2 = lines[0]
    body = "\n".join(lines[1:])
    h3_parts = [p.strip("\n") for p in re.split(r"(?m)(?=^### )", body) if p.strip()]
    if not h3_parts:
        return [s[i : i + max_chars] for i in range(0, len(s), max_chars)]
    chunks: list[str] = []
    cur_parts: list[str] = []
    cur_len = len(h2) + 2
    for part in h3_parts:
        part_len = len(part) + 2
        if cur_parts and cur_len + part_len > max_chars:
            chunks.append((h2 + "\n\n" + "\n\n".join(cur_parts)).strip("\n"))
            cur_parts = [part]
            cur_len = len(h2) + 2 + part_len
        else:
            cur_parts.append(part)
            cur_len += part_len
    if cur_parts:
        chunks.append((h2 + "\n\n" + "\n\n".join(cur_parts)).strip("\n"))
    return chunks


def merge_proofread_blocks(blocks: list[str]) -> str:
    if not blocks:
        return ""
    merged: list[str] = [blocks[0].strip("\n")]
    for blk in blocks[1:]:
        lines = blk.strip("\n").splitlines()
        if lines and lines[0].startswith("## "):
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
        tail = "\n".join(lines).strip("\n")
        if tail:
            merged.append(tail)
    return "\n\n".join(merged).strip("\n")


def proofread_detail_sections_with_llm(vertex_cfg: dict, detail_lines: list[str]) -> list[str]:
    if not detail_lines or not LLM_FINAL_PROOFREAD:
        return detail_lines
    detail_text = "\n".join(detail_lines).strip("\n")
    sections = split_h2_sections(detail_text)
    revised: list[str] = []
    total = len(sections)
    for idx, sec in enumerate(sections, start=1):
        blocks = split_section_for_proofread(sec, max_chars=5000)
        fixed_blocks: list[str] = []
        for jdx, block in enumerate(blocks, start=1):
            print(f"[PROOFREAD] section={idx}/{total} block={jdx}/{len(blocks)}", flush=True)
            raw = vertex_generate_text(
                vertex_cfg,
                build_proofread_prompt(block),
                max_output_tokens=4096,
                temperature=0.0,
            )
            fixed = extract_markdown_text(raw)
            if not fixed.strip():
                raise RuntimeError("Proofread output is empty.")
            fixed_blocks.append(fixed.strip("\n"))
        revised.append(merge_proofread_blocks(fixed_blocks))
    merged = "\n\n".join(revised).strip("\n")
    return merged.splitlines()


def refine_sections_with_llm(vertex_cfg: dict, token: str, rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    if not vertex_cfg:
        raise RuntimeError("Vertex Gemini unavailable.")
    out = []
    batch_size = LLM_BATCH_SIZE
    total_batches = (len(rows) + batch_size - 1) // batch_size if rows else 0
    for start in range(0, len(rows), batch_size):
        batch_rows = rows[start : start + batch_size]
        batch_idx = (start // batch_size) + 1
        print(
            f"[LLM] token={token} batch={batch_idx}/{total_batches} rows={len(batch_rows)}",
            flush=True,
        )
        raw = vertex_generate_text(
            vertex_cfg,
            build_refine_prompt(token, batch_rows),
            max_output_tokens=4096,
            temperature=0.1,
        )
        parsed = json.loads(extract_json_text(raw))
        if not isinstance(parsed, list) or len(parsed) != len(batch_rows):
            raise RuntimeError("Gemini response is not aligned with input rows.")
        for i, item in enumerate(parsed):
            if not isinstance(item, dict):
                raise RuntimeError("Gemini response item is not an object.")
            title = str(item.get("focus_title", "")).strip()
            if not title:
                raise RuntimeError("Gemini focus_title is empty.")
            core = [str(x).strip() for x in item.get("core_points", []) if str(x).strip()]
            levels = [str(x).strip() for x in item.get("key_levels", []) if str(x).strip()]
            actions = [str(x).strip() for x in item.get("actions", []) if str(x).strip()]
            core = dedupe_keep_order(core)[:6]
            levels = dedupe_keep_order(levels)[:5]
            actions = dedupe_keep_order(actions)[:3]
            out.append(
                post_process_refined_item(
                    {
                        "title": title,
                        "core_points": core,
                        "levels": levels,
                        "actions": actions,
                    }
                )
            )
    if len(out) != len(rows):
        raise RuntimeError("Gemini output row count mismatch.")
    return out


def chunk_relevance(token: str, chunk: str) -> int:
    aliases = TOKEN_ALIASES.get(token, [token])
    allowed_cross = TOKEN_CROSS_ALLOWED_ALIASES.get(token, set())
    other_aliases = []
    for k, arr in TOKEN_ALIASES.items():
        if k != token:
            for a in arr:
                if a not in allowed_cross:
                    other_aliases.append(a)

    score = 0
    own_hit = any(a in chunk for a in aliases)
    foreign_hit = any(oa in chunk for oa in other_aliases if oa and len(oa) > 1)
    if own_hit:
        score += 3
    if any(k in chunk for k in CORE_KEYWORDS):
        score += 2
    if re.search(r"[$￥]?\d+(?:\.\d+)?", chunk):
        score += 1
    if any(n in chunk for n in NOISE_HINTS):
        score -= 2
    # No own mention at all but foreign tokens present → strong penalty
    if foreign_hit and not own_hit:
        score -= 2
    # Both own and foreign tokens present → check dominance
    elif foreign_hit and own_hit:
        own_score = token_alias_score(token, chunk)
        other_max = max(
            (token_alias_score(other, chunk) for other in TOKEN_ALIASES if other != token),
            default=0,
        )
        if other_max > own_score:
            # Foreign token dominates → very strong penalty (this paragraph mainly
            # discusses a different instrument, e.g. "465美元" in a gold paragraph)
            score -= 5
        else:
            score -= 1
    if len(chunk) < 10:
        score -= 2
    return score


def token_alias_score(token: str, text: str) -> int:
    aliases = TOKEN_ALIASES.get(token, [token])
    score = 0
    for a in aliases:
        if not a:
            continue
        c = text.count(a)
        if c <= 0:
            continue
        # Give stronger weight to ticker/id-like aliases.
        strong = bool(re.search(r"[A-Z0-9.]{2,}", a)) or len(a) >= 4
        score += c * (2 if strong else 1)
    return score


def row_relevant_to_token(token: str, topics_text: str, body_text: str) -> bool:
    aliases = TOKEN_ALIASES.get(token, [token])
    topic_hit = token in topics_text or any(a in topics_text for a in aliases)
    combined = f"{topics_text}\n{body_text}"
    own_score = token_alias_score(token, combined)
    other_scores = [
        token_alias_score(other, combined) for other in TOKEN_ALIASES.keys() if other != token
    ]
    other_max = max(other_scores) if other_scores else 0
    keyword_hit = any(k in body_text for k in CORE_KEYWORDS)

    if topic_hit:
        return True
    if own_score >= 2 and own_score > other_max:
        return True
    if own_score >= 1 and keyword_hit and own_score >= other_max + 1:
        return True
    return False


def has_foreign_token_signal(token: str, text: str) -> bool:
    allowed = TOKEN_CROSS_ALLOWED_ALIASES.get(token, set())
    for other, aliases in TOKEN_ALIASES.items():
        if other == token:
            continue
        for a in aliases:
            if not a or len(a) <= 1:
                continue
            if a in allowed:
                continue
            if a in text:
                return True
    return False


def point_relevant_to_token(token: str, text: str) -> bool:
    s = compact_spaces(text)
    if not s:
        return False
    own_hit = any(a in s for a in TOKEN_ALIASES.get(token, [token]))
    foreign_hit = has_foreign_token_signal(token, s)

    # Gold section frequently contains COMEX-linked levels; keep those.
    if token == "黄金":
        if re.search(r"\b1[45]\d{3}\b", s) and not re.search(r"GLD|COMEX|黄金|金价", s):
            return False
        if "COMEX" in s:
            own_hit = True

    # If foreign token signal is present but own token signal is absent, drop it.
    if foreign_hit and not own_hit:
        return False
    return True


def filter_points_by_token(token: str, items: list[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        if point_relevant_to_token(token, it):
            out.append(it)
    return dedupe_keep_order(out)


def full_sections_for_token(sections: list[dict], token: str) -> list[dict]:
    """Collect whole articles relevant to *token*, with dominance filtering.

    Strategy (section-level, preserving temporal narrative):
    - Title/topic hit → keep entire article.
    - Otherwise check article-level dominance: own alias score must beat
      every other token's score.  This prevents gold articles that casually
      mention "美元" from leaking into the DXY section.
    - Images are kept alongside the article text.
    """
    aliases = TOKEN_ALIASES.get(token, [token])
    rows: list[dict] = []

    for s in sections:
        topics_text = " ".join(s["topics"])
        body_text = extract_original_part(s["text"])
        if not body_text:
            continue

        images = extract_markdown_images(body_text)
        text_only = strip_images_and_table_scaffold(body_text)
        topic_hit = token in s["topics"] or any(a in topics_text for a in aliases)

        if topic_hit:
            kept_text = text_only.strip()
        else:
            # Article-level dominance check
            own_score = token_alias_score(token, text_only)
            if own_score < 1:
                continue
            other_max = max(
                (token_alias_score(other, text_only)
                 for other in TOKEN_ALIASES if other != token),
                default=0,
            )
            # Only include if this token dominates the article
            if own_score < other_max:
                continue
            keyword_hit = any(k in text_only for k in CORE_KEYWORDS)
            if own_score < 2 and not keyword_hit:
                continue
            kept_text = text_only.strip()

        if kept_text.strip() or images:
            rows.append(
                {
                    "date": s["date"],
                    "topics": topics_text,
                    "text": kept_text,
                    "images": dedupe_image_items(images) if kept_text.strip() else [],
                }
            )

    return dedupe_rows_by_content(rows)


def infer_action(inst: dict, current: float) -> str:
    iid = inst["id"]
    if iid == "gold_gld":
        if current < 435:
            return "跌破失效位，暂停加仓，等待新平衡。"
        if 440 <= current <= 450:
            return "位于支撑带内，可按分批节奏加仓。"
        if current < 465:
            return "支撑与阻力之间，持有/观察为主，不追。"
        if current < 475:
            return "进入阻力前沿，谨慎看待追涨。"
        return "若能连续站稳475，可上看更高目标位。"

    if iid == "consumption":
        if current < 14700:
            return "触发失效位，执行风控，暂停开仓。"
        if current < 15115:
            return "低于观察位，按原文规则不新增仓位。"
        if current < 15500:
            return "回到观察位上方，但未过主阻力，先观察。"
        return "站上15500后可逐步恢复右侧加仓。"

    if iid == "dividend":
        if current <= 5300:
            return "进入历史舒适回补区，可开始网格分批。"
        return "高于历史回补带，新增仓位宜慢，不追涨。"

    if iid == "dxy":
        if current >= 96.6:
            return "处于关键支撑上方，美元偏强阶段成立。"
        return "跌破关键支撑，美元强势逻辑弱化。"

    if iid == "tlt":
        if current < 91.5:
            return "仍在关键阻力下方，不做进攻型加仓。"
        return "重回阻力上方，可观察是否转强延续。"

    if iid == "vgit":
        if current < 59.6:
            return "低于参考阻力位，偏中性观察。"
        return "高于参考阻力位，相对长债更稳。"

    if iid == "comm":
        if current <= 1.05:
            return "接近历史买入观察位，可小仓试错。"
        if current < 1.25:
            return "位于区间中段，按波段纪律管理仓位。"
        return "接近/触及历史阻力，优先止盈而非追涨。"

    if iid == "xlv":
        if 130 <= current <= 132:
            return "处于历史低吸区，可分批配置。"
        if current > 132:
            return "高于低吸区，等待回撤再考虑加仓。"
        return "低于低吸区，需确认是否趋势走坏后再决定。"

    if iid == "oil_gas_uso":
        return "长期看好油气，向下动力有限，逢低分批买入持有。"

    return "按原文纪律执行，等待关键位触发。"


def materialize_source_image(
    src: str,
    input_md: Path,
    source_images_dir: Path,
    copied: dict[str, str],
) -> str | None:
    if src in copied:
        return copied[src]
    if src.startswith("data:"):
        copied[src] = src
        return src
    if "://" in src:
        copied[src] = src
        return src
    resolved = (input_md.parent / src).resolve()
    if not resolved.exists():
        copied[src] = ""
        return None
    if EMBED_SOURCE_IMAGES:
        uri = image_to_data_uri(resolved)
        copied[src] = uri
        return uri
    source_images_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", resolved.stem).strip("._") or "image"
    suffix = resolved.suffix.lower() or ".png"
    path_sig = hashlib.md5(str(resolved).encode("utf-8", "ignore")).hexdigest()[:10]
    dst = source_images_dir / f"{safe_stem}_{path_sig}{suffix}"
    if (not dst.exists()) or (resolved.stat().st_mtime > dst.stat().st_mtime):
        shutil.copy2(resolved, dst)
    rel = f"{source_images_dir.name}/{dst.name}"
    copied[src] = rel
    return rel




def dedupe_report_lines_global(lines: list[str]) -> list[str]:
    seen_bullets: set[str] = set()
    seen_quotes: set[str] = set()
    seen_imgs: set[str] = set()
    out: list[str] = []
    for line in lines:
        raw = line.rstrip()
        stripped = raw.strip()
        if raw.lstrip().startswith("<img ") and 'src="' in raw:
            m = re.search(r'src="([^"]+)"', raw)
            if m:
                src = m.group(1).strip()
                if src in seen_imgs:
                    continue
                seen_imgs.add(src)
        if stripped.startswith("- "):
            sig = normalize_point_signature(stripped[2:])
            if len(sig) >= 12 and sig in seen_bullets:
                continue
            if len(sig) >= 12:
                seen_bullets.add(sig)
        elif stripped.startswith(">"):
            sig = normalize_point_signature(stripped)
            if len(sig) >= 12 and sig in seen_quotes:
                continue
            if len(sig) >= 12:
                seen_quotes.add(sig)
        if stripped == "" and out and out[-1].strip() == "":
            continue
        out.append(raw)
    return out


def build_dashboard(vertex_cfg: dict | None, summary_rows: list[dict]) -> list[str]:
    """Generate a data-driven cross-asset snapshot (no LLM).

    For each main instrument, output one line:
      name (current) — nearest level — action
    """
    if not summary_rows:
        return []

    now = dt.datetime.now().strftime("%Y-%m-%d")
    out: list[str] = ["## 全局快照", "", f"生成时间：{now}", ""]
    out.append("| 品种 | 当前点位 | 最近关键位 | 对应操作 |")
    out.append("| --- | ---: | --- | --- |")

    for row in summary_rows:
        sig = row.get("signal", "主力")
        if sig != "主力":
            continue
        name = row.get("name", "")
        cur = row.get("current", "-")
        nearest = row.get("nearest", "-")
        action = row.get("action", "-")
        out.append(f"| {name} | {cur} | {nearest} | {action} |")

    out.append("")
    return out


def build_report(md_text: str, input_md: Path, out_md: Path, charts_dir: Path) -> None:
    sections = parse_sections(md_text)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    vertex_cfg = make_vertex_config()
    if not vertex_cfg:
        raise RuntimeError("Vertex Gemini config is unavailable.")
    if vertex_cfg:
        print(
            f"[LLM-CONFIG] provider=vertex model={DEFAULT_MODEL} location={vertex_cfg['location']}",
            flush=True,
        )
    rows_cache: dict[str, list[dict]] = {}
    refined_cache: dict[str, list[dict]] = {}
    source_images_dir = out_md.parent / f"{out_md.stem}_source_images"
    source_img_map: dict[str, str] = {}
    lines = []
    summary_rows: list[dict] = []
    detail_lines: list[str] = []
    watchlist_rows: list[dict] = []
    # Global image de-duplication across all instruments/dates.
    global_seen_image_signatures: set[str] = set()
    global_seen_core_signatures: set[str] = set()
    global_seen_level_signatures: set[str] = set()
    global_seen_action_signatures: set[str] = set()
    report_date = dt.datetime.now().strftime("%Y-%m-%d")
    title = f"# 多资产品种点位分析（{report_date}）"
    lines.append(title)
    lines.append("")
    lines.append(f"生成时间：{now}")
    lines.append("原文来源：[量化榨汁机](https://weibo.com/u/6937480224)")
    lines.append("")
    lines.append('说明：图中虚线为原文提及关键位；"对应操作"基于原文作者观点由 LLM 提炼生成。')
    lines.append("原文部分按日期保留，附对应日期的技术图表。")
    lines.append("")

    emitted_group_headings: set[str] = set()
    for inst in INSTRUMENTS:
        display_name = normalize_instrument_name(inst.get("name", ""), inst.get("symbol", ""))
        inst_id = inst.get("id", "")
        is_watchlist_inst = inst_id in WATCHLIST_INSTRUMENTS

        # Check if this instrument belongs to a group
        group_heading = None
        for gh, ids in INSTRUMENT_GROUPS.items():
            if inst_id in ids:
                group_heading = gh
                break

        # Emit headings only for non-watchlist instruments
        if not is_watchlist_inst:
            if group_heading and group_heading not in emitted_group_headings:
                detail_lines.append(f"### {group_heading}")
                detail_lines.append("")
                emitted_group_headings.add(group_heading)
            if group_heading:
                detail_lines.append(f"#### {display_name}")
            else:
                detail_lines.append(f"### {display_name}")
        # Sub-heading level: #### for top-level instruments, ##### inside groups
        h3 = "#####" if group_heading else "####"
        summary_added = False
        try:
            price_basis, _level_basis = validate_instrument_basis(inst)
            df, data_source = fetch_history(inst, "1y")
            chart_path = charts_dir / f"{inst['id']}.png"
            current = plot_chart(df, inst, chart_path)
            last_date = df["date"].iloc[-1].strftime("%Y-%m-%d")
            chart_data_uri = image_to_data_uri(chart_path)
            # Fetch matching source text rows for LLM summary
            if inst["token"] not in rows_cache:
                rows_cache[inst["token"]] = full_sections_for_token(sections, inst["token"])
            _summary_rows_for_llm = rows_cache[inst["token"]]
            print(f"[LLM-ACTION] {display_name} ...", flush=True)
            inst_summary = summarize_instrument_with_llm(
                vertex_cfg, display_name, current, _summary_rows_for_llm
            )
            action_text = inst_summary["action"]
            nearest_text = inst_summary["nearest_level"]
            analysis_date = inst_summary["analysis_date"]

            summary_rows.append(
                {
                    "name": display_name,
                    "current": f"{current:.4f}",
                    "date": analysis_date,
                    "nearest": nearest_text,
                    "action": action_text,
                    "source": data_source,
                    "basis": price_basis,
                    "category": classify_summary_category(
                        display_name,
                        price_basis,
                        data_source,
                        token=inst.get("token", ""),
                        symbol=inst.get("symbol", ""),
                    ),
                }
            )
            summary_added = True

            # Watchlist instruments: summary row only, no detail section
            if is_watchlist_inst:
                watchlist_rows.append(
                    {
                        "name": display_name,
                        "date": last_date,
                        "core": f"点位 {current:.4f}，{action_text}",
                        "action": nearest_text or "-",
                    }
                )
                continue

            detail_lines.append(render_image(chart_data_uri, f"{display_name}走势图", TOP_CHART_WIDTH))
            detail_lines.append("")
            detail_lines.append(f"{h3} 关键位与操作建议")
            detail_lines.append("")
            if inst["levels"]:
                detail_lines.append("| 关键位 | 数值 | 类型 | 来源日期 | 与现价偏离 |")
                detail_lines.append("| --- | ---: | --- | --- | ---: |")
                for lv in inst["levels"]:
                    diff = (current - lv["value"]) / lv["value"] * 100
                    detail_lines.append(
                        f"| {lv['label']} | {lv['value']:.4f} | {lv['kind']} | {lv['source']} | {diff:+.2f}% |"
                    )
            else:
                detail_lines.append("> 关键位：暂未配置。")
            detail_lines.append("")
            detail_lines.append(f"> **对应操作：{action_text}**")
            detail_lines.append("")
            # Track which tokens already had their source text rendered
            token_already_rendered = inst["token"] in rows_cache and inst["token"] in refined_cache
            if inst["token"] not in rows_cache:
                rows_cache[inst["token"]] = full_sections_for_token(sections, inst["token"])
            full_rows = rows_cache[inst["token"]]

            # If this token's source text was already rendered by a sibling instrument,
            # show a cross-reference instead of duplicate/empty content.
            _first_inst_for_token = None
            if token_already_rendered:
                for prev in INSTRUMENTS:
                    if prev["token"] == inst["token"] and prev["id"] != inst_id:
                        _first_inst_for_token = normalize_instrument_name(prev.get("name", ""), prev.get("symbol", ""))
                        break
            if _first_inst_for_token:
                detail_lines.append(f"> 原文与 **{_first_inst_for_token}** 共享，请参见该品种的原文部分。")
                detail_lines.append("")
                # Still build validation_evidence from cached data
                validation_evidence: list[dict] = []
                if LLM_SECTION_REFINEMENT:
                    refined_rows = refined_cache.get(inst["token"], [])
                else:
                    refined_rows = [
                        refine_section(inst["token"], str(r.get("topics", "")), str(r.get("text", ""))) for r in full_rows
                    ]
                for item_v, refined_v in zip(full_rows, refined_rows):
                    norm_v = post_process_refined_item(refined_v)
                    validation_evidence.append(
                        {
                            "date": item_v.get("date", ""),
                            "focus_title": norm_v.get("title", ""),
                            "core_points": norm_v.get("core_points", [])[:4],
                            "key_levels": norm_v.get("levels", [])[:4],
                            "actions": norm_v.get("actions", [])[:2],
                        }
                    )
            else:
                # Compute date range for summary label
                _dates = [r.get("date", "") for r in full_rows if r.get("date")]
                _date_range = f"{_dates[-1]} ~ {_dates[0]}" if len(_dates) >= 2 else (_dates[0] if _dates else "")
                detail_lines.append("<details>")
                detail_lines.append(f'<summary>📂 原文记录（{_date_range}，共 {len(full_rows)} 篇）</summary>')
                detail_lines.append("")
                if LLM_SECTION_REFINEMENT:
                    if inst["token"] not in refined_cache:
                        refined_cache[inst["token"]] = refine_sections_with_llm(vertex_cfg, inst["token"], full_rows)
                    refined_rows = refined_cache[inst["token"]]
                else:
                    refined_rows = [
                        refine_section(inst["token"], str(r.get("topics", "")), str(r.get("text", ""))) for r in full_rows
                    ]
                    refined_cache[inst["token"]] = refined_rows
                validation_evidence: list[dict] = []
                seen_core_signatures: set[str] = set()
                seen_level_signatures: set[str] = set()
                seen_action_signatures: set[str] = set()
                rendered_blocks = 0
                if not full_rows:
                    detail_lines.append("- 无匹配原文。")
                for item, refined in zip(full_rows, refined_rows):
                    # Skip empty shell entries (no text content)
                    raw_excerpt = strip_images_and_table_scaffold(str(item.get("text", ""))).strip()
                    # Also strip lines that are only punctuation/whitespace
                    raw_excerpt = "\n".join(
                        ln for ln in raw_excerpt.splitlines()
                        if ln.strip() and not PUNCT_ONLY_PAT.match(ln)
                    ).strip()
                    if not raw_excerpt:
                        # Still check images
                        pass
                    normalized = post_process_refined_item(refined)
                    validation_evidence.append(
                        {
                            "date": item.get("date", ""),
                            "focus_title": normalized.get("title", ""),
                            "core_points": normalized.get("core_points", [])[:4],
                            "key_levels": normalized.get("levels", [])[:4],
                            "actions": normalized.get("actions", [])[:2],
                            "raw_excerpt": compact_spaces(raw_excerpt)[:360],
                        }
                    )
                    image_raw: list[tuple[str, str, tuple[int, int] | None]] = []
                    for img in dedupe_image_items(item.get("images", [])):
                        fp = image_fingerprint(img["src"], input_md)
                        if fp in global_seen_image_signatures:
                            continue
                        global_seen_image_signatures.add(fp)
                        img_link = materialize_source_image(img["src"], input_md, source_images_dir, source_img_map)
                        if not img_link:
                            continue
                        local_path = resolve_local_image_path(img["src"], input_md)
                        size = detect_image_size(path=local_path, data_uri=img_link)
                        image_raw.append((img_link, f"{item['date']} {img['alt']}", size))

                    if LLM_SECTION_REFINEMENT:
                        local_core = dedupe_new_points(normalized["core_points"], seen_core_signatures)
                        local_levels = dedupe_new_points(normalized["levels"], seen_level_signatures)
                        local_levels = filter_levels_by_price_context(local_levels, current)
                        local_actions = dedupe_new_points(normalized["actions"], seen_action_signatures)
                        new_core = dedupe_new_points(local_core, global_seen_core_signatures)
                        new_levels = dedupe_new_points(local_levels, global_seen_level_signatures)
                        new_actions = dedupe_new_points(local_actions, global_seen_action_signatures)
                        if not new_core and not new_levels and not new_actions and not image_raw:
                            continue
                    else:
                        if not raw_excerpt and not image_raw:
                            continue

                    detail_lines.append("")
                    detail_lines.append(f"{h3} {item['date']} | {normalized['title']}")
                    detail_lines.append("")
                    if LLM_SECTION_REFINEMENT:
                        if new_core:
                            detail_lines.append(f"{h3} 核心观点")
                            for p in new_core:
                                detail_lines.append(f"- {p}")
                            detail_lines.append("")
                        if new_levels:
                            detail_lines.append(f"{h3} 关键点位")
                            for lv in new_levels:
                                detail_lines.append(f"- {lv}")
                            detail_lines.append("")
                        if new_actions:
                            detail_lines.append(f"{h3} 对应操作")
                            for ac in new_actions:
                                detail_lines.append(f"- {ac}")
                            detail_lines.append("")
                    else:
                        if raw_excerpt:
                            detail_lines.extend(break_into_paragraphs(raw_excerpt).splitlines())
                            detail_lines.append("")
                    if image_raw:
                        portrait_items = [(src, alt, size) for src, alt, size in image_raw if size and size[1] > size[0] * 1.2]
                        landscape_items = [(src, alt, size) for src, alt, size in image_raw if (src, alt, size) not in portrait_items]
                        if landscape_items:
                            ls_row = [(src, alt, choose_source_image_width(size, in_row=True)) for src, alt, size in landscape_items]
                            detail_lines.extend(render_image_row(ls_row))
                        if portrait_items:
                            pt_row = [(src, alt, min(280, choose_source_image_width(size, in_row=True))) for src, alt, size in portrait_items]
                            detail_lines.extend(render_image_row(pt_row))
                        detail_lines.append("")
                    rendered_blocks += 1
                if full_rows and rendered_blocks == 0:
                    if LLM_SECTION_REFINEMENT:
                        detail_lines.append("- 无新增要点（跨日期去重后）。")
                    else:
                        detail_lines.append("- 无可展示原文。")
                detail_lines.append("</details>")
                detail_lines.append("")
                detail_lines.append("---")
                detail_lines.append("")
        except Exception as exc:
            if "Gemini" in str(exc) or "timed out" in str(exc):
                raise
            if not summary_added:
                summary_rows.append(
                    {
                        "name": display_name,
                        "current": "抓取失败",
                        "date": "-",
                        "nearest": "-",
                        "action": "数据异常，待重跑",
                        "source": f"error:{exc}",
                        "basis": normalize_basis_label(inst.get("price_basis", "UNKNOWN")),
                    }
                )
            if not is_watchlist_inst:
                if not summary_added:
                    detail_lines.append(f"- 当前点位：`抓取失败`（{exc}）")
                else:
                    detail_lines.append(f"- 原文提炼阶段异常：`{exc}`")
                detail_lines.append("")

    # --- Uncovered instruments: discover from source text via analyze_source_coverage ---

    coverage = analyze_source_coverage(md_text, sections)
    # Filter: skip noise symbols that are not real tradeable instruments
    _symbol_blocklist = {
        "PE", "PB", "RSI", "EPS", "GDP", "CPI", "PPI", "PMI",  # metrics
        "AI", "IP", "IT", "CEO", "CFO", "SEC", "FED", "FOMC",  # acronyms
        "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG",
        "SEP", "OCT", "NOV", "DEC",  # months
        "BRK", "VS",  # misc noise
    }
    uncovered_items = [
        c for c in coverage
        if not c["covered"] and c["hit"] >= 2 and c["name"] not in _symbol_blocklist
    ]
    existing_summary_names = {row.get("name", "") for row in summary_rows}

    # Dedup US stock tokens: merge 纳指/标普/QQQ/SPY/VTV etc. into 美股
    _us_primary = {"美股", "纳指", "纳斯达克", "标普500", "标普"}
    _us_secondary = {"QQQ", "SPY", "VTV", "NDX", "SPX", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOG", "META"}
    _us_all = _us_primary | _us_secondary
    _us_found = [c for c in uncovered_items if c["name"] in _us_all]
    if len(_us_found) >= 2:
        # Keep the primary token with highest hit count as anchor
        primary = [c for c in _us_found if c["name"] in _us_primary]
        anchor = max(primary, key=lambda c: c["hit"]) if primary else _us_found[0]
        merged_away: set[str] = set()
        for other in _us_found:
            if other["name"] == anchor["name"]:
                continue
            anchor["hit"] += other["hit"]
            merged_away.add(other["name"])
        if merged_away:
            uncovered_items = [c for c in uncovered_items if c["name"] not in merged_away]

    for uc_item in uncovered_items:
        token = uc_item["name"]
        display_name = normalize_instrument_name(token)
        if display_name in existing_summary_names:
            continue
        rows = full_sections_for_token(sections, token)
        # Skip items with no matching source text
        if not rows:
            continue
        is_main = len(rows) >= 3

        # Use LLM to summarize action from source text
        date_hint = rows[0].get("date", "-") if rows else "-"
        signal_label = "主力" if is_main else "观察"
        print(f"[LLM-ACTION] {display_name} (uncovered) ...", flush=True)
        uc_summary = summarize_instrument_with_llm(vertex_cfg, display_name, None, rows)
        uc_action = uc_summary["action"]
        uc_nearest = uc_summary["nearest_level"]
        uc_date = uc_summary["analysis_date"]
        core_summary = uc_summary["core_summary"]

        if uc_action != "见正文":
            _nearest_disp = uc_nearest if len(uc_nearest) <= 40 else uc_nearest[:38] + "…"
            _action_disp = uc_action if len(uc_action) <= 40 else uc_action[:38] + "…"
            summary_rows.append(
                {
                    "name": display_name,
                    "current": "-",
                    "date": uc_date,
                    "nearest": _nearest_disp,
                    "action": _action_disp,
                    "source": "source:coverage",
                    "basis": "TEXT",
                    "category": classify_summary_category(display_name, "TEXT", "source:coverage"),
                    "signal": signal_label,
                }
            )
        existing_summary_names.add(display_name)

        if is_main:
            # --- Major uncovered: generate detail section ---
            detail_lines.append(f"### {display_name}")
            detail_lines.append("")
            if uc_action != "见正文":
                detail_lines.append(f"> **对应操作：{uc_action}**")
                detail_lines.append("")
            if uc_nearest != "-":
                detail_lines.append(f"> 关键位：{uc_nearest}")
                detail_lines.append("")
            _uc_dates = [r.get("date", "") for r in rows if r.get("date")]
            _uc_date_range = f"{_uc_dates[-1]} ~ {_uc_dates[0]}" if len(_uc_dates) >= 2 else (_uc_dates[0] if _uc_dates else "")
            detail_lines.append("<details>")
            detail_lines.append(f'<summary>📂 原文记录（{_uc_date_range}，共 {len(rows)} 篇）</summary>')
            detail_lines.append("")
            for row in rows:
                row_text = strip_images_and_table_scaffold(
                    extract_original_part(str(row.get("text", "")))
                ).strip()
                # Collect images for this row
                uc_image_raw: list[tuple[str, str, tuple[int, int] | None]] = []
                for img in dedupe_image_items(row.get("images", [])):
                    fp = image_fingerprint(img["src"], input_md)
                    if fp in global_seen_image_signatures:
                        continue
                    global_seen_image_signatures.add(fp)
                    img_link = materialize_source_image(img["src"], input_md, source_images_dir, source_img_map)
                    if not img_link:
                        continue
                    local_path = resolve_local_image_path(img["src"], input_md)
                    size = detect_image_size(path=local_path, data_uri=img_link)
                    uc_image_raw.append((img_link, f"{row.get('date', '')} {img['alt']}", size))
                if not row_text and not uc_image_raw:
                    continue
                row_date = row.get("date", "")
                row_refined = refine_section(token, str(row.get("topics", "")), str(row.get("text", "")))
                row_title = row_refined.get("title", "原文")
                detail_lines.append(f"#### {row_date} | {row_title}")
                if row_text:
                    detail_lines.extend(break_into_paragraphs(row_text).splitlines())
                if uc_image_raw:
                    detail_lines.append("")
                    uc_portrait = [(src, alt, sz) for src, alt, sz in uc_image_raw if sz and sz[1] > sz[0] * 1.2]
                    uc_landscape = [(src, alt, sz) for src, alt, sz in uc_image_raw if (src, alt, sz) not in uc_portrait]
                    if uc_landscape:
                        uc_ls_row = [(src, alt, choose_source_image_width(sz, in_row=True)) for src, alt, sz in uc_landscape]
                        detail_lines.extend(render_image_row(uc_ls_row))
                    if uc_portrait:
                        uc_pt_row = [(src, alt, min(280, choose_source_image_width(sz, in_row=True))) for src, alt, sz in uc_portrait]
                        detail_lines.extend(render_image_row(uc_pt_row))
                detail_lines.append("")
            detail_lines.append("</details>")
            detail_lines.append("")
            detail_lines.append("---")
            detail_lines.append("")
        else:
            # --- Watchlist uncovered: collect for compact table ---
            raw_text_parts = []
            for row in rows:
                row_text = strip_images_and_table_scaffold(
                    extract_original_part(str(row.get("text", "")))
                ).strip()
                if not row_text:
                    continue
                row_date = row.get("date", "")
                row_topics = str(row.get("topics", ""))
                raw_text_parts.append(f"**{row_date} | {row_topics}**\n\n{break_into_paragraphs(row_text)}")
            watchlist_rows.append(
                {
                    "name": display_name,
                    "date": uc_date,
                    "core": core_summary,
                    "action": uc_action,
                    "raw_text": "\n\n".join(raw_text_parts),
                }
            )

    # --- Watchlist section (low-evidence uncovered instruments) ---
    watchlist_lines: list[str] = []
    if watchlist_rows:
        watchlist_lines.append("## 四、观察清单")
        watchlist_lines.append("")
        watchlist_lines.append("| 品种 | 日期 | 核心观点 | 操作建议 |")
        watchlist_lines.append("| --- | --- | --- | --- |")
        for wr in watchlist_rows:
            core = wr['core'].rstrip("。")
            if len(core) > 40:
                core = core[:38] + "…"
            action = wr['action']
            if len(action) > 40:
                action = action[:38] + "…"
            watchlist_lines.append(
                f"| {wr['name']} | {wr['date']} | {core} | {action} |"
            )
        watchlist_lines.append("")
        # Append raw evidence for watchlist items
        watchlist_lines.append("### 观察清单原文")
        watchlist_lines.append("")
        watchlist_lines.append("<details>")
        watchlist_lines.append(f'<summary>📂 观察清单原文（共 {len(watchlist_rows)} 个品种）</summary>')
        watchlist_lines.append("")
        for wr in watchlist_rows:
            raw = wr.get("raw_text", "").strip()
            if not raw:
                continue
            watchlist_lines.append(f"#### {wr['name']}（{wr['date']}）")
            watchlist_lines.append("")
            watchlist_lines.append(raw)
            watchlist_lines.append("")
        watchlist_lines.append("</details>")
        watchlist_lines.append("")

    # --- Methodology block (LLM-generated) ---
    methodology_lines = ["## 二、方法论总结", ""]
    methodology_md = summarize_methodology_with_llm(vertex_cfg, sections)
    methodology_lines.extend(methodology_md.splitlines())
    methodology_lines.extend(["", "---", ""])

    # --- Summary table: single table with category tag ---
    summary_block: list[str] = []
    summary_block.append("## 一、品种关键位汇总")
    summary_block.append("")
    # Assign signal label and category
    main_names = {
        normalize_instrument_name(i.get("name", ""), i.get("symbol", ""))
        for i in INSTRUMENTS if i.get("id", "") not in WATCHLIST_INSTRUMENTS
    }
    for row in summary_rows:
        if "signal" not in row:
            row["signal"] = "主力" if row["name"] in main_names else "观察"
        if "category" not in row or not row["category"]:
            row["category"] = classify_summary_category(
                str(row.get("name", "")),
                str(row.get("basis", "UNKNOWN")),
                str(row.get("source", "")),
            )

    # Sort: by category order, then by date (newest first)
    cat_order = {c: i for i, c in enumerate(SUMMARY_CATEGORY_ORDER)}
    all_rows = sorted(
        summary_rows,
        key=lambda r: (cat_order.get(r.get("category", ""), 99), summary_row_sort_key(r)),
        reverse=False,
    )
    # Within same category, newest first
    all_rows = sorted(
        all_rows,
        key=lambda r: cat_order.get(r.get("category", ""), 99),
    )

    summary_block.append("| 分类 | 品种 | 当前点位 | 日期 | 最近关键位 | 对应操作 |")
    summary_block.append("| --- | --- | ---: | --- | --- | --- |")
    for row in all_rows:
        cat = row.get("category", "其他")
        summary_block.append(
            f"| {cat} | {row['name']} | {row['current']} | {row['date']} | {row['nearest']} | {row['action']} |"
        )
    summary_block.append("")
    summary_block.append("---")
    summary_block.append("")

    # --- Assemble final report ---
    # Order: title/header → summary → methodology → detailed analysis → watchlist
    lines.extend(summary_block)
    lines.extend(methodology_lines)

    if LLM_FINAL_PROOFREAD:
        if vertex_cfg:
            detail_lines = proofread_detail_sections_with_llm(vertex_cfg, detail_lines)
    detail_lines = dedupe_report_lines_global(detail_lines)
    lines.extend(["## 三、品种详解", ""])
    lines.extend(detail_lines)
    lines.extend(watchlist_lines)

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--charts-dir", required=True, type=Path)
    args = parser.parse_args()

    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-config")
    configure_matplotlib_fonts()

    text = args.input.read_text(encoding="utf-8")
    build_report(text, args.input, args.output, args.charts_dir)
    print(f"Wrote report: {args.output}")
    print(f"Charts dir: {args.charts_dir}")


if __name__ == "__main__":
    main()

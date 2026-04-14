# Quant Investment Juicer - 量化榨汁机

## Project Overview
从微博投资分析师（量化榨汁机, UID 6937480224）抓取内容，结合实时行情数据和技术图表，生成多资产投资分析报告。

## Data Flow
```
Weibo API (ingest_weibo.py) → processed/quant_juicer_weibo_latest.md
  → report_multi_asset.py (LLM提取 + 行情数据 + 图表)
  → output/multi_asset_analysis_YYYY_MM_DD.md + charts/
```

## Directory Structure
- `scripts/ingestion/` — 数据采集（微博抓取、DOCX导入）
- `scripts/report/` — 分析和报告生成
- `scripts/analysis/` — 图表分类和反向工程
- `scripts/analysis/chart_templates/` — 5个参数化图表生成器
- `scripts/automation/` — pipeline 编排和 cookie 刷新
- `processed/` — 输入源和中间数据
- `output/` — 最终报告、图表、价格缓存

## Critical Anti-Patterns

### Weibo 爬虫
- **必须过滤 SSOLoginState cookie**——保留它会触发持久 432 反爬错误
- Cookie 提取用 `refresh_weibo_cookie.py`（Chrome browser_cookie3），不要用 Playwright 登录
- 请求间隔 8-15 秒（RateLimitConfig），不要调小
- Cookie 域名要合并 `.weibo.cn` + `.weibo.com`
- 增量抓取用 `--since`，逻辑是 `<` 不是 `<=`（包含当天帖子）

### 报告生成
- 跨资产污染问题：TOKEN_ALIASES + 贪心评分解决，不要改动评分逻辑
- 允许的跨提及白名单在 TOKEN_CROSS_ALLOWED_ALIASES（如黄金→美元）
- 股票拆分有自动回调逻辑（back_adjust_split_like_gaps），不要手动改历史价格

### 数据源优先级
- A股指数行情：Eastmoney API（默认）
- 美股行情：yfinance（默认）
- 期权数据：yfinance（主）→ NASDAQ API（fallback），因为 NASDAQ 不覆盖 NYSE ETF
- 价格缓存在 `output/price_cache/`，避免重复请求

## API & Auth
- **Vertex AI**: `SA_KEY_PATH` 环境变量 或 `~/.config/secrets/liaoandi_vertex_ai_key.json`
- **Weibo Cookie**: `WEIBO_COOKIE` 环境变量（最高优先级）→ `~/.weibo_cookie_storage/`（crawl4weibo 缓存）
- **Eastmoney**: 免认证，需设 Referer header
- **NASDAQ**: 免认证，需设 User-Agent
- 所有 API key 从 `~/.config/api-keys.env` 读取

## Environment Variables
| Variable | Purpose |
|----------|---------|
| `SA_KEY_PATH` | Vertex AI service account key |
| `WEIBO_COOKIE` | 微博认证 cookie 字符串 |
| `LLM_TIMEOUT_SEC` | Gemini 超时（默认120秒） |
| `LLM_BATCH_SIZE` | LLM 标签提取批大小（默认3） |
| `EMBED_SOURCE_IMAGES` | 报告中嵌入源图片（0/1） |

## Before Running Pipeline
1. 先刷新 cookie：`python scripts/automation/refresh_weibo_cookie.py`
2. 检查 cookie 有效：确认无 SSOLoginState
3. 再运行 `auto_pipeline.py` 或单独的脚本

## File Naming
- 报告：`multi_asset_analysis_YYYY_MM_DD.md`
- 图表目录：`multi_asset_analysis_YYYY_MM_DD_charts/`
- 增量数据：`quant_juicer_weibo_latest.md`
- 抓取状态：`processed/.weibo_fetch_state.json`

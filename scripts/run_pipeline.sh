#!/bin/bash
# 定时 pipeline 入口：刷新 cookie + 加载环境变量 + 运行流水线
# 由 launchd 调用，不经过 shell profile

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python3"
cd "$PROJECT_ROOT" || exit 1

# Step 0: 从 Chrome 提取最新微博 cookie 并更新 api-keys.env
echo "=== Refreshing Weibo cookie from Chrome ==="
$VENV_PYTHON scripts/automation/refresh_weibo_cookie.py 2>&1

# 加载（可能刚更新过的）环境变量
set -a
source "$HOME/.config/api-keys.env"
set +a

# Step 1-4: 运行 pipeline
echo "=== Running pipeline ==="
$VENV_PYTHON scripts/automation/auto_pipeline.py \
    2>&1 | tee -a /tmp/quant_juicer_pipeline.log

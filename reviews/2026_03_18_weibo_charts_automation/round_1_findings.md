## Round 1 Review Findings

### MUST FIX
1. `ingest_weibo.py` — `--since` cutoff uses `<=` instead of `<`, silently drops posts on the cutoff date
2. `auto_pipeline.py` — missing `--append` flag when calling ingest_weibo, overwrites incremental data

### SHOULD FIX
3. `analysis_charts.py` — `run_generated_code()` naive string replacement of "output.png"/"chart.png"
4. `analysis_charts.py` — no SA_KEY_PATH guard before calling `_get_vertex_creds()`
5. `auto_pipeline.py` — chart image paths embedded as absolute paths in MD
6. Stale docstring paths in ingest_weibo.py, analysis_charts.py, candlestick_avwap.py (old script locations)
7. `exponential_fit.py` — x-axis label says "Days Since Start" but formatter shows years

### OBSERVATIONS (no action needed)
- README says "Gemini 3.1 Pro" which matches the code model ID `gemini-3.1-pro-preview`
- No requirements.txt — acceptable for personal project
- Token expiry timezone handling is correct across files

#!/usr/bin/env python3
"""
Analyze images from Weibo posts:
1. Classify each image (news screenshot / app screenshot / code-generated chart)
2. For code-generated charts: reverse-engineer the logic and generate Python code
3. Run the generated code with latest data to produce updated conclusions

Usage:
    python scripts/analyze_charts.py --input processed/quant_juicer_weibo_latest.md
    python scripts/analyze_charts.py --image path/to/single_chart.png
"""

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent
SA_KEY_PATH = Path(os.getenv(
    "SA_KEY_PATH",
    str(Path.home() / "Desktop/liaoandi-vertex-ai-key.json"),
))
DEFAULT_MODEL = "gemini-3.1-pro-preview"
VERTEX_LOCATION = "global"
OUTPUT_DIR = BASE_DIR / "output" / "chart_analysis"

_VERTEX_CREDS = None


# ---------------------------------------------------------------------------
# Vertex AI auth (shared with fetch_weibo.py)
# ---------------------------------------------------------------------------
def _detect_project(creds_path: str) -> str:
    try:
        return json.loads(Path(creds_path).read_text()).get("project_id", "")
    except Exception:
        return ""


def _get_vertex_creds():
    global _VERTEX_CREDS
    if _VERTEX_CREDS is None:
        _VERTEX_CREDS = service_account.Credentials.from_service_account_file(
            str(SA_KEY_PATH), scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _VERTEX_CREDS.token or not getattr(_VERTEX_CREDS, "expiry", None):
        _VERTEX_CREDS.refresh(Request())
    else:
        from datetime import timezone
        expiry = _VERTEX_CREDS.expiry
        now = datetime.now() if getattr(expiry, "tzinfo", None) is None else datetime.now(timezone.utc)
        if (expiry - now).total_seconds() < 120:
            _VERTEX_CREDS.refresh(Request())
    return _VERTEX_CREDS


def gemini_vision(prompt: str, image_path: str, max_tokens: int = 4096, temperature: float = 0.1) -> str:
    """Call Gemini with an image + text prompt."""
    creds = _get_vertex_creds()
    project = _detect_project(str(SA_KEY_PATH))
    url = (
        f"https://aiplatform.googleapis.com/v1/"
        f"projects/{project}/locations/{VERTEX_LOCATION}/publishers/google/models/{DEFAULT_MODEL}:generateContent"
    )

    # Encode image
    img_bytes = Path(image_path).read_bytes()
    b64 = base64.b64encode(img_bytes).decode()
    mime = "image/jpeg" if image_path.lower().endswith((".jpg", ".jpeg")) else "image/png"

    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inlineData": {"mimeType": mime, "data": b64}},
                {"text": prompt},
            ],
        }],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=(15, 120))
    if resp.status_code != 200:
        # If thinkingConfig not supported, retry without it
        if resp.status_code == 400 and "thinking" in resp.text.lower():
            payload["generationConfig"].pop("thinkingConfig", None)
            resp = requests.post(url, headers=headers, json=payload, timeout=(15, 120))
        if resp.status_code != 200:
            raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    # Filter out thinking parts — only keep parts with text and no thoughtSignature
    texts = []
    for p in parts:
        if isinstance(p, dict) and p.get("text") and "thoughtSignature" not in p:
            texts.append(p["text"])
    return " ".join(texts).strip()


# ---------------------------------------------------------------------------
# Step 1: Classify image
# ---------------------------------------------------------------------------
CLASSIFY_PROMPT = """\
Please classify this image into exactly ONE of these categories:

1. "news_screenshot" — a screenshot of a news article, social media post, or text-heavy content
2. "app_screenshot" — a screenshot from a financial app or website showing fund performance, \
stock quotes, tables, or UI elements (e.g. fund comparison charts from Tiantian Fund, East Money, etc.)
3. "technical_chart" — a programmatically generated chart for technical/quantitative analysis \
(e.g. candlestick charts, gamma exposure profiles, options flow charts, indicator plots, \
price charts with overlays, heatmaps generated from data)

Rules:
- If the image has clear UI chrome (app bars, buttons, watermarks from financial apps), it's "app_screenshot"
- If it's mostly text/news, it's "news_screenshot"
- Only classify as "technical_chart" if it looks like it was generated by code (matplotlib, plotly, \
trading platforms like TradingView with clean programmatic output)

Respond with ONLY the category name, nothing else."""


def classify_image(image_path: str) -> str:
    """Classify an image into: news_screenshot, app_screenshot, technical_chart."""
    result = gemini_vision(CLASSIFY_PROMPT, image_path, max_tokens=20, temperature=0.0)
    result = result.strip().strip('"').strip("'").lower()
    for cat in ("technical_chart", "app_screenshot", "news_screenshot"):
        if cat in result:
            return cat
    return "unknown"


# ---------------------------------------------------------------------------
# Step 2: Analyze technical chart and generate code
# ---------------------------------------------------------------------------
ANALYZE_PROMPT = """\
You are a quantitative analyst. Analyze this technical/financial chart image and:

1. **Describe** what the chart shows:
   - Chart type (candlestick, line, area, bar, heatmap, scatter, etc.)
   - What data is plotted (price, volume, gamma exposure, options OI, etc.)
   - X-axis and Y-axis meanings and ranges
   - Any overlays, annotations, key levels, or indicators visible
   - Time period covered
   - The underlying asset or instrument

2. **Identify data sources**: What data would be needed to reproduce this chart?
   (e.g., OHLCV price data, options chain data, gamma exposure calculations)

3. **Generate Python code** that reproduces a similar chart using matplotlib.
   Requirements:
   - Use real, fetchable data sources (yfinance, Yahoo Finance API, or similar free APIs)
   - Include all necessary imports
   - The code should be self-contained and runnable
   - Use the EXACT same visual style where possible (colors, annotations, layout)
   - Add comments explaining the analysis logic
   - At the end, print key findings/conclusions based on the latest data
   - Save the chart to a file (use argparse or a hardcoded path)

Respond in this JSON format:
```json
{
  "description": "Brief description of what the chart shows",
  "asset": "The asset/instrument (e.g. KWEB, GLD, SPY)",
  "chart_type": "e.g. gamma_exposure, candlestick, options_flow",
  "data_sources": ["list of data needed"],
  "python_code": "full runnable Python code as a string",
  "key_observations": ["observation 1", "observation 2"]
}
```

IMPORTANT: Return ONLY valid JSON, no markdown fences, no extra text."""


def analyze_chart(image_path: str) -> dict:
    """Analyze a technical chart and return structured analysis with code."""
    result = gemini_vision(ANALYZE_PROMPT, image_path, max_tokens=8192, temperature=0.1)

    # Try to parse JSON
    # Strip markdown code fences if present
    result = re.sub(r"^```json\s*", "", result.strip())
    result = re.sub(r"\s*```$", "", result.strip())

    try:
        return json.loads(result)
    except json.JSONDecodeError:
        # Try to extract JSON from response
        match = re.search(r"\{[\s\S]*\}", result)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {
            "description": "Failed to parse LLM response",
            "raw_response": result[:2000],
            "python_code": "",
        }


# ---------------------------------------------------------------------------
# Step 3: Run generated code
# ---------------------------------------------------------------------------
def run_generated_code(code: str, output_dir: Path, chart_name: str) -> dict:
    """Execute the generated Python code and capture results."""
    import subprocess
    import tempfile

    output_dir.mkdir(parents=True, exist_ok=True)
    code_path = output_dir / f"{chart_name}.py"
    chart_path = output_dir / f"{chart_name}.png"

    # Inject output path into code
    code = code.replace("output.png", str(chart_path))
    code = code.replace("chart.png", str(chart_path))
    # Add savefig if not present
    if "savefig" not in code and "plt.show" in code:
        code = code.replace("plt.show()", f'plt.savefig("{chart_path}", dpi=150, bbox_inches="tight")\nplt.show()')

    code_path.write_text(code)

    try:
        result = subprocess.run(
            [sys.executable, str(code_path)],
            capture_output=True, text=True, timeout=60,
            cwd=str(BASE_DIR),
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-1000:] if result.stderr else "",
            "code_path": str(code_path),
            "chart_path": str(chart_path) if chart_path.exists() else None,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stderr": "Timeout (60s)", "code_path": str(code_path)}
    except Exception as e:
        return {"success": False, "stderr": str(e), "code_path": str(code_path)}


# ---------------------------------------------------------------------------
# Pipeline: process images from markdown
# ---------------------------------------------------------------------------
def extract_images_from_md(md_path: Path) -> list[dict]:
    """Extract image paths and their context from markdown."""
    md_dir = md_path.parent
    text = md_path.read_text()

    images = []
    # Known path mappings for mismatched directory names
    path_aliases = {
        "量化投资榨汁机_new_assets": "quant_juicer_weibo_assets",
    }

    # Find all image references
    for match in re.finditer(r"!\[.*?\]\(([^)]+)\)", text):
        rel_path = match.group(1)
        abs_path = (md_dir / rel_path).resolve()

        # Try alias if path doesn't exist
        if not abs_path.exists():
            for old_dir, new_dir in path_aliases.items():
                if old_dir in rel_path:
                    alt_rel = rel_path.replace(old_dir, new_dir)
                    alt_abs = (md_dir / alt_rel).resolve()
                    if alt_abs.exists():
                        abs_path = alt_abs
                        rel_path = alt_rel
                        break

        if abs_path.exists():
            # Get surrounding context (nearby text)
            start = max(0, match.start() - 500)
            end = min(len(text), match.end() + 200)
            context = text[start:end].strip()
            images.append({
                "path": str(abs_path),
                "rel_path": rel_path,
                "context": context,
            })
    return images


def process_pipeline(images: list[dict], output_dir: Path):
    """Full pipeline: classify -> analyze -> generate code -> run."""
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    total = len(images)
    technical_count = 0

    for i, img in enumerate(images):
        img_path = img["path"]
        img_name = Path(img_path).stem
        print(f"\n[{i+1}/{total}] {Path(img_path).name}")

        # Step 1: Classify
        category = classify_image(img_path)
        print(f"  Category: {category}")

        if category != "technical_chart":
            results.append({
                "image": img["rel_path"],
                "category": category,
                "skipped": True,
            })
            continue

        technical_count += 1

        # Step 2: Analyze and generate code
        print(f"  Analyzing chart and generating code...")
        analysis = analyze_chart(img_path)
        print(f"  Asset: {analysis.get('asset', '?')}")
        print(f"  Type: {analysis.get('chart_type', '?')}")
        print(f"  Description: {analysis.get('description', '?')[:80]}")

        code = analysis.get("python_code", "")
        run_result = None

        if code:
            # Step 3: Run the code
            print(f"  Running generated code...")
            chart_name = f"chart_{i+1:02d}_{analysis.get('asset', 'unknown').replace(' ', '_')}"
            run_result = run_generated_code(code, output_dir, chart_name)

            if run_result["success"]:
                print(f"  Code ran successfully!")
                if run_result.get("stdout"):
                    print(f"  Output: {run_result['stdout'][:200]}")
            else:
                print(f"  Code failed: {run_result.get('stderr', '')[:200]}")
        else:
            print(f"  No code generated")

        results.append({
            "image": img["rel_path"],
            "category": category,
            "analysis": analysis,
            "run_result": run_result,
        })

        # Progress
        if total >= 5 and (i + 1) % max(1, total // 5) == 0:
            print(f"\n[progress] {i+1}/{total} images processed, {technical_count} technical charts found")

    # Write results
    results_path = output_dir / "analysis_results.json"
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    # Summary
    print(f"\n--- Summary ---")
    print(f"Total images: {total}")
    print(f"Technical charts: {technical_count}")
    print(f"Skipped (screenshots): {total - technical_count}")
    succeeded = sum(1 for r in results if r.get("run_result", {}).get("success"))
    print(f"Code ran successfully: {succeeded}/{technical_count}")
    print(f"Results: {results_path}")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Analyze technical charts from Weibo posts")
    parser.add_argument("--input", type=Path, help="Input markdown file with image references")
    parser.add_argument("--image", type=Path, help="Analyze a single image file")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="Output directory")
    parser.add_argument("--classify-only", action="store_true", help="Only classify, skip analysis")
    args = parser.parse_args()

    if args.image:
        # Single image mode
        print(f"Classifying: {args.image}")
        cat = classify_image(str(args.image))
        print(f"Category: {cat}")
        if cat == "technical_chart" and not args.classify_only:
            print("Analyzing...")
            analysis = analyze_chart(str(args.image))
            print(json.dumps(analysis, ensure_ascii=False, indent=2))
            if analysis.get("python_code"):
                print("\nRunning generated code...")
                result = run_generated_code(
                    analysis["python_code"], args.output_dir,
                    args.image.stem,
                )
                if result["success"]:
                    print(f"Success! Chart saved to: {result.get('chart_path')}")
                    if result.get("stdout"):
                        print(f"Output:\n{result['stdout']}")
                else:
                    print(f"Failed: {result.get('stderr', '')[:500]}")
        return

    if args.input:
        # Markdown mode — process all images
        images = extract_images_from_md(args.input)
        if not images:
            print("[info] No images found in markdown file")
            return
        print(f"Found {len(images)} images in {args.input}")

        if args.classify_only:
            for img in images:
                cat = classify_image(img["path"])
                print(f"  {Path(img['path']).name}: {cat}")
            return

        process_pipeline(images, args.output_dir)
        return

    parser.print_help()


if __name__ == "__main__":
    main()

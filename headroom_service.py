#!/usr/bin/env python3
"""
Headroom Compression Microservice & Proxy Backend
Integrates https://github.com/headroomlabs-ai/headroom (headroom-ai)
Runs the official Headroom TransformPipeline + ContentRouter (_no_ccr_pipeline / _ccr_pipeline)
identically to `headroom proxy --port 8787` (headroom/proxy/handlers/openai.py).
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
import re
import time
from typing import Any

# Configure environment for fast local offline execution without network hangs
os.environ.setdefault("HEADROOM_OFFLINE", "1")
os.environ.setdefault("HEADROOM_TIKTOKEN_LOAD_TIMEOUT_SECONDS", "0.001")
os.environ.setdefault("HEADROOM_DETECT_BACKEND", "python")
os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("HEADROOM_BEACON", "off")

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

import headroom
from headroom.transforms.code_compressor import CodeAwareCompressor, detect_language
from headroom.transforms.content_detector import detect_content_type
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig
from headroom.transforms.diff_compressor import DiffCompressor
from headroom.transforms.log_compressor import LogCompressor
from headroom.transforms.pipeline import TransformPipeline
from headroom.transforms.search_compressor import SearchCompressor
from headroom.transforms.smart_crusher import SmartCrusher
from headroom.transforms.text_crusher import TextCrusher, TextCrusherConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("headroom-service")

START_TIME = time.time()
CCR_HASH_RE = re.compile(r"hash=([0-9a-fA-F]{12,64})")

# In-memory CCR store, LRU text-compression cache (for single-text UI fallback), & stats tracker
CCR_STORE: dict[str, dict[str, Any]] = {}
TEXT_CACHE: OrderedDict[str, tuple[str, str | None, int, int]] = OrderedDict()
TEXT_CACHE_MAX = 2048

STATS: dict[str, Any] = {
    "total_requests": 0,
    "total_tokens_before": 0,
    "total_tokens_after": 0,
    "total_tokens_saved": 0,
    "recent_requests": [],
    "feed": [],
}

# Official Headroom /v1/compress pipelines (matching headroom/proxy/server.py & headroom/proxy/handlers/openai.py):
# 1. _NO_CCR_PIPELINE: default marker-free pipeline for gateways like techwiz37/routermod & LiteLLM
_NO_CCR_PIPELINE = TransformPipeline(
    transforms=[
        ContentRouter(
            ContentRouterConfig(
                ccr_inject_marker=False,
                ccr_enabled=False,
                enable_code_aware=True,
                prefer_code_aware_for_code=True,
                enable_kompress=False,
                protect_recent_reads_fraction=0.3,
                search_group_by_file=True,
                smart_crusher_with_compaction=True,
                result_cache_disk=True,
            )
        )
    ],
    provider=None,
)

# 2. _CCR_PIPELINE: marker-enabled pipeline when config.mode == "ccr" or single-text UI test
_CCR_PIPELINE = TransformPipeline(
    transforms=[
        ContentRouter(
            ContentRouterConfig(
                ccr_inject_marker=True,
                ccr_enabled=True,
                enable_code_aware=True,
                prefer_code_aware_for_code=True,
                enable_kompress=False,
                protect_recent_reads_fraction=0.3,
                search_group_by_file=True,
                smart_crusher_with_compaction=True,
                result_cache_disk=True,
            )
        )
    ],
    provider=None,
)

# Reusable native Rust & AST compressor instances (used when user selects a manual strategy in /compress UI)
_log_compressor = LogCompressor()
_diff_compressor = DiffCompressor()
_search_compressor = SearchCompressor()
_code_compressor = CodeAwareCompressor()
_smart_crusher = SmartCrusher()
_text_crusher = TextCrusher()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Pre-warm the official TransformPipeline + ContentRouter (including SmartCrusher BM25/HybridScorer,
    LogCompressor, MixedContent, and tokenizers) at startup so the first request takes ~50ms instead of ~3.4s.
    """
    try:
        t0 = time.time()
        w_json = json.dumps([{"id": j, "node": f"srv-{j}", "status": "ok"} for j in range(30)], indent=2)
        w_log = (
            "\n".join(f"2026-11-18 10:00:{j%60:02d} INFO [worker] warmup {j}" for j in range(50))
            + "\n2026-11-18 10:01:00 ERROR warmup err"
        )
        w_mixed = "Cluster status:\n" + w_json + "\nEnd of report."
        for pipe in (_NO_CCR_PIPELINE, _CCR_PIPELINE):
            pipe.apply(
                messages=[
                    {"role": "tool", "tool_call_id": "w1", "content": w_json},
                    {"role": "tool", "tool_call_id": "w2", "content": w_log},
                    {"role": "tool", "tool_call_id": "w3", "content": w_mixed},
                ],
                model="gpt-4o",
                model_limit=200000,
            )
        _text_crusher.compress("warmup line 1\n" * 30)
        logger.info("Official Headroom TransformPipeline pre-warmed in %.3fs", time.time() - t0)
    except Exception as exc:
        logger.warning("Warm-up warning: %s", exc)
    yield


app = FastAPI(
    title="Headroom Compression Service",
    description="Context optimization and compression layer for LLM prompts, logs, JSON, code, and files.",
    version=getattr(headroom, "__version__", "0.44.0"),
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1024)


def _fast_count_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _compress_single_text(
    text: str,
    strategy: str = "auto",
    target_ratio: float | None = None,
) -> tuple[str, str | None]:
    """Direct compressor for manual strategy selection in the /compress Web UI."""
    if not text or not isinstance(text, str) or len(text.strip()) < 40:
        return text, None

    chosen = strategy.lower().strip() if strategy else "auto"

    if chosen == "auto":
        sample = text[:12288]
        det = detect_content_type(sample)
        ct_val = getattr(det.content_type, "value", str(det.content_type)).lower()
        if "json" in ct_val:
            chosen = "smart_crusher"
        elif "diff" in ct_val:
            chosen = "diff"
        elif "search" in ct_val:
            chosen = "search"
        elif ct_val in ("build", "build_output", "test_output", "stack_trace", "log"):
            chosen = "log"
        elif "code" in ct_val:
            chosen = "code"
        else:
            _, conf = detect_language(sample)
            chosen = "code" if conf >= 0.6 else "text"

    try:
        if chosen in ("smart_crusher", "json"):
            res = _smart_crusher.crush(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"router:smart_crusher:{len(res.compressed)/len(text):.2f}"
        elif chosen in ("log", "logs", "build"):
            res = _log_compressor.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                if getattr(res, "cache_key", None):
                    CCR_STORE[res.cache_key] = {
                        "hash": res.cache_key,
                        "original": text,
                        "strategy": "log",
                        "timestamp": time.time(),
                    }
                return res.compressed, f"router:log:{len(res.compressed)/len(text):.2f}"
        elif chosen in ("diff", "git_diff"):
            res = _diff_compressor.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"router:diff:{len(res.compressed)/len(text):.2f}"
        elif chosen in ("search", "grep"):
            res = _search_compressor.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"router:search:{len(res.compressed)/len(text):.2f}"
        elif chosen in ("code", "ast"):
            res = _code_compressor.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"router:code_aware:{len(res.compressed)/len(text):.2f}"
            tc_res = _text_crusher.compress(text)
            if tc_res and tc_res.compressed and len(tc_res.compressed) < len(text):
                return tc_res.compressed, f"router:text:{len(tc_res.compressed)/len(text):.2f}"
        elif chosen in ("text", "text_crusher"):
            if target_ratio and 0.05 <= float(target_ratio) <= 0.95:
                tc = TextCrusher(TextCrusherConfig(max_token_ratio=float(target_ratio)))
                res = tc.compress(text)
            else:
                res = _text_crusher.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"router:text:{len(res.compressed)/len(text):.2f}"
    except Exception as exc:
        logger.warning("Compressor (%s) warning: %s", chosen, exc)

    return text, None


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "service": "headroom-proxy",
        "status": "healthy",
        "ready": True,
        "version": getattr(headroom, "__version__", "0.44.0"),
        "uptime_seconds": round(time.time() - START_TIME, 2),
        "ccr_entries": len(CCR_STORE),
    }


@app.get("/stats")
async def stats() -> dict[str, Any]:
    tb = STATS["total_tokens_before"]
    ta = STATS["total_tokens_after"]
    ts = STATS["total_tokens_saved"]
    avg_pct = round((ts / tb) * 100, 2) if tb > 0 else 0.0
    return {
        **STATS,
        "average_savings_pct": avg_pct,
        "tokens": {
            "before": tb,
            "after": ta,
            "saved": ts,
            "savings_percent": avg_pct,
        },
        "requests": {
            "total": STATS["total_requests"],
            "compressed": sum(1 for r in STATS["recent_requests"] if r.get("tokens_saved", 0) > 0),
        },
        "version": getattr(headroom, "__version__", "0.44.0"),
        "uptime_seconds": round(time.time() - START_TIME, 2),
    }


@app.get("/stats-history")
async def stats_history() -> dict[str, Any]:
    return {
        "series": STATS["recent_requests"],
        "total_requests": STATS["total_requests"],
        "total_tokens_saved": STATS["total_tokens_saved"],
    }


@app.get("/stats-lifetime")
async def stats_lifetime() -> dict[str, Any]:
    return await stats()


@app.get("/transformations/feed")
async def transformations_feed() -> dict[str, Any]:
    return {
        "items": STATS["feed"],
        "total": len(STATS["feed"]),
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Lightweight live Headroom Dashboard compatible with routermod's /api/headroom/proxy/dashboard."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Headroom Proxy Dashboard</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; background: #0b0f19; color: #f3f4f6; margin: 0; padding: 32px; }
    .wrap { max-width: 920px; margin: 0 auto; }
    h1 { color: #00f0b5; margin-bottom: 8px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin: 24px 0; }
    .card { background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 18px; }
    .label { font-size: 12px; color: #9ca3af; text-transform: uppercase; }
    .val { font-size: 28px; font-weight: 700; margin-top: 6px; color: #fff; }
    .val.green { color: #00f0b5; }
    table { width: 100%; border-collapse: collapse; background: #111827; border-radius: 12px; overflow: hidden; }
    th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #1f2937; font-size: 14px; }
    th { background: #1f2937; color: #9ca3af; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>⚡ Headroom Compression Proxy Dashboard</h1>
    <p style="color:#9ca3af">Connected to 9Router / routermod &amp; /compress API</p>
    <div class="cards">
      <div class="card"><div class="label">Total Requests</div><div class="val" id="req">0</div></div>
      <div class="card"><div class="label">Tokens Before</div><div class="val" id="tb">0</div></div>
      <div class="card"><div class="label">Tokens Saved</div><div class="val green" id="ts">0</div></div>
      <div class="card"><div class="label">Avg Savings</div><div class="val green" id="pct">0%</div></div>
    </div>
    <h3>Recent Compression Requests</h3>
    <table>
      <thead><tr><th>Time</th><th>Model</th><th>Before</th><th>After</th><th>Saved</th><th>Transforms</th><th>Latency</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <script>
    async function load() {
      try {
        const res = await fetch('/stats');
        const d = await res.json();
        document.getElementById('req').textContent = d.total_requests || 0;
        document.getElementById('tb').textContent = (d.total_tokens_before || 0).toLocaleString();
        document.getElementById('ts').textContent = (d.total_tokens_saved || 0).toLocaleString();
        document.getElementById('pct').textContent = (d.average_savings_pct || 0) + '%';
        const rows = (d.recent_requests || []).map(r =>
          `<tr><td>${r.timestamp}</td><td>${r.model}</td><td>${r.tokens_before}</td><td>${r.tokens_after}</td><td style="color:#00f0b5">${r.tokens_saved} (${r.savings_percentage}%)</td><td>${(r.transforms_applied || []).slice(0, 4).join(', ')}</td><td>${r.latency_ms}ms</td></tr>`
        ).join('');
        document.getElementById('rows').innerHTML = rows || '<tr><td colspan="7" style="text-align:center;color:#9ca3af">No requests yet</td></tr>';
      } catch (e) {}
    }
    load();
    setInterval(load, 3000);
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/v1/retrieve")
async def retrieve(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    hash_key = body.get("hash") or body.get("key") or body.get("cache_key")
    if not hash_key:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request", "message": "Missing required field: hash"}},
        )

    entry = CCR_STORE.get(hash_key)
    if not entry:
        matches = [v for k, v in CCR_STORE.items() if k.startswith(str(hash_key))]
        if len(matches) == 1:
            entry = matches[0]

    if not entry:
        try:
            from headroom.ccr import get_ccr_store

            store = get_ccr_store()
            item = store.get(hash_key)
            if item is not None:
                content = getattr(item, "original_content", None) or getattr(item, "content", str(item))
                return JSONResponse({"hash": hash_key, "content": content, "found": True})
        except Exception:
            pass

        return JSONResponse(
            status_code=404,
            content={"error": {"type": "not_found", "message": f"No cached entry found for hash={hash_key}"}},
        )

    return JSONResponse(
        {
            "hash": entry["hash"],
            "content": entry["original"],
            "strategy": entry.get("strategy"),
            "timestamp": entry.get("timestamp"),
            "found": True,
        }
    )


@app.post("/v1/compress")
async def handle_compress(request: Request) -> JSONResponse:
    """
    Executes the official Headroom TransformPipeline + ContentRouter (_NO_CCR_PIPELINE / _CCR_PIPELINE),
    matching headroom/proxy/handlers/openai.py (`handle_compress`) 1:1.
    """
    t0 = time.time()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request", "message": "Invalid JSON in request body."}},
        )

    messages = body.get("messages")
    if messages is None and isinstance(body.get("input"), list):
        messages = body.get("input")
    model = body.get("model") or "gpt-4o"
    token_budget = body.get("token_budget")
    req_config = body.get("config") or {}
    if not isinstance(req_config, dict):
        req_config = {}

    if messages is None or not isinstance(messages, list):
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request", "message": "Missing required field: messages (must be a list)"}},
        )

    if len(messages) == 0:
        return JSONResponse(
            {
                "messages": [],
                "tokens_before": 0,
                "tokens_after": 0,
                "tokens_saved": 0,
                "compression_ratio": 1.0,
                "savings_percentage": 0.0,
                "transforms_applied": [],
                "ccr_hashes": [],
            }
        )

    strategy = req_config.get("strategy", "auto")
    forced_strategy = strategy and strategy.lower() not in ("auto", "default", "router")
    compress_user_messages = bool(req_config.get("compress_user_messages", len(messages) == 1))
    target_ratio = req_config.get("target_ratio")
    protect_recent = req_config.get("protect_recent")
    protect_analysis_context = req_config.get("protect_analysis_context")
    frozen_message_count = req_config.get("frozen_message_count")
    mode = req_config.get("mode")

    context_limit = int(token_budget) if token_budget and isinstance(token_budget, int) and token_budget > 0 else 200000

    # Select official pipeline:
    # - For single-message UI test or mode="ccr", use _CCR_PIPELINE
    # - For gateway/routermod traffic, use _NO_CCR_PIPELINE (exact default of `headroom proxy` /v1/compress)
    pipeline = _CCR_PIPELINE if (mode == "ccr" or len(messages) == 1) else _NO_CCR_PIPELINE

    pipeline_kwargs: dict[str, Any] = {
        "model_limit": context_limit,
        "compress_user_messages": compress_user_messages,
    }
    if target_ratio is not None:
        pipeline_kwargs["target_ratio"] = float(target_ratio)
    if protect_recent is not None:
        pipeline_kwargs["protect_recent"] = int(protect_recent)
    elif len(messages) == 1:
        pipeline_kwargs["protect_recent"] = 0
    if protect_analysis_context is not None:
        pipeline_kwargs["protect_analysis_context"] = bool(protect_analysis_context)
    elif len(messages) == 1:
        pipeline_kwargs["protect_analysis_context"] = False
    if isinstance(frozen_message_count, int) and frozen_message_count >= 0:
        pipeline_kwargs["frozen_message_count"] = frozen_message_count

    if not forced_strategy:
        result = pipeline.apply(messages=messages, model=str(model), **pipeline_kwargs)
        compressed_messages = result.messages
        tokens_before = result.tokens_before
        tokens_after = result.tokens_after
        transforms_applied = list(result.transforms_applied or ())
    else:
        compressed_messages = [dict(m) if isinstance(m, dict) else m for m in messages]
        tokens_before = sum(
            _fast_count_tokens(m.get("content", "")) + 4
            for m in messages
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        )
        tokens_after = tokens_before
        transforms_applied = []

    # Only if the user explicitly picked a manual strategy in the /compress UI (or single-text UI fallback)
    if forced_strategy or (len(messages) == 1 and tokens_after >= tokens_before):
        if len(compressed_messages) == 1 and isinstance(compressed_messages[0], dict):
            orig_c = messages[0].get("content") if isinstance(messages[0], dict) else None
            if isinstance(orig_c, str) and len(orig_c) >= 40:
                new_text, label = _compress_single_text(
                    orig_c,
                    strategy=strategy if forced_strategy else "auto",
                    target_ratio=float(target_ratio) if target_ratio is not None else None,
                )
                if label and new_text != orig_c and len(new_text) < len(orig_c):
                    orig_tok = _fast_count_tokens(orig_c)
                    new_tok = _fast_count_tokens(new_text)
                    if new_tok < orig_tok:
                        compressed_messages = [{**compressed_messages[0], "content": new_text}]
                        tokens_before = orig_tok + 4
                        tokens_after = new_tok + 4
                        transforms_applied = [label]

    tokens_saved = max(0, tokens_before - tokens_after)
    if tokens_saved == 0:
        tokens_after = tokens_before

    proxy_compression_ratio = round(tokens_after / tokens_before, 4) if tokens_before > 0 else 1.0
    savings_percentage = round((tokens_saved / tokens_before) * 100, 2) if tokens_before > 0 else 0.0

    ccr_hashes: list[str] = []
    for idx, m in enumerate(compressed_messages):
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str) and "hash=" in c:
            for h in CCR_HASH_RE.findall(c):
                if h not in ccr_hashes:
                    ccr_hashes.append(h)
                if h not in CCR_STORE and idx < len(messages) and isinstance(messages[idx], dict):
                    orig_c = messages[idx].get("content")
                    if isinstance(orig_c, str):
                        CCR_STORE[h] = {
                            "hash": h,
                            "original": orig_c,
                            "strategy": ",".join(transforms_applied[:5]),
                            "timestamp": time.time(),
                        }

    latency_ms = round((time.time() - t0) * 1000, 2)
    req_entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "tokens_saved": tokens_saved,
        "savings_percentage": savings_percentage,
        "transforms_applied": transforms_applied,
        "latency_ms": latency_ms,
    }

    STATS["total_requests"] += 1
    STATS["total_tokens_before"] += tokens_before
    STATS["total_tokens_after"] += tokens_after
    STATS["total_tokens_saved"] += tokens_saved
    STATS["recent_requests"] = ([req_entry] + STATS["recent_requests"])[:30]
    STATS["feed"] = ([req_entry] + STATS["feed"])[:50]

    return JSONResponse(
        {
            "messages": compressed_messages,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "tokens_saved": tokens_saved,
            "compression_ratio": proxy_compression_ratio,
            "savings_percentage": savings_percentage,
            "transforms_applied": transforms_applied,
            "ccr_hashes": ccr_hashes,
            "model": model,
            "latency_ms": latency_ms,
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("HEADROOM_PORT", "8787"))
    host = os.environ.get("HEADROOM_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")

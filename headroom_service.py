#!/usr/bin/env python3
"""
Headroom Compression Microservice & Proxy Backend
Integrates https://github.com/headroomlabs-ai/headroom (headroom-ai)
High-throughput, low-latency engine optimized for cloud gateways (techwiz37/routermod / 9Router)
and Koyeb deployments (<50ms warm latency on 200K+ token payloads).
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
from headroom import CompressConfig, compress
from headroom.transforms.code_compressor import CodeAwareCompressor, detect_language
from headroom.transforms.content_detector import detect_content_type
from headroom.transforms.diff_compressor import DiffCompressor
from headroom.transforms.log_compressor import LogCompressor
from headroom.transforms.search_compressor import SearchCompressor
from headroom.transforms.smart_crusher import SmartCrusher
from headroom.transforms.text_crusher import TextCrusher, TextCrusherConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("headroom-service")

START_TIME = time.time()
CCR_HASH_RE = re.compile(r"hash=([0-9a-fA-F]{12,64})")

# Time budget (seconds) per request so even a cold 500K-token request finishes well under routermod's 3000ms timeout
REQUEST_TIME_BUDGET_SEC = float(os.environ.get("HEADROOM_REQUEST_BUDGET_SEC", "1.4"))

# In-memory CCR store, LRU text-compression cache, & stats tracker
CCR_STORE: dict[str, dict[str, Any]] = {}
# Key: blake2b(text) + strategy + ratio -> (compressed_text, transform_label, orig_tokens, new_tokens)
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

# Reusable native Rust & AST compressor instances from headroom-ai
_log_compressor = LogCompressor()
_diff_compressor = DiffCompressor()
_search_compressor = SearchCompressor()
_code_compressor = CodeAwareCompressor()
_smart_crusher = SmartCrusher()
_text_crusher = TextCrusher()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-warm Headroom native Rust compressors on startup so first routermod request is instant."""
    try:
        t0 = time.time()
        _text_crusher.compress("warmup line 1\n" * 30)
        _log_compressor.compress("2026-11-18 INFO warmup 1\n" * 30 + "2026-11-18 FATAL err\n")
        _smart_crusher.crush(json.dumps([{"id": i, "v": "ok"} for i in range(20)]))
        logger.info("Headroom native compressors pre-warmed in %.3fs", time.time() - t0)
    except Exception as exc:
        logger.warning("Warm-up warning: %s", exc)
    yield


app = FastAPI(
    title="Headroom Compression Service",
    description="Context optimization and compression layer for LLM prompts, logs, JSON, code, and files.",
    version=getattr(headroom, "__version__", "0.44.0"),
    lifespan=lifespan,
)

# Compress large HTTP responses (>1KB) when client supports gzip
app.add_middleware(GZipMiddleware, minimum_size=1024)


def _fast_count_tokens(text: str) -> int:
    """
    Ultra-fast O(1) token estimator (~3.8 chars/token for code/JSON/logs).
    Avoids 1500ms+ Python BPE tokenization overhead on 200K+ token payloads on shared vCPUs.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _compress_single_text(
    text: str,
    strategy: str = "auto",
    target_ratio: float | None = None,
) -> tuple[str, str | None]:
    """
    Compress a single string using Headroom's ContentRouter and native Rust transforms
    (SmartCrusher, LogCompressor, DiffCompressor, SearchCompressor, TextCrusher, CodeAwareCompressor).
    Guarantees len(compressed_text) < len(text) when a transform is returned.
    """
    if not text or not isinstance(text, str) or len(text.strip()) < 60:
        return text, None

    chosen = strategy.lower().strip() if strategy else "auto"

    # 1. Fast JSON detection
    if chosen == "auto":
        stripped = text.strip()
        if (stripped.startswith("[") and stripped.endswith("]")) or (
            stripped.startswith("{") and stripped.endswith("}")
        ):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, (list, dict)):
                    chosen = "smart_crusher"
            except Exception:
                pass

    # 2. Fast content-type detection on a 12KB prefix sample
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
        elif "code" in ct_val and len(text) <= 8000:
            chosen = "code"
        else:
            if len(text) <= 8000:
                _, conf = detect_language(sample)
                chosen = "code" if conf >= 0.65 else "text"
            else:
                # For >8KB blocks in auto mode, use native Rust TextCrusher (<5ms)
                chosen = "text"

    try:
        if chosen in ("smart_crusher", "json"):
            res = _smart_crusher.crush(text)
            if res and res.compressed and len(res.compressed) < len(text):
                ratio = len(res.compressed) / len(text)
                return res.compressed, f"router:smart_crusher:{ratio:.2f}"
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
                ratio = len(res.compressed) / len(text)
                return res.compressed, f"router:log:{ratio:.2f}"
        elif chosen in ("diff", "git_diff"):
            res = _diff_compressor.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                ratio = len(res.compressed) / len(text)
                return res.compressed, f"router:diff:{ratio:.2f}"
        elif chosen in ("search", "grep"):
            res = _search_compressor.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                ratio = len(res.compressed) / len(text)
                return res.compressed, f"router:search:{ratio:.2f}"
        elif chosen in ("code", "ast"):
            if len(text) <= 20000:
                res = _code_compressor.compress(text)
                if res and res.compressed and len(res.compressed) < len(text):
                    ratio = len(res.compressed) / len(text)
                    return res.compressed, f"headroom:code_aware:{ratio:.2f}"
            tc_res = _text_crusher.compress(text)
            if tc_res and tc_res.compressed and len(tc_res.compressed) < len(text):
                ratio = len(tc_res.compressed) / len(text)
                return tc_res.compressed, f"headroom:text_crusher:{ratio:.2f}"
        elif chosen in ("text", "text_crusher"):
            if target_ratio and 0.05 <= float(target_ratio) <= 0.95:
                tc = TextCrusher(TextCrusherConfig(max_token_ratio=float(target_ratio)))
                res = tc.compress(text)
            else:
                res = _text_crusher.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                ratio = len(res.compressed) / len(text)
                return res.compressed, f"headroom:text_crusher:{ratio:.2f}"
    except Exception as exc:
        logger.warning("Compressor (%s) warning: %s", chosen, exc)

    # Fallback: try native Rust TextCrusher if primary strategy didn't shrink the text
    if chosen != "text":
        try:
            res = _text_crusher.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                ratio = len(res.compressed) / len(text)
                return res.compressed, f"headroom:text_crusher:{ratio:.2f}"
        except Exception:
            pass

    return text, None


def _compress_text_cached(
    text: str,
    strategy: str = "auto",
    target_ratio: float | None = None,
    min_tokens: int = 10,
) -> tuple[str, str | None, int, int]:
    """
    Compress a single string with O(1) Blake2b content-addressed caching.
    Returns (compressed_text, transform_label, orig_tokens, new_tokens).
    """
    if not text or not isinstance(text, str):
        return text, None, 0, 0

    orig_tok = _fast_count_tokens(text)
    if orig_tok < min_tokens or len(text.strip()) < 60:
        return text, None, orig_tok, orig_tok

    chosen = strategy.lower().strip() if strategy else "auto"
    ratio_key = f"{float(target_ratio):.2f}" if target_ratio is not None else "default"
    digest = hashlib.blake2b(text.encode("utf-8", errors="ignore"), digest_size=16).hexdigest()
    cache_key = f"{digest}:{chosen}:{ratio_key}"

    cached = TEXT_CACHE.get(cache_key)
    if cached is not None:
        TEXT_CACHE.move_to_end(cache_key)
        return cached

    new_text, label = _compress_single_text(text, strategy=chosen, target_ratio=target_ratio)
    if label and new_text != text and len(new_text) < len(text):
        new_tok = _fast_count_tokens(new_text)
        if new_tok >= orig_tok:
            new_text, label, new_tok = text, None, orig_tok
    else:
        new_text, label, new_tok = text, None, orig_tok

    entry = (new_text, label, orig_tok, new_tok)
    TEXT_CACHE[cache_key] = entry
    if len(TEXT_CACHE) > TEXT_CACHE_MAX:
        TEXT_CACHE.popitem(last=False)

    return entry


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "service": "headroom-proxy",
        "status": "healthy",
        "ready": True,
        "version": getattr(headroom, "__version__", "0.44.0"),
        "uptime_seconds": round(time.time() - START_TIME, 2),
        "ccr_entries": len(CCR_STORE),
        "cache_entries": len(TEXT_CACHE),
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
    compress_user_messages = req_config.get("compress_user_messages", True)
    compress_system_messages = req_config.get("compress_system_messages", True)
    min_tokens_to_compress = int(req_config.get("min_tokens_to_compress", 10))
    target_ratio = req_config.get("target_ratio")
    ratio_val = float(target_ratio) if target_ratio is not None else None

    # Fast single-pass per-message cached compression pipeline:
    # - Preserves exact message count, order, role, tool_call_id, tool_calls, name
    # - Uses content-addressed blake2b cache so historical turns in routermod finish in <1ms
    # - Bounded by REQUEST_TIME_BUDGET_SEC (1.4s) so responses never exceed routermod's 3000ms timeout
    deadline = t0 + REQUEST_TIME_BUDGET_SEC
    compressed_messages: list[Any] = []
    transforms_applied: list[str] = []
    ccr_hashes: list[str] = []
    tokens_before = 0
    tokens_after = 0

    for orig_msg in messages:
        if not isinstance(orig_msg, dict):
            compressed_messages.append(orig_msg)
            continue

        comp_msg = dict(orig_msg)
        role = comp_msg.get("role", "user")
        content = comp_msg.get("content")
        budget_expired = time.time() > deadline
        skip_role = (role == "user" and not compress_user_messages) or (
            role in ("system", "developer") and not compress_system_messages
        )

        # Case A: String content
        if isinstance(content, str):
            tokens_before += 4
            tokens_after += 4
            if skip_role or budget_expired or len(content) < 60:
                tok = _fast_count_tokens(content)
                tokens_before += tok
                tokens_after += tok
            else:
                new_text, label, orig_tok, new_tok = _compress_text_cached(
                    content,
                    strategy=strategy,
                    target_ratio=ratio_val,
                    min_tokens=min_tokens_to_compress,
                )
                tokens_before += orig_tok
                tokens_after += new_tok
                if label and new_text != content:
                    comp_msg["content"] = new_text
                    transforms_applied.append(label)
                    if "hash=" in new_text:
                        for h in CCR_HASH_RE.findall(new_text):
                            if h not in ccr_hashes:
                                ccr_hashes.append(h)
                            if h not in CCR_STORE:
                                CCR_STORE[h] = {
                                    "hash": h,
                                    "original": content,
                                    "strategy": label,
                                    "timestamp": time.time(),
                                }

        # Case B: List of content blocks (OpenAI / Claude content arrays)
        elif isinstance(content, list):
            tokens_before += 4
            tokens_after += 4
            if skip_role or budget_expired:
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        tok = _fast_count_tokens(block["text"])
                        tokens_before += tok
                        tokens_after += tok
            else:
                new_blocks = []
                block_changed = False
                for block in content:
                    if (
                        time.time() <= deadline
                        and isinstance(block, dict)
                        and isinstance(block.get("text"), str)
                    ):
                        b_text = block["text"]
                        if len(b_text) < 60:
                            tok = _fast_count_tokens(b_text)
                            tokens_before += tok
                            tokens_after += tok
                            new_blocks.append(block)
                        else:
                            new_text, label, orig_tok, new_tok = _compress_text_cached(
                                b_text,
                                strategy=strategy,
                                target_ratio=ratio_val,
                                min_tokens=min_tokens_to_compress,
                            )
                            tokens_before += orig_tok
                            tokens_after += new_tok
                            if label and new_text != b_text:
                                block = {**block, "text": new_text}
                                transforms_applied.append(label)
                                block_changed = True
                            new_blocks.append(block)
                    else:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            tok = _fast_count_tokens(block["text"])
                            tokens_before += tok
                            tokens_after += tok
                        new_blocks.append(block)
                if block_changed:
                    comp_msg["content"] = new_blocks
        else:
            tokens_before += 4
            tokens_after += 4

        compressed_messages.append(comp_msg)

    if not transforms_applied:
        transforms_applied.append("router:noop")

    tokens_saved = max(0, tokens_before - tokens_after)
    if tokens_saved == 0:
        tokens_after = tokens_before

    proxy_compression_ratio = round(tokens_after / tokens_before, 4) if tokens_before > 0 else 1.0
    savings_percentage = round((tokens_saved / tokens_before) * 100, 2) if tokens_before > 0 else 0.0
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

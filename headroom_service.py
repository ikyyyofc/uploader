#!/usr/bin/env python3
"""
Headroom Compression Microservice & Proxy Backend
Integrates https://github.com/headroomlabs-ai/headroom (headroom-ai)
Compatible with the headroom-ai Node.js/TypeScript SDK (/v1/compress, /health, /stats, /v1/retrieve).
"""

from __future__ import annotations

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
from fastapi.responses import JSONResponse
import uvicorn

import headroom
from headroom import CompressConfig, compress
from headroom.transforms.code_compressor import CodeAwareCompressor, detect_language
from headroom.transforms.content_detector import ContentType, detect_content_type
from headroom.transforms.diff_compressor import DiffCompressor
from headroom.transforms.log_compressor import LogCompressor
from headroom.transforms.search_compressor import SearchCompressor
from headroom.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig
from headroom.transforms.text_crusher import TextCrusher, TextCrusherConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("headroom-service")

app = FastAPI(
    title="Headroom Compression Service",
    description="Context optimization and compression layer for LLM prompts, logs, JSON, code, and files.",
    version=getattr(headroom, "__version__", "0.44.0"),
)

START_TIME = time.time()
CCR_HASH_RE = re.compile(r"hash=([0-9a-fA-F]{12,64})")

# In-memory CCR store & stats tracker
CCR_STORE: dict[str, dict[str, Any]] = {}
STATS: dict[str, Any] = {
    "total_requests": 0,
    "total_tokens_before": 0,
    "total_tokens_after": 0,
    "total_tokens_saved": 0,
    "recent_requests": [],
}

# Reusable compressor instances
_log_compressor = LogCompressor()
_diff_compressor = DiffCompressor()
_search_compressor = SearchCompressor()
_code_compressor = CodeAwareCompressor()
_smart_crusher = SmartCrusher()
_text_crusher = TextCrusher()


def _count_text_tokens(text: str, model: str = "gpt-4o") -> int:
    """Count tokens using Headroom's pipeline tokenizer with character estimation fallback."""
    if not text:
        return 0
    try:
        from headroom.compress import _get_pipeline

        pipeline = _get_pipeline()
        tokenizer = pipeline._get_tokenizer(model)
        return tokenizer.count_text(text)
    except Exception:
        return max(1, (len(text) + 3) // 4)


def _count_messages_tokens(messages: list[dict[str, Any]], model: str = "gpt-4o") -> int:
    """Count tokens across a list of messages."""
    if not messages:
        return 0
    try:
        from headroom.compress import _get_pipeline

        pipeline = _get_pipeline()
        tokenizer = pipeline._get_tokenizer(model)
        return tokenizer.count_messages(messages)
    except Exception:
        total = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total += _count_text_tokens(content, model) + 4
            elif isinstance(content, list):
                total += _count_text_tokens(json.dumps(content), model) + 4
        return total


def _apply_direct_compressor(
    text: str,
    strategy: str = "auto",
    target_ratio: float | None = None,
) -> tuple[str, str | None]:
    """
    Apply a specific or auto-detected Headroom transform directly when the
    high-level ContentRouter skips a message (e.g. due to CCR marker gates or short inputs).
    Returns (compressed_text, transform_label).
    """
    if not text or not isinstance(text, str) or len(text.strip()) < 40:
        return text, None

    chosen = strategy.lower().strip() if strategy else "auto"

    if chosen == "auto":
        # Check if valid JSON array/object
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

    if chosen == "auto":
        det = detect_content_type(text)
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
            lang, conf = detect_language(text)
            if conf >= 0.55:
                chosen = "code"
            else:
                chosen = "text"

    try:
        if chosen in ("smart_crusher", "json"):
            res = _smart_crusher.crush(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"smart_crusher:{res.compression_ratio:.2f}"
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
                return res.compressed, f"log:{res.compression_ratio:.2f}"
        elif chosen in ("diff", "git_diff"):
            res = _diff_compressor.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"diff:{res.compression_ratio:.2f}"
        elif chosen in ("search", "grep"):
            res = _search_compressor.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"search:{res.compression_ratio:.2f}"
        elif chosen in ("code", "ast"):
            res = _code_compressor.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"code_aware:{res.compression_ratio:.2f}"
            # Fallback to TextCrusher if AST compressor doesn't shrink
            tc_res = _text_crusher.compress(text)
            if tc_res and tc_res.compressed and len(tc_res.compressed) < len(text):
                return tc_res.compressed, f"text_crusher:{tc_res.compression_ratio:.2f}"
        elif chosen in ("text", "text_crusher"):
            if target_ratio and 0.05 <= float(target_ratio) <= 0.95:
                tc = TextCrusher(TextCrusherConfig(max_token_ratio=float(target_ratio)))
                res = tc.compress(text)
            else:
                res = _text_crusher.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"text_crusher:{res.compression_ratio:.2f}"
    except Exception as exc:
        logger.warning("Direct compressor (%s) warning: %s", chosen, exc)

    # Final fallback: try TextCrusher if nothing compressed yet
    if chosen != "text":
        try:
            res = _text_crusher.compress(text)
            if res and res.compressed and len(res.compressed) < len(text):
                return res.compressed, f"text_crusher:{res.compression_ratio:.2f}"
        except Exception:
            pass

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
    ts = STATS["total_tokens_saved"]
    avg_pct = round((ts / tb) * 100, 2) if tb > 0 else 0.0
    return {
        **STATS,
        "average_savings_pct": avg_pct,
        "version": getattr(headroom, "__version__", "0.44.0"),
        "uptime_seconds": round(time.time() - START_TIME, 2),
    }


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

    # Check local CCR_STORE or prefix match
    entry = CCR_STORE.get(hash_key)
    if not entry:
        matches = [v for k, v in CCR_STORE.items() if k.startswith(str(hash_key))]
        if len(matches) == 1:
            entry = matches[0]

    if not entry:
        # Also check Headroom's internal CCR store if available
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
    compress_user_messages = req_config.get("compress_user_messages", True)
    compress_system_messages = req_config.get("compress_system_messages", True)
    protect_recent = int(req_config.get("protect_recent", 0))
    protect_analysis_context = bool(req_config.get("protect_analysis_context", False))
    min_tokens_to_compress = int(req_config.get("min_tokens_to_compress", 10))
    target_ratio = req_config.get("target_ratio")
    mode = req_config.get("mode") if req_config.get("mode") in (None, "agent") else None
    verify_lossless = bool(req_config.get("verify_lossless", False))

    cfg = CompressConfig(
        compress_user_messages=compress_user_messages,
        compress_system_messages=compress_system_messages,
        protect_recent=protect_recent,
        protect_analysis_context=protect_analysis_context,
        min_tokens_to_compress=min_tokens_to_compress,
        target_ratio=float(target_ratio) if target_ratio is not None else None,
        kompress_model="disabled",
        mode=mode,
        verify_lossless=verify_lossless,
    )

    model_limit = int(token_budget) if token_budget and int(token_budget) > 0 else 200000

    # If user explicitly forced a specific compressor strategy (not "auto"), run it directly
    forced_strategy = strategy and strategy.lower() not in ("auto", "default", "router")

    if not forced_strategy:
        result = compress(
            messages,
            model=model,
            model_limit=model_limit,
            config=cfg,
        )
        compressed_messages = [dict(m) for m in result.messages]
        tokens_before = result.tokens_before
        tokens_after = result.tokens_after
        transforms_applied = list(result.transforms_applied)
    else:
        compressed_messages = [dict(m) for m in messages]
        tokens_before = _count_messages_tokens(messages, model)
        tokens_after = tokens_before
        transforms_applied = []

    # Check if any message was skipped or if forced_strategy is active
    if forced_strategy or tokens_after >= tokens_before:
        updated_messages = []
        any_direct_change = False
        for idx, orig_msg in enumerate(messages):
            comp_msg = dict(compressed_messages[idx]) if idx < len(compressed_messages) else dict(orig_msg)
            role = comp_msg.get("role", "user")
            content = comp_msg.get("content")

            if role == "user" and not compress_user_messages:
                updated_messages.append(comp_msg)
                continue
            if role == "system" and not compress_system_messages:
                updated_messages.append(comp_msg)
                continue

            if isinstance(content, str) and (forced_strategy or content == orig_msg.get("content")):
                orig_tok = _count_text_tokens(content, model)
                if orig_tok >= min_tokens_to_compress:
                    new_text, label = _apply_direct_compressor(
                        content,
                        strategy=strategy if forced_strategy else "auto",
                        target_ratio=float(target_ratio) if target_ratio is not None else None,
                    )
                    if label and new_text != content:
                        new_tok = _count_text_tokens(new_text, model)
                        if new_tok < orig_tok:
                            comp_msg["content"] = new_text
                            transforms_applied = [t for t in transforms_applied if t != "router:noop"]
                            transforms_applied.append(f"headroom:{label}")
                            any_direct_change = True

            updated_messages.append(comp_msg)

        if any_direct_change:
            compressed_messages = updated_messages
            tokens_before = _count_messages_tokens(messages, model)
            tokens_after = _count_messages_tokens(compressed_messages, model)

    tokens_saved = max(0, tokens_before - tokens_after)
    if tokens_saved == 0:
        tokens_after = tokens_before

    # Proxy convention expected by headroom-ai npm SDK:
    # compression_ratio = tokens_after / tokens_before (1.0 = no compression, 0.25 = 75% saved)
    proxy_compression_ratio = round(tokens_after / tokens_before, 4) if tokens_before > 0 else 1.0
    savings_percentage = round((tokens_saved / tokens_before) * 100, 2) if tokens_before > 0 else 0.0

    # Extract any CCR hashes from compressed messages and store originals
    ccr_hashes: list[str] = []
    for idx, m in enumerate(compressed_messages):
        c = m.get("content")
        if isinstance(c, str):
            found = CCR_HASH_RE.findall(c)
            for h in found:
                if h not in ccr_hashes:
                    ccr_hashes.append(h)
                if h not in CCR_STORE and idx < len(messages):
                    orig_c = messages[idx].get("content")
                    if isinstance(orig_c, str):
                        CCR_STORE[h] = {
                            "hash": h,
                            "original": orig_c,
                            "strategy": ",".join(transforms_applied),
                            "timestamp": time.time(),
                        }

    latency_ms = round((time.time() - t0) * 1000, 2)

    # Update global stats
    STATS["total_requests"] += 1
    STATS["total_tokens_before"] += tokens_before
    STATS["total_tokens_after"] += tokens_after
    STATS["total_tokens_saved"] += tokens_saved
    STATS["recent_requests"] = (
        [
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "model": model,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "tokens_saved": tokens_saved,
                "savings_percentage": savings_percentage,
                "transforms_applied": transforms_applied,
                "latency_ms": latency_ms,
            }
        ]
        + STATS["recent_requests"]
    )[:25]

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

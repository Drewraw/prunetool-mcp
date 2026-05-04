"""
proxy_server.py — PruneTool Local AI Proxy
==========================================
OpenAI-compatible proxy on http://localhost:8080/v1

Point ANY IDE or tool here instead of the real LLM API:
  Cursor:       Settings → OpenAI Base URL → http://localhost:8080/v1
  Continue.dev: config.json → apiBase: http://localhost:8080/v1
  JetBrains AI: Settings → Custom OpenAI endpoint → http://localhost:8080/v1
  LM Studio:    API base → http://localhost:8080/v1

What happens on every request:
  1. Intercepts the chat/completions call
  2. Extracts the user query from messages
  3. Calls PruneTool gateway /prune → gets pruned codebase context
  4. Injects pruned context into the system prompt
  5. Forwards enriched request to the real LLM (Anthropic / OpenAI / Groq)
  6. Streams response back unchanged
  7. Logs token usage

Without PruneTool:  IDE sends full files (20K-100K tokens)
With this proxy:    IDE sends pruned context (1K-5K tokens) — 90% savings
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
import uvicorn

# ── Config ────────────────────────────────────────────────────────────
# Load from ~/.prunetool/.env first (user config), then local .env
_user_env = Path.home() / ".prunetool" / ".env"
if _user_env.exists():
    load_dotenv(_user_env)
load_dotenv(Path(__file__).parent / ".env")

PROXY_PORT    = int(os.environ.get("PRUNE_PROXY_PORT",    8080))
GATEWAY_URL   = os.environ.get("GATEWAY_URL",             "http://localhost:8000")
CODEBASE_ROOT = Path(os.environ.get("PRUNE_CODEBASE_ROOT", Path.cwd()))
TOKEN_LOG     = CODEBASE_ROOT / ".prunetool" / "token_log.jsonl"

# ── Pre-fetch cache ───────────────────────────────────────────────────
# Keyed by query string. Populated by background watcher when files change.
# Structure: { query_hash: {"context": str, "raw_t": int, "pruned_t": int, "ts": float} }
_prefetch_cache: dict[str, dict] = {}
_prefetch_lock  = asyncio.Lock()

# Last file that changed — used to warm the cache for the active file
_last_changed_file: Optional[str] = None
PREFETCH_TTL    = 30.0   # seconds before a cached entry expires
PREFETCH_MAX    = 20     # max entries to keep in memory

# Where to forward requests after context injection
# Auto-detected from API key present in environment
def _detect_upstream() -> tuple[str, str]:
    """Return (upstream_base_url, provider_name)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "https://api.anthropic.com", "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "https://api.openai.com", "openai"
    if os.environ.get("GROQ_API_KEY"):
        return "https://api.groq.com/openai", "groq"
    # Default fallback
    return "https://api.openai.com", "openai"

UPSTREAM_URL, UPSTREAM_PROVIDER = _detect_upstream()

# ── FastAPI app ───────────────────────────────────────────────────────
app = FastAPI(title="PruneTool Local AI Proxy", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Token logging ─────────────────────────────────────────────────────
def _log_tokens(input_t: int, output_t: int, model: str, query: str = ""):
    try:
        TOKEN_LOG.parent.mkdir(exist_ok=True)
        entry = json.dumps({
            "ts":           time.time(),
            "tokens":       input_t + output_t,
            "input_tokens": input_t,
            "output_tokens": output_t,
            "model":        model,
            "query":        query[:120],
            "source":       "proxy",
        })
        with TOKEN_LOG.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass


# ── PruneTool context injection ───────────────────────────────────────

def _cache_key(query: str) -> str:
    import hashlib
    return hashlib.md5(query.strip().lower().encode()).hexdigest()[:12]


async def _call_prune(query: str) -> tuple[str, int, int]:
    """Raw /prune call. Returns (context, raw_tokens, pruned_tokens)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{GATEWAY_URL}/prune",
                json={"user_query": query, "max_tokens": 6000},
            )
        if resp.status_code != 200:
            return "", 0, 0
        data     = resp.json()
        files    = data.get("pruned_files", [])
        stats    = data.get("stats", {})
        if not files:
            return "", 0, 0
        parts = []
        for f in files:
            path    = f.get("file_path", "")
            content = f.get("pruned_content", "")
            if path and content:
                parts.append(f"### {path}\n```\n{content}\n```")
        context  = "\n\n".join(parts)
        raw_t    = stats.get("total_raw_tokens", 0)
        pruned_t = stats.get("total_pruned_tokens", 0)
        return context, raw_t, pruned_t
    except Exception as e:
        print(f"[proxy] /prune call failed: {e}", flush=True)
        return "", 0, 0


async def _trigger_scan_and_wait() -> bool:
    """
    Trigger a project scan via gateway and wait for completion.
    Called by proxy when /prune returns empty (no index yet).
    Returns True when scan completes.
    """
    print(f"\n[proxy] No project index found — triggering auto-scan...", flush=True)
    print(f"[proxy] This takes ~15-60s and only happens once.", flush=True)
    print(f"[proxy] Your request will continue after scan completes.", flush=True)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{GATEWAY_URL}/re-scan", json={})
    except Exception as e:
        print(f"[proxy] Could not trigger scan: {e}", flush=True)
        return False

    # Poll /scan-status until complete
    deadline = time.time() + 180
    last_msg = ""
    while time.time() < deadline:
        await asyncio.sleep(3)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{GATEWAY_URL}/scan-status")
            data  = resp.json()
            stage = data.get("stage", "")
            msg   = data.get("message", "")
            ann   = data.get("annotated", 0)
            total = data.get("total_to_annotate", 0)
            files = data.get("files_found", 0)
            syms  = data.get("symbols_found", 0)

            if stage == "annotating":
                pct  = int(ann / total * 100) if total else 0
                line = f"annotating {ann}/{total} ({pct}%)"
            elif stage == "complete":
                line = f"done — {files} files, {syms} symbols"
            else:
                line = msg or stage

            if line != last_msg:
                print(f"[scan]  {line}", flush=True)
                last_msg = line

            if stage == "complete":
                print(f"[proxy] ✓ Scan complete — retrying your request now\n", flush=True)
                return True
        except Exception:
            pass

    print(f"[proxy] Scan timed out — continuing without full context", flush=True)
    return False


async def _get_pruned_context(query: str) -> tuple[str, int, int]:
    """
    Return pruned context for query.
    1. Check pre-fetch cache (sub-millisecond)
    2. Call /prune live on cache miss
    3. If /prune returns empty (no index), trigger auto-scan and retry once
    """
    if not query.strip():
        return "", 0, 0

    key = _cache_key(query)
    now = time.time()

    async with _prefetch_lock:
        entry = _prefetch_cache.get(key)
        if entry and (now - entry["ts"]) < PREFETCH_TTL:
            print(f"[proxy] cache HIT  — skipping /prune call (~{entry['pruned_t']} tokens ready)", flush=True)
            return entry["context"], entry["raw_t"], entry["pruned_t"]

    # Cache miss — call live
    context, raw_t, pruned_t = await _call_prune(query)

    # If empty, index might not exist yet — trigger scan and retry once
    if not context:
        scanned = await _trigger_scan_and_wait()
        if scanned:
            context, raw_t, pruned_t = await _call_prune(query)

    if context:
        async with _prefetch_lock:
            if len(_prefetch_cache) >= PREFETCH_MAX:
                oldest = min(_prefetch_cache, key=lambda k: _prefetch_cache[k]["ts"])
                del _prefetch_cache[oldest]
            _prefetch_cache[key] = {
                "context":  context,
                "raw_t":    raw_t,
                "pruned_t": pruned_t,
                "ts":       time.time(),
            }

    return context, raw_t, pruned_t


async def _prefetch_for_query(query: str):
    """
    Fire-and-forget pre-fetch. Called when a file changes so the
    context is warm before the user's next keystroke trigger.
    """
    if not query.strip():
        return
    key = _cache_key(query)
    now = time.time()
    async with _prefetch_lock:
        entry = _prefetch_cache.get(key)
        if entry and (now - entry["ts"]) < PREFETCH_TTL:
            return  # already fresh

    context, raw_t, pruned_t = await _call_prune(query)
    if context:
        async with _prefetch_lock:
            if len(_prefetch_cache) >= PREFETCH_MAX:
                oldest = min(_prefetch_cache, key=lambda k: _prefetch_cache[k]["ts"])
                del _prefetch_cache[oldest]
            _prefetch_cache[key] = {
                "context":  context,
                "raw_t":    raw_t,
                "pruned_t": pruned_t,
                "ts":       time.time(),
            }
        print(f"[proxy] pre-fetch done — {pruned_t} tokens cached for next request", flush=True)


def _inject_context(messages: list, context: str) -> list:
    """
    Inject pruned context into the messages list.
    Prepends a system message or appends to existing system message.
    """
    if not context:
        return messages

    context_block = (
        "## Relevant Codebase Context (injected by PruneTool)\n"
        "The following is the pruned, goal-relevant code from the project. "
        "Use it to answer accurately. Ignore irrelevant sections.\n\n"
        + context
    )

    result = list(messages)
    # Find existing system message
    for i, msg in enumerate(result):
        if msg.get("role") == "system":
            existing = msg.get("content", "")
            if isinstance(existing, str):
                result[i] = {"role": "system", "content": existing + "\n\n" + context_block}
            elif isinstance(existing, list):
                result[i]["content"].append({"type": "text", "text": "\n\n" + context_block})
            return result

    # No system message — prepend one
    result.insert(0, {"role": "system", "content": context_block})
    return result


def _extract_query(messages: list) -> str:
    """Extract the last user message text as the query for PruneTool."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content[:500]
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block.get("text", "")[:500]
    return ""


def _print_proxy_summary(query: str, raw_t: int, pruned_t: int, model: str, injected: bool):
    savings = round((1 - pruned_t / raw_t) * 100) if raw_t and pruned_t else 0
    if injected:
        print(f"\n┌─ PruneTool Proxy — Context Injected ──────────────────────────────", flush=True)
        print(f"│  Model    : {model}", flush=True)
        print(f"│  Query    : {query[:60]}{'...' if len(query) > 60 else ''}", flush=True)
        print(f"│  Raw code : {raw_t:,} tokens → Pruned: {pruned_t:,} tokens ({savings}% saved)", flush=True)
        print(f"│  Upstream : {UPSTREAM_URL} ({UPSTREAM_PROVIDER})", flush=True)
        print(f"└────────────────────────────────────────────────────────────────────\n", flush=True)
    else:
        print(f"[proxy] passthrough → {model} (no pruned context available)", flush=True)


# ── Background: poll gateway for context_updated events ─────────────
async def _watch_context_updates():
    """
    Polls gateway /context-version every 3s.
    When version changes, invalidates stale cache entries so next
    request gets fresh context instead of outdated pruned output.
    """
    last_version = ""
    while True:
        try:
            await asyncio.sleep(3)
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{GATEWAY_URL}/context-version")
            if resp.status_code == 200:
                data        = resp.json()
                new_version = data.get("version", "")
                if new_version and new_version != last_version:
                    if last_version:
                        # Cache is stale — clear it so next request re-prunes
                        async with _prefetch_lock:
                            _prefetch_cache.clear()
                        print(
                            f"[proxy] context updated ({last_version} → {new_version}) "
                            f"— cache cleared, next request will re-prune",
                            flush=True,
                        )
                    last_version = new_version
        except asyncio.CancelledError:
            break
        except Exception:
            pass  # gateway not up yet, keep polling


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_watch_context_updates())


# ── /prefetch — called by IDE extensions or gateway to warm cache ─────
class PrefetchRequest(BaseModel):
    query: str

@app.post("/prefetch")
async def prefetch_endpoint(req: PrefetchRequest):
    """
    Pre-warm the context cache for a query.
    IDEs can call this when a file opens (before user types).
    Gateway calls this after every auto-update.
    """
    asyncio.create_task(_prefetch_for_query(req.query))
    return {"status": "queued", "query": req.query[:60]}


# ── /v1/models — IDE compatibility ───────────────────────────────────
@app.get("/v1/models")
async def list_models():
    """Return a minimal model list so IDEs don't reject the endpoint."""
    return JSONResponse({
        "object": "list",
        "data": [
            {"id": "prunetool-proxy",          "object": "model", "owned_by": "prunetool"},
            {"id": "claude-sonnet-4-6",        "object": "model", "owned_by": "anthropic"},
            {"id": "claude-opus-4-7",          "object": "model", "owned_by": "anthropic"},
            {"id": "gpt-4o",                   "object": "model", "owned_by": "openai"},
            {"id": "llama-3.1-8b-instant",     "object": "model", "owned_by": "groq"},
        ],
    })


# ── /v1/chat/completions — main interception point ───────────────────
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body      = await request.body()
    body_json = json.loads(body)

    messages  = body_json.get("messages", [])
    model     = body_json.get("model", "gpt-4o")
    is_stream = body_json.get("stream", False)
    query     = _extract_query(messages)

    # ── Step 1: Get pruned context from gateway ───────────────────
    context, raw_t, pruned_t = await _get_pruned_context(query)
    injected = bool(context)

    # ── Step 2: Inject context into messages ──────────────────────
    enriched_messages = _inject_context(messages, context)
    body_json["messages"] = enriched_messages

    _print_proxy_summary(query, raw_t, pruned_t, model, injected)

    # ── Step 3: Forward to real upstream LLM ─────────────────────
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "content-encoding")
    }
    upstream_path = f"{UPSTREAM_URL}/v1/chat/completions"
    enriched_body = json.dumps(body_json).encode()

    async with httpx.AsyncClient(timeout=120.0) as client:

        if is_stream:
            async def stream_response() -> AsyncIterator[bytes]:
                input_t = output_t = 0
                async with client.stream(
                    "POST", upstream_path,
                    headers=headers, content=enriched_body,
                ) as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:].strip()
                            if data == "[DONE]":
                                yield b"data: [DONE]\n\n"
                                continue
                            try:
                                evt = json.loads(data)
                                # OpenAI streaming usage (stream_options)
                                usage = evt.get("usage") or {}
                                if usage:
                                    input_t  = usage.get("prompt_tokens", input_t)
                                    output_t = usage.get("completion_tokens", output_t)
                            except Exception:
                                pass
                            yield f"data: {data}\n\n".encode()
                        elif line:
                            yield (line + "\n").encode()
                if input_t + output_t:
                    _log_tokens(input_t, output_t, model, query)

            return StreamingResponse(
                stream_response(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        else:
            resp = await client.post(
                upstream_path, headers=headers, content=enriched_body,
            )
            try:
                rj    = resp.json()
                usage = rj.get("usage", {})
                _log_tokens(
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                    model, query,
                )
            except Exception:
                pass
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type="application/json",
            )


# ── Passthrough for all other routes (Anthropic native, etc.) ────────
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
async def passthrough(request: Request, path: str):
    """Forward anything else (Anthropic native API, health checks, etc.) unchanged."""
    url     = f"{UPSTREAM_URL}/{path}"
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    body = await request.body()

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.request(request.method, url, headers=headers, content=body)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in ("content-encoding", "transfer-encoding")},
    )


# ── Startup banner ────────────────────────────────────────────────────
def _print_banner():
    print(f"\n{'═'*62}", flush=True)
    print(f"  PruneTool Local AI Proxy  —  v2.0", flush=True)
    print(f"{'═'*62}", flush=True)
    print(f"  Proxy URL  : http://localhost:{PROXY_PORT}/v1", flush=True)
    print(f"  Upstream   : {UPSTREAM_URL} ({UPSTREAM_PROVIDER})", flush=True)
    print(f"  Gateway    : {GATEWAY_URL}", flush=True)
    print(f"  Token log  : {TOKEN_LOG}", flush=True)
    print(f"{'─'*62}", flush=True)
    print(f"  Point your IDE here:", flush=True)
    print(f"    Cursor      → Settings → OpenAI Base URL", flush=True)
    print(f"    Continue    → config.json → apiBase", flush=True)
    print(f"    JetBrains   → Custom OpenAI endpoint", flush=True)
    print(f"    LM Studio   → API base", flush=True)
    print(f"  All set to: http://localhost:{PROXY_PORT}/v1", flush=True)
    print(f"{'═'*62}\n", flush=True)


if __name__ == "__main__":
    _print_banner()
    uvicorn.run(app, host="127.0.0.1", port=PROXY_PORT, log_level="warning")

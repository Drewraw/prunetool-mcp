"""
Context-Aware Pruning Gateway — FastAPI Middleware Server
==========================================================
The central gateway that orchestrates the full pruning pipeline:

  Intercept Request → Goal-Directed Search → On-Demand Extraction
  → Precision Pruning → Cache-Stable Prompt Assembly → LLM API

Serves the React Webview UI and provides WebSocket for live updates.

Endpoints:
  POST /prune          — Run the pruning pipeline on a query
  POST /index          — Trigger a full re-index of the codebase
  GET  /skeleton       — Get the current skeletal index
  GET  /stats          — Get pruning/cache statistics
  GET  /config         — Get/update gateway configuration
  WS   /ws             — WebSocket for live skeleton & pruning updates
  GET  /               — Serve the Webview UI
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indexer.skeletal_indexer import SkeletalIndexer
from indexer.models import SkeletalIndex
from indexer.file_watcher import SkeletonFileWatcher
from indexer.mindmap_generator import MindmapGenerator, generate_mindmap_summary
from indexer.module_annotations import ModuleAnnotationsManager
from pruner.pruning_engine import PruningEngine
from pruner.models import PruneRequest
from pruner.token_counter import count_tokens
from pruner.scout import Scout
from pruner.storage_manager import StorageManager
from cache.cache_stabilizer import CacheStabilizer, CacheConfig, PrunedCodeBlock
from server.user_manager import UserManager

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------

CODEBASE_ROOT = os.environ.get("PRUNE_CODEBASE_ROOT", os.getcwd())
INDEX_PATH = os.environ.get(
    "PRUNE_INDEX_PATH",
    os.path.join(CODEBASE_ROOT, ".prunetool", "skeleton.json"),
)
ANNOTATIONS_PATH = os.path.join(CODEBASE_ROOT, ".prunetool", "annotations.json")
FIREBASE_CREDS = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
UI_DIST_PATH = str(Path(__file__).resolve().parent.parent / "ui" / "dist")
DEFAULT_SYSTEM_INSTRUCTIONS = """You are an expert software engineer.
Analyze the provided codebase context to answer the user's question.
Focus on accuracy and cite specific file paths and line numbers."""

_ENV_SKIP_DIRS = {
    ".git", ".venv", "node_modules", "dist", "build", "cache", "__pycache__",
    ".next", ".dart_tool", ".firebase",
}


def _parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return env


def _find_env_file(search_root: Path | None = None) -> Path | None:
    env_hint = os.environ.get("PRUNE_ENV_FILE", "").strip()
    if env_hint:
        hinted = Path(env_hint)
        if hinted.exists():
            return hinted

    root = search_root or Path(CODEBASE_ROOT)
    if root.is_file():
        root = root.parent

    direct = root / ".env"
    if direct.exists():
        return direct

    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _ENV_SKIP_DIRS]
        if ".env" in filenames:
            candidate = Path(dirpath) / ".env"
            if candidate != direct:
                candidates.append(candidate)

    if not candidates:
        return None

    for candidate in candidates:
        if _parse_env_file(candidate).get("PRUNE_CODEBASE_ROOT"):
            return candidate

    return min(candidates, key=lambda p: len(p.relative_to(root).parts))

# -------------------------------------------------------------------
# Global State
# -------------------------------------------------------------------

indexer: Optional[SkeletalIndexer] = None
skeleton: Optional[SkeletalIndex] = None
pruner: Optional[PruningEngine] = None
cache_stabilizer: Optional[CacheStabilizer] = None
file_watcher: Optional[SkeletonFileWatcher] = None
annotations_manager: Optional[ModuleAnnotationsManager] = None
user_manager: Optional[UserManager] = None
scout: Optional[Scout] = None
storage: Optional[StorageManager] = None
connected_websockets: list[WebSocket] = []

# Pruning history for the UI
prune_history: list[dict] = []
MAX_HISTORY = 50

# Bifrost token metrics — polled every 10s from http://localhost:8090/metrics
burned_stats: dict = {
    "input_tokens":  0,
    "output_tokens": 0,
    "total_tokens":  0,
    "status":        "Offline",
}

# Shared cache for Prompt Assist, synced from terminal_context.md.
_prompt_assist_shared_context: dict = {
    "text": "",
    "updated_at": None,
    "source": "",
    "last_reason": "",
}

# Live scan progress — polled by MCP server terminal
_scan_status: dict = {
    "stage":        "idle",   # idle | loading_library | scanning | building_map | annotating | complete
    "message":      "",
    "files_found":  0,
    "symbols_found": 0,
    "annotated":    0,
    "total_to_annotate": 0,
    "started_at":   None,
    "finished_at":  None,
}

# Context version — bumped every time terminal_context.md changes
# Sections stored separately so describe_project can return deltas
_context_version: dict = {
    "version":          "",          # short hash e.g. "a3f9b2"
    "updated_at":       0.0,
    "sections": {                    # last-known hash per section
        "folder_map":       "",
        "auto_annotations": "",
        "prune_library":    "",
        "readme":           "",
    },
}

# -------------------------------------------------------------------
# Lifespan
# -------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the indexer, load/build skeleton, start file watcher."""
    global indexer, skeleton, pruner, cache_stabilizer, file_watcher, annotations_manager, user_manager, scout, storage

    print(f"[gateway] Codebase root: {CODEBASE_ROOT}")
    print(f"[gateway] Index path: {INDEX_PATH}")
    
    # Validate CODEBASE_ROOT exists and is accessible
    croot_path = Path(CODEBASE_ROOT)
    if not croot_path.exists():
        print(f"[gateway] ERROR: CODEBASE_ROOT does not exist: {CODEBASE_ROOT}")
        raise ValueError(f"PRUNE_CODEBASE_ROOT path not found: {CODEBASE_ROOT}")
    if not croot_path.is_dir():
        print(f"[gateway] ERROR: CODEBASE_ROOT is not a directory: {CODEBASE_ROOT}")
        raise ValueError(f"PRUNE_CODEBASE_ROOT is not a directory: {CODEBASE_ROOT}")

    # Initialize Storage Manager (single source of truth)
    storage = StorageManager(CODEBASE_ROOT)
    print(f"[gateway] Storage Manager: {len(storage.user_annotations)} annotations loaded")

    # Initialize indexer
    indexer = SkeletalIndexer(CODEBASE_ROOT, INDEX_PATH)

    # Try to load existing index, or build fresh
    skeleton = indexer.load()
    if skeleton:
        print(f"[gateway] Loaded existing index: {skeleton.total_symbols} symbols from {skeleton.file_count} files")
    else:
        print("[gateway] No existing index found — building fresh...")
        skeleton = indexer.index_and_save()

    # Initialize annotations manager (legacy, still used by cache_stabilizer)
    annotations_manager = ModuleAnnotationsManager(ANNOTATIONS_PATH)

    # Sync annotations: storage manager is the authority
    if storage.user_annotations:
        annotations_manager.annotations = dict(storage.user_annotations)

    # Initialize Scout (Llama 3.1-8B-Instant via Ollama or Groq)
    scout = Scout()
    backends = scout.is_available()
    print(f"[gateway] Scout backends: Ollama={'ON' if backends['ollama'] else 'OFF'}, "
          f"Groq={'ON' if backends['groq'] else 'OFF'}")

    # Initialize pruner with Scout, annotations, and folder map
    pruner = PruningEngine(
        skeleton, CODEBASE_ROOT,
        annotations=storage.get_all_annotations(),
        scout=scout,
        folder_map=storage.folder_map,
    )
    cache_stabilizer = CacheStabilizer(CacheConfig())

    # Initialize user manager (freemium: 50 queries/day per Gmail via Firebase)
    user_manager = UserManager(FIREBASE_CREDS if FIREBASE_CREDS else None)

    # Start file watcher in background
    file_watcher = SkeletonFileWatcher(
        indexer=indexer,
        skeleton=skeleton,
        debounce_ms=500,
        on_update=_on_skeleton_update,
    )
    watcher_task = asyncio.create_task(_run_watcher())
    bifrost_task = asyncio.create_task(_poll_bifrost())

    # ── KB context auto-inject disabled — user calls describe_project manually ──
    # To re-enable: uncomment the block below
    # try:
    #     kb = _build_kb_context()
    #     ctx = kb.get("context_path", "")
    #     print(f"[gateway] KB context written → {ctx}")
    #     print("[gateway] CLAUDE.md updated — open VS Code terminal to start working")
    # except Exception as e:
    #     print(f"[gateway] KB context build failed: {e}")

    _refresh_prompt_assist_shared_context(reason="startup")
    print("[gateway] Ready. Serving on http://localhost:8420")
    yield

    # Cleanup
    if file_watcher:
        file_watcher.stop()
    watcher_task.cancel()
    bifrost_task.cancel()


async def _run_watcher():
    """Run the file watcher as a background task."""
    try:
        if file_watcher:
            await file_watcher.start()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[gateway] File watcher error: {e}")


async def _poll_bifrost():
    """Poll Bifrost /metrics every 10s and update burned_stats."""
    import re
    import httpx

    bifrost_url = os.environ.get("ANTHROPIC_BASE_URL", "http://localhost:8090")
    # Normalise: strip /v1 suffix to get base URL
    bifrost_base = bifrost_url.rstrip("/")
    if bifrost_base.endswith("/v1"):
        bifrost_base = bifrost_base[:-3]
    metrics_url = f"{bifrost_base}/metrics"

    while True:
        try:
            await asyncio.sleep(10)
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(metrics_url)
            if resp.status_code == 200:
                text = resp.text
                input_t  = sum(int(x) for x in re.findall(
                    r'bifrost_input_tokens_total\{.*?\}\s+(\d+)', text))
                output_t = sum(int(x) for x in re.findall(
                    r'bifrost_output_tokens_total\{.*?\}\s+(\d+)', text))
                burned_stats.update({
                    "input_tokens":  input_t,
                    "output_tokens": output_t,
                    "total_tokens":  input_t + output_t,
                    "status":        "Connected",
                })
            else:
                burned_stats["status"] = "Bifrost Offline"
        except asyncio.CancelledError:
            break
        except Exception:
            burned_stats["status"] = "Bifrost Offline"


def _on_skeleton_update(updated_skeleton: SkeletalIndex, changed_files: list[str] = None):
    """Called by file watcher when source files change. Runs full cache pipeline."""
    global skeleton, pruner
    skeleton = updated_skeleton
    changed_files = changed_files or []

    # Run the full async pipeline as a background task
    asyncio.create_task(_auto_update_pipeline(changed_files))


async def _auto_update_pipeline(changed_files: list[str]):
    """
    Full auto-update pipeline triggered by file watcher.
    Runs: folder_map → pruner rebuild → annotate changed files →
          terminal_context.md → version stamp → SSE push → user message.
    """
    global skeleton, pruner, _context_version

    t_start = time.time()
    groq_tokens_used = 0

    # ── Step 1: Rebuild folder map (incremental, ~1s) ──────────────
    try:
        if storage:
            storage.build_folder_map(CODEBASE_ROOT)
    except Exception as e:
        print(f"[auto-update] folder_map rebuild failed: {e}", flush=True)

    # ── Step 2: Rebuild PruningEngine with fresh data ──────────────
    annos = storage.get_all_annotations() if storage else {}
    fm    = storage.folder_map if storage else None
    pruner = PruningEngine(skeleton, CODEBASE_ROOT, annotations=annos, scout=scout, folder_map=fm)

    # ── Step 3: Auto-annotate only the changed files ───────────────
    if pruner and pruner.auto_annotator and changed_files:
        added_or_modified = [f for f in changed_files
                             if not f.endswith(('.md', '.txt', '.json', '.yaml', '.yml', '.toml'))]
        if added_or_modified:
            try:
                groq_tokens_used = await _annotate_specific_files(pruner, added_or_modified)
            except Exception as e:
                print(f"[auto-update] annotation failed: {e}", flush=True)

    # ── Step 4: Rebuild terminal_context.md + compute version ──────
    old_sections = dict(_context_version["sections"])
    try:
        _build_kb_context()
        _refresh_prompt_assist_shared_context(reason="auto-update")
    except Exception as e:
        print(f"[auto-update] terminal_context rebuild failed: {e}", flush=True)

    # Compute per-section hashes from storage data
    def _h(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()[:8]

    new_sections = {
        "folder_map":       _h(json.dumps(storage.folder_map, default=str) if storage else ""),
        "auto_annotations": _h(json.dumps(
            (storage.auto_annotations if hasattr(storage, "auto_annotations") else {}),
            default=str) if storage else ""),
        "prune_library":    _h(json.dumps(storage.user_annotations, default=str) if storage else ""),
        "readme":           _h((Path(CODEBASE_ROOT) / "README.md").read_text(encoding="utf-8", errors="ignore")
                               if (Path(CODEBASE_ROOT) / "README.md").exists() else ""),
    }

    changed_sections = [k for k, v in new_sections.items() if old_sections.get(k) != v]

    # New overall version hash
    combined = "".join(new_sections.values())
    new_version = hashlib.md5(combined.encode()).hexdigest()[:8]
    _context_version["version"]    = new_version
    _context_version["updated_at"] = time.time()
    _context_version["sections"]   = new_sections

    elapsed = time.time() - t_start

    # ── Step 5: Broadcast to dashboard WebSocket ───────────────────
    await _broadcast({
        "type":             "skeleton_updated",
        "total_symbols":    skeleton.total_symbols,
        "file_count":       skeleton.file_count,
    })

    # ── Step 6: Push context_updated SSE event to MCP clients ──────
    await _broadcast({
        "type":            "context_updated",
        "new_version":     new_version,
        "changed_sections": changed_sections,
        "changed_files":   changed_files,
        "groq_tokens_used": groq_tokens_used,
        "elapsed_s":       round(elapsed, 2),
    })

    # ── Step 7: User-visible terminal message ─────────────────────
    files_str  = ", ".join(changed_files[:3]) + ("..." if len(changed_files) > 3 else "")
    annot_note = f" | Groq annotation: ~{groq_tokens_used} tokens" if groq_tokens_used else ""
    changed_note = f" | changed: {', '.join(changed_sections)}" if changed_sections else " | no section changes"

    print(f"\n┌─ Project cache auto-saved ({'v' + new_version}) ────────────────────────", flush=True)
    print(f"│  Files  : {files_str}", flush=True)
    print(f"│  Time   : {elapsed:.2f}s{annot_note}", flush=True)
    print(f"│  Sections{changed_note}", flush=True)
    print(f"│  LLMs with stale context will receive a tap to refresh (~300 tokens)", flush=True)
    print(f"└────────────────────────────────────────────────────────────────────────\n", flush=True)


async def _annotate_specific_files(engine, file_paths: list[str]) -> int:
    """
    Annotate only the given files. Returns estimated Groq tokens used.
    Reuses the same batch logic as full annotation but for a small set.
    """
    if not engine or not engine.auto_annotator:
        return 0

    annotator = engine.auto_annotator
    tokens_used = 0
    BATCH = 8

    # Filter to files that are actually in the skeleton
    skeleton_files = {e.file_path for e in engine.skeleton.entries}
    to_annotate = [f for f in file_paths if f in skeleton_files]

    for i in range(0, len(to_annotate), BATCH):
        batch = to_annotate[i:i + BATCH]
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, annotator.annotate_batch, batch
            )
            if isinstance(result, dict):
                tokens_used += result.get("tokens_used", len(batch) * 530)
            else:
                tokens_used += len(batch) * 530
        except Exception:
            tokens_used += len(batch) * 530

    return tokens_used


async def _broadcast(message: dict):
    """Send a message to all connected WebSocket clients."""
    data = json.dumps(message)
    disconnected = []
    for ws in connected_websockets:
        try:
            await ws.send_text(data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        connected_websockets.remove(ws)


_TOKEN_LOG_PATH = os.path.join(CODEBASE_ROOT, "token_log.jsonl")

def _append_token_log(tokens_in: int, query: str = ""):
    """Append one JSON line to token_log.jsonl for the MCP token monitor."""
    entry = json.dumps({
        "ts": time.time(),
        "tokens": tokens_in,
        "query": query,
    })
    try:
        with open(_TOKEN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except OSError:
        pass  # non-fatal


# -------------------------------------------------------------------
# App
# -------------------------------------------------------------------

app = FastAPI(
    title="Context-Aware Pruning Gateway",
    description="SWE-Pruner inspired middleware for LLM context optimization",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve UI static files if built
if os.path.isdir(UI_DIST_PATH):
    app.mount("/assets", StaticFiles(directory=os.path.join(UI_DIST_PATH, "assets")), name="assets")

# -------------------------------------------------------------------
# Request/Response Models
# -------------------------------------------------------------------


class PruneRequestBody(BaseModel):
    user_query: str = Field(..., description="The developer's question or prompt")
    file_paths: list[str] = Field(default=[], description="Specific files to include")
    goal_hint: str = Field(default="", description="Optional goal hint for pruning")
    max_tokens: int = Field(default=80_000, description="Token budget for pruned output")
    compression_target: float = Field(default=0.5, description="Target compression ratio")
    system_instructions: str = Field(
        default=DEFAULT_SYSTEM_INSTRUCTIONS,
        description="System prompt for the LLM",
    )
    provider: str = Field(default="anthropic", description="LLM provider for cache format")
    user_email: str = Field(default="", description="Gmail of the signed-in user (for quota tracking)")


class IndexRequestBody(BaseModel):
    root_path: str = Field(default="", description="Override codebase root path")


class SearchRequestBody(BaseModel):
    query: str = Field(..., description="Search query against the skeleton")
    top_k: int = Field(default=20, description="Number of results to return")


class ConfigBody(BaseModel):
    codebase_root: str = Field(default="")
    cache_type: str = Field(default="ephemeral")
    provider: str = Field(default="anthropic")
    max_system_tokens: int = Field(default=4_000)
    max_code_tokens: int = Field(default=100_000)


class AnnotationSetBody(BaseModel):
    file_path: str = Field(..., description="Relative path to file (e.g., 'indexer/skeletal_indexer.py')")
    annotation: str = Field(default="", description="User's comment/note about this file")


class GoogleAuthBody(BaseModel):
    """Google OAuth token from the frontend's Google Sign-In."""
    credential: str = Field(..., description="Google ID token (JWT from Sign-In)")


class LicenseActivateBody(BaseModel):
    email: str = Field(..., description="User's Gmail address")
    license_key: str = Field(..., description="Pro license key (e.g., PRUNE-XXXX-XXXX-XXXX)")


class ScoutSelectBody(BaseModel):
    user_query: str = Field(..., description="Developer query")
    goal_hint: str = Field(default="", description="Optional focus hint")


class PromptAssistBody(BaseModel):
    user_input: str = Field(..., description="User's rough request")
    model: str = Field(default="PruneTool Balanced", description="Prompt-assist model label")
    mode: str = Field(default="prompt-assist", description="Prompt mode or style")


# -------------------------------------------------------------------
# Auth Endpoints (Firebase)
# -------------------------------------------------------------------


@app.post("/auth/verify")
async def verify_auth(body: GoogleAuthBody):
    """
    Verify a Firebase ID token from Google Sign-In.
    Returns user record + quota info.

    Flow:
      1. VS Code extension → Firebase Auth → Google Sign-In
      2. Extension gets ID token (JWT)
      3. Extension sends token here
      4. We verify with Firebase Admin SDK
      5. Return user profile + remaining queries
    """
    if not user_manager or not user_manager.is_enabled:
        # Firebase not configured — return anonymous access
        return {
            "status": "ok",
            "user": {"email": "anonymous", "tier": "local"},
            "quota": {"allowed": True, "remaining": -1, "limit": -1, "tier": "local"},
            "firebase_enabled": False,
        }

    # Verify the Firebase ID token
    decoded = user_manager.verify_token(body.credential)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Register or fetch user
    user = user_manager.login_or_register(
        email=decoded["email"],
        name=decoded.get("name", ""),
        picture=decoded.get("picture", ""),
    )

    # Check quota
    quota = user_manager.check_quota(decoded["email"])

    return {
        "status": "ok",
        "user": {
            "email": decoded["email"],
            "name": decoded.get("name", ""),
            "picture": decoded.get("picture", ""),
            "tier": user.get("tier", "free"),
        },
        "quota": quota,
        "firebase_enabled": True,
    }


@app.post("/auth/activate-pro")
async def activate_pro(body: LicenseActivateBody):
    """Activate Pro tier with a license key."""
    if not user_manager or not user_manager.is_enabled:
        raise HTTPException(status_code=503, detail="Auth service not available")

    success = user_manager.activate_pro(body.email, body.license_key)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid license key or user not found")

    return {
        "status": "ok",
        "email": body.email,
        "tier": "pro",
        "message": "Pro activated! Unlimited queries enabled.",
    }


@app.get("/auth/quota/{email}")
async def get_quota(email: str):
    """Check remaining queries for a user."""
    if not user_manager or not user_manager.is_enabled:
        return {"allowed": True, "remaining": -1, "limit": -1, "tier": "local"}

    return user_manager.check_quota(email)


@app.get("/auth/stats/{email}")
async def get_user_stats(email: str):
    """Get usage stats for a user (queries, tokens saved, tier)."""
    if not user_manager or not user_manager.is_enabled:
        return {"tier": "local", "message": "Auth not configured"}

    stats = user_manager.get_user_stats(email)
    if not stats:
        raise HTTPException(status_code=404, detail="User not found")

    return stats


# -------------------------------------------------------------------
# Endpoints
# -------------------------------------------------------------------


@app.post("/prune")
async def prune_endpoint(body: PruneRequestBody):
    """
    The main pruning pipeline endpoint.
    Intercept → Search → Extract → Prune → Assemble → Return
    """
    if not pruner or not cache_stabilizer:
        raise HTTPException(status_code=503, detail="Gateway not initialized")

    # Quota check — free tier: 50 queries/day per Gmail
    if body.user_email and user_manager:
        quota = user_manager.check_quota(body.user_email)
        if not quota["allowed"]:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "daily_limit_reached",
                    "message": quota.get("reason", "Daily query limit reached."),
                    "limit": quota.get("limit", 50),
                    "tier": quota.get("tier", "free"),
                    "upgrade_url": "https://prunetool.dev/pricing",
                },
            )

    start = time.time()

    # Step 1-4: Run the pruning engine
    # Enhance goal_hint with annotations context if available
    enhanced_goal = body.goal_hint or ""
    if annotations_manager:
        annotations_context = annotations_manager.get_context_for_query(body.file_paths if body.file_paths else None)
        if annotations_context:
            enhanced_goal = f"{enhanced_goal}\n\n{annotations_context}".strip()

    request = PruneRequest(
        user_query=body.user_query,
        file_paths=body.file_paths,
        goal_hint=enhanced_goal,
        max_tokens=body.max_tokens,
        compression_target=body.compression_target,
    )
    result = pruner.prune(request)

    # Step 5: Assemble cache-stable prompt
    # Pass structured blocks — the stabilizer handles sorting, normalization,
    # and metadata stripping to produce a bit-for-bit deterministic prefix.
    pruned_blocks = [
        PrunedCodeBlock(file_path=pf.file_path, content=pf.pruned_content)
        for pf in result.pruned_files
    ]

    # Build LLM-readable annotation context so Claude can semantically
    # understand developer notes (e.g. "billing" ↔ "payment")
    llm_annotations = ""
    if annotations_manager:
        llm_annotations = annotations_manager.get_llm_context(
            body.file_paths if body.file_paths else None
        )

    cache_stabilizer.config.provider = body.provider
    assembled = cache_stabilizer.assemble(
        system_instructions=body.system_instructions,
        pruned_blocks=pruned_blocks,
        user_query=body.user_query,
        goal_hint=result.goal_hint_used,
        extra_context=llm_annotations,
    )

    elapsed = time.time() - start

    # Build response
    response = {
        "pruned_files": [
            {
                "file_path": pf.file_path,
                "raw_content": pf.raw_content,
                "pruned_content": pf.pruned_content,
                "raw_lines": pf.raw_lines,
                "pruned_lines": pf.pruned_lines,
                "raw_tokens": pf.raw_tokens,
                "pruned_tokens": pf.pruned_tokens,
                "kept_symbols": pf.kept_symbols,
                "removed_sections": pf.removed_sections,
            }
            for pf in result.pruned_files
        ],
        "stats": {
            "total_raw_tokens": result.stats.total_raw_tokens,
            "total_pruned_tokens": result.stats.total_pruned_tokens,
            "total_raw_lines": result.stats.total_raw_lines,
            "total_pruned_lines": result.stats.total_pruned_lines,
            "files_processed": result.stats.files_processed,
            "symbols_matched": result.stats.symbols_matched,
            "compression_ratio": round(result.stats.compression_ratio, 2),
            "token_savings_pct": round(result.stats.token_savings_pct, 1),
        },
        "assembled_prompt": cache_stabilizer.format_for_api(assembled),
        "cache_info": {
            "code_hash": assembled.code_hash,
            "cache_hit_likely": assembled.cache_hit_likely,
            "system_tokens": assembled.system_tokens,
            "code_tokens": assembled.code_tokens,
            "query_tokens": assembled.query_tokens,
            "total_tokens": assembled.total_tokens,
        },
        "goal_hint": result.goal_hint_used,
        "elapsed_ms": round(elapsed * 1000, 1),
        # Active folders for Knowledge Graph highlighting
        "active_folder_ids": sorted({
            os.path.dirname(pf.file_path).replace("\\", "/") or "(root)"
            for pf in result.pruned_files
        }),
    }

    # Include quota info in response so frontend can show remaining queries
    if body.user_email and user_manager and user_manager.is_enabled:
        response["quota"] = user_manager.check_quota(body.user_email)

    # Save to history
    history_entry = {
        "timestamp": time.time(),
        "query": body.user_query[:100],
        "stats": response["stats"],
        "cache_hit": assembled.cache_hit_likely,
        "elapsed_ms": response["elapsed_ms"],
    }
    prune_history.append(history_entry)
    if len(prune_history) > MAX_HISTORY:
        prune_history.pop(0)

    # ── Append to token_log.jsonl (read by MCP token monitor) ───────
    _append_token_log(
        tokens_in=response["cache_info"]["total_tokens"],
        query=body.user_query[:80],
    )

    # Record query usage for quota tracking
    if body.user_email and user_manager and user_manager.is_enabled:
        tokens_saved = response["stats"].get("total_raw_tokens", 0) - response["stats"].get("total_pruned_tokens", 0)
        user_manager.record_query(body.user_email, tokens_saved=max(tokens_saved, 0))

    # Broadcast to WebSocket clients
    await _broadcast({
        "type": "prune_result",
        "stats": response["stats"],
        "cache_info": response["cache_info"],
        "elapsed_ms": response["elapsed_ms"],
    })

    return response


@app.post("/index")
async def index_endpoint(body: IndexRequestBody):
    """Trigger a full re-index of the codebase."""
    global indexer, skeleton, pruner

    root = body.root_path or CODEBASE_ROOT
    indexer = SkeletalIndexer(root, INDEX_PATH)
    skeleton = indexer.index_and_save()
    annos = annotations_manager.get_all_annotations() if annotations_manager else {}
    fm = storage.folder_map if storage else None
    pruner = PruningEngine(skeleton, root, annotations=annos, scout=scout, folder_map=fm)

    await _broadcast({
        "type": "skeleton_updated",
        "total_symbols": skeleton.total_symbols,
        "file_count": skeleton.file_count,
    })

    return {
        "status": "indexed",
        "file_count": skeleton.file_count,
        "total_symbols": skeleton.total_symbols,
        "indexed_at": skeleton.indexed_at,
    }


@app.post("/scout-select")
async def scout_select_endpoint(body: ScoutSelectBody):
    """
    Use knowledge graph + Scout LLM to identify relevant folders/files for a query.
    Returns selected_folders with per-folder reasoning, and all_folders for the full map.
    """
    if not pruner or not skeleton:
        raise HTTPException(status_code=503, detail="Not indexed yet. Run Scan Project first.")

    import json as _json
    from pathlib import Path as _PPath

    # Build all_folders from skeleton (full relative paths grouped by parent folder)
    all_folders: dict = {}
    for entry in skeleton.entries:
        fp = entry.file_path.replace("\\", "/")
        folder = str(_PPath(fp).parent).replace("\\", "/")
        if folder == ".":
            folder = ""
        if folder not in all_folders:
            all_folders[folder] = []
        if fp not in all_folders[folder]:
            all_folders[folder].append(fp)
    for f in all_folders:
        all_folders[f] = sorted(set(all_folders[f]))

    # Load folder_map for import relationship reasoning
    folder_map_folders: dict = {}
    folder_map_path = os.path.join(CODEBASE_ROOT, ".prunetool", "folder_map.json")
    if os.path.exists(folder_map_path):
        try:
            with open(folder_map_path, "r", encoding="utf-8") as _fh:
                _fm = _json.load(_fh)
                folder_map_folders = _fm.get("folders", {})
        except Exception:
            pass

    # Load annotations for reasoning context
    all_annotations: dict = {}
    if storage:
        try:
            all_annotations = storage.get_all_annotations() or {}
        except Exception:
            pass

    # Scout-rank; fall back to keyword search if Scout returns nothing
    entries = pruner._scout_rank(body.user_query, body.goal_hint or body.user_query)
    scout_backend = "scout"
    if not entries:
        print("[scout-select] Scout returned nothing, falling back to keyword search")
        entries = skeleton.search(body.user_query, top_k=40)
        scout_backend = "keyword"

    # Group entries by folder
    selected_folders: dict = {}
    for entry in entries:
        fp = entry.file_path.replace("\\", "/")
        folder = str(_PPath(fp).parent).replace("\\", "/")
        if folder == ".":
            folder = ""
        if folder not in selected_folders:
            selected_folders[folder] = {"files": [], "reasoning": ""}
        if fp not in selected_folders[folder]["files"]:
            selected_folders[folder]["files"].append(fp)

    # ── Force-include annotated files/folders whose annotation matches the query ──
    # Annotations are stored on FILES (e.g. "android\app\...\MainActivity.kt").
    # When a file annotation matches the query, add its parent folder.
    query_words = set(body.user_query.lower().split())
    for ann_path, ann_text in all_annotations.items():
        ann_lower = ann_text.lower()
        # Check if any meaningful query word appears in the annotation text
        if any(w in ann_lower for w in query_words if len(w) > 3):
            # Normalize path to forward slashes
            norm_path = ann_path.replace("\\", "/")
            # Get parent folder of the annotated file
            ann_folder = str(_PPath(norm_path).parent).replace("\\", "/")
            if ann_folder == ".":
                ann_folder = ""
            # Find the matching folder in all_folders
            matched_folder = ann_folder if ann_folder in all_folders else None
            if not matched_folder:
                # Try to find a folder that contains this file
                for f in all_folders:
                    if norm_path in all_folders[f] or any(norm_path.endswith(fp) for fp in all_folders[f]):
                        matched_folder = f
                        break
            if matched_folder and matched_folder not in selected_folders:
                selected_folders[matched_folder] = {
                    "files": list(all_folders[matched_folder]),
                    "reasoning": f'Included because your annotation on "{_PPath(norm_path).name}" says: "{ann_text}"',
                }
                print(f"[scout-select] Annotation match: added /{matched_folder} via file {norm_path!r}")

    # Filter .md files from the root folder — documentation files are rarely useful for code queries
    root_key = ""
    if root_key in selected_folders:
        selected_folders[root_key]["files"] = [
            f for f in selected_folders[root_key]["files"]
            if not f.lower().endswith(".md")
        ]
        if not selected_folders[root_key]["files"]:
            del selected_folders[root_key]

    # ── Shared keyword extraction ───────────────────────────────────────────
    import re as _re

    _STOP = {'how', 'does', 'do', 'the', 'a', 'an', 'is', 'are', 'was', 'were',
             'what', 'why', 'when', 'where', 'which', 'who', 'work', 'works',
             'use', 'used', 'using', 'get', 'set', 'and', 'or', 'in', 'on',
             'to', 'for', 'of', 'with', 'its', 'it', 'this', 'that', 'screen',
             'screens', 'page', 'view', 'feature', 'function', 'method', 'class',
             # File extensions — prevent "dart", "ts", "js" from matching every file
             'dart', 'ts', 'js', 'py', 'md', 'json', 'yaml', 'txt', 'html', 'css'}
    q_words = {w.lower() for w in _re.findall(r'\w+', body.user_query)
               if len(w) > 2 and w.lower() not in _STOP}

    def _feature_score(file_path: str, keywords: set) -> int:
        """Score how well a file's feature context matches query keywords."""
        parts = file_path.lower().replace("\\", "/").split("/")
        if "features" in parts:
            idx = parts.index("features")
            if idx + 1 < len(parts):
                feature_words = set(_re.findall(r'[a-z]+', parts[idx + 1]))
                match = sum(1 for kw in keywords if kw in feature_words)
                if match:
                    return match * 2
        path_words = set(_re.findall(r'[a-z]+', file_path.lower()))
        return sum(1 for kw in keywords if kw in path_words)

    def _filename_score(file_path: str, keywords: set) -> int:
        """Score by filename only (not full path) — used for intra-folder filtering."""
        fname = file_path.split("/")[-1]
        fname_words = set(_re.findall(r'[a-z]+', fname.lower()))
        return sum(1 for kw in keywords if kw in fname_words)

    # ── FIX 0: Filter generated platform files and root-level debug scripts ──
    # Generated platform files (android/ios/linux/macos/windows Flutter glue)
    # and root/functions-root debug scripts add noise without code insight.
    _GENERATED_PREFIXES = (
        '.dart_tool/', 'android/app/src/main/java/io/flutter/',
        'ios/flutter/ephemeral/', 'linux/flutter/', 'macos/flutter/',
        'windows/flutter/', 'windows/runner/',
    )
    _DEBUG_SCRIPT_RE = _re.compile(
        r'^(?:check|fix|test|migrate|run|debug)_', _re.IGNORECASE
    )
    for _folder in list(selected_folders.keys()):
        _kept = []
        for _f in selected_folders[_folder]["files"]:
            _fname = _f.split("/")[-1].lower()
            _fp_lower = _f.lower()
            if any(_fp_lower.startswith(_p) for _p in _GENERATED_PREFIXES):
                continue
            # Skip debug/test/fix scripts at root or functions root depth
            if _folder.count("/") <= 1 and _DEBUG_SCRIPT_RE.match(_fname):
                continue
            _kept.append(_f)
        if _kept:
            selected_folders[_folder]["files"] = _kept
        else:
            del selected_folders[_folder]

    # ── FIX 1: Force-include files explicitly named in the query ───────────
    # e.g. "trace upload_queue_manager.dart → foreground_upload_service.dart"
    named_files = set(_re.findall(r'\b[\w_-]+\.(?:dart|ts|js|rules|yaml|json|py)\b',
                                   body.user_query.lower()))
    if named_files:
        already_selected = {f for d in selected_folders.values() for f in d["files"]}
        for named in named_files:
            for folder, folder_files in all_folders.items():
                for fp in folder_files:
                    if fp.lower().endswith("/" + named) or fp.lower() == named:
                        if fp not in already_selected:
                            if folder not in selected_folders:
                                selected_folders[folder] = {
                                    "files": [],
                                    "reasoning": f"Explicitly named in query: {named}",
                                }
                            if fp not in selected_folders[folder]["files"]:
                                selected_folders[folder]["files"].append(fp)
                                already_selected.add(fp)

    # ── FIX 2: Subdirectory keyword expansion ──────────────────────────────
    # When Scout selected a parent folder (e.g. functions/lib/), also include
    # subdirectories whose name matches query keywords
    # (e.g. functions/lib/incident-engine/ for "incident detection" query).
    if q_words:
        new_subdir_folders: dict = {}
        for folder in list(selected_folders.keys()):
            for all_folder, folder_files in all_folders.items():
                if not all_folder.startswith(folder + "/"):
                    continue
                if all_folder in selected_folders:
                    continue
                # Get immediate subdirectory name under `folder`
                remainder = all_folder[len(folder) + 1:]
                subdir = remainder.split("/")[0]
                subdir_words = set(_re.findall(r'[a-z]+', subdir.lower()))
                if any(kw in subdir_words for kw in q_words):
                    if all_folder not in new_subdir_folders:
                        new_subdir_folders[all_folder] = {
                            "files": list(folder_files),
                            "reasoning": f"Subdirectory keyword match: {subdir}",
                        }
        selected_folders.update(new_subdir_folders)

    # ── FIX 3: Auto-include .rules files for security/audit queries ────────
    # .rules files are not indexed by the skeleton, so scan filesystem directly.
    _SECURITY_KW = {'audit', 'rules', 'security', 'permission', 'access',
                    'unauthenticated', 'firestore', 'ownership', 'read', 'write',
                    'safe', 'safety', 'network', 'moderator', 'moderation'}
    query_lower = body.user_query.lower()
    if any(kw in query_lower for kw in _SECURITY_KW):
        import os as _os
        already_selected = {f for d in selected_folders.values() for f in d["files"]}
        _skip_dirs = {'.git', 'node_modules', '.dart_tool', '.idea', 'build', '.gradle'}
        for _root, _dirs, _files in _os.walk(CODEBASE_ROOT):
            _dirs[:] = [d for d in _dirs if d not in _skip_dirs and not d.startswith('.')]
            for _fname in _files:
                if _fname.lower().endswith('.rules'):
                    _full = _os.path.join(_root, _fname).replace("\\", "/")
                    _rel = _os.path.relpath(_full, CODEBASE_ROOT).replace("\\", "/")
                    if _rel in already_selected:
                        continue
                    _folder = str(_PPath(_rel).parent).replace("\\", "/")
                    if _folder == ".":
                        _folder = ""
                    if _folder not in selected_folders:
                        selected_folders[_folder] = {
                            "files": [],
                            "reasoning": "Security query: auto-included rules file",
                        }
                    if _rel not in selected_folders[_folder]["files"]:
                        selected_folders[_folder]["files"].append(_rel)
                        already_selected.add(_rel)

    # ── FIX 4: Intra-folder file filtering for large service folders ────────
    # When a folder has >3 files selected, drop files whose filename has zero
    # keyword overlap with the query — keeps relevant services, drops noise.
    if q_words:
        for folder, data in selected_folders.items():
            files = data["files"]
            if len(files) <= 3:
                continue
            scores = {f: _filename_score(f, q_words) for f in files}
            max_fscore = max(scores.values(), default=0)
            if max_fscore >= 1:
                # Drop zero-score files; always keep at least 2
                kept = [f for f in files if scores[f] > 0]
                if len(kept) < 2:
                    kept = sorted(files, key=lambda f: scores[f], reverse=True)[:2]
                data["files"] = kept

    # ── FIX 5: Deprioritise monolith index.* files when subfolder matches ──
    # index.js / index.ts in a parent folder are often 10K+ line monoliths.
    # If a relevant subdirectory was selected, remove the index file from the
    # parent to avoid sending the entire compiled bundle to the LLM.
    if q_words:
        subdir_folders = {f for f in selected_folders
                          if "/" in f and any(kw in f.lower() for kw in q_words)}
        if subdir_folders:
            for folder, data in selected_folders.items():
                data["files"] = [
                    f for f in data["files"]
                    if not (
                        _re.search(r'/index\.[jt]s$', f.lower())
                        and any(f.lower().startswith(sf.rsplit("/", 1)[0] + "/")
                                for sf in subdir_folders)
                    )
                ]
            # Clean up folders that became empty
            empty = [f for f, d in selected_folders.items() if not d["files"]]
            for f in empty:
                del selected_folders[f]

    # ── Feature-path relevance filter ──────────────────────────────────────
    # If the query matches a specific feature folder (lib/features/X), remove
    # files from unrelated feature folders that the Scout included by mistake.
    if q_words:
        all_files_flat = [f for d in selected_folders.values() for f in d["files"]]
        max_score = max((_feature_score(f, q_words) for f in all_files_flat), default=0)
        if max_score >= 2:
            to_remove = [
                folder for folder, data in selected_folders.items()
                if max((_feature_score(f, q_words) for f in data["files"]), default=0) == 0
            ]
            for folder in to_remove:
                del selected_folders[folder]

    # ── Minimum context expansion ───────────────────────────────────────────
    # If fewer than 3 files survived, scan all indexed files for keyword path
    # matches and add them — ensures the LLM always gets enough context.
    total_selected = sum(len(d["files"]) for d in selected_folders.values())
    if q_words and total_selected < 3:
        meaningful = q_words - _STOP
        already = {f for d in selected_folders.values() for f in d["files"]}
        for folder, folder_files in all_folders.items():
            for fp in folder_files:
                if fp in already:
                    continue
                fp_words = set(_re.findall(r'[a-z]+', fp.lower()))
                if any(kw in fp_words for kw in meaningful):
                    if folder not in selected_folders:
                        selected_folders[folder] = {"files": [], "reasoning": "Expanded: path keyword match"}
                    if fp not in selected_folders[folder]["files"]:
                        selected_folders[folder]["files"].append(fp)
                        already.add(fp)

    # Build per-folder reasoning from annotations + import graph
    for folder in selected_folders:
        selected_folders[folder]["files"] = sorted(selected_folders[folder]["files"])

        # Skip folders that already have annotation-based reasoning set above
        if selected_folders[folder]["reasoning"].startswith("Included because"):
            continue

        parts = []

        # Check user annotations for this folder (folder-level or any file in it)
        ann = (all_annotations.get(folder) or
               all_annotations.get(folder + "/") or
               all_annotations.get(folder + "\\"))
        if not ann:
            # Check if any file in this folder has an annotation
            for ann_p, ann_v in all_annotations.items():
                norm_p = ann_p.replace("\\", "/")
                if norm_p.startswith(folder + "/") or str(_PPath(norm_p).parent).replace("\\", "/") == folder:
                    ann = ann_v
                    break
        if ann:
            parts.append(f'your annotation says: "{ann}"')

        # Check import relationships from folder_map
        fm_entry = folder_map_folders.get(folder) or folder_map_folders.get(folder + "/")
        if fm_entry:
            imports_from = fm_entry.get("imports_from", [])
            imported_by = fm_entry.get("imported_by", [])
            if imports_from:
                parts.append(f"imports from {', '.join(imports_from[:3])}")
            if imported_by:
                parts.append(f"used by {', '.join(imported_by[:3])}")

        if parts:
            selected_folders[folder]["reasoning"] = (
                f"Scouted /{folder} because {' and '.join(parts)}"
            )
        else:
            selected_folders[folder]["reasoning"] = (
                f"Scouted /{folder} as relevant to: \"{body.user_query}\""
            )

    return {
        "selected_folders": selected_folders,   # {folder: {files: [], reasoning: ""}}
        "all_folders": all_folders,              # {folder: [file_paths]}
        "query": body.user_query,
        "scout_backend": scout_backend,
    }


def _load_prune_library_annotations(codebase_root: str) -> dict:
    """
    Read every ## section from prune library/*.md and return as
    {path_key: content} annotations ready for storage.update_annotation().

    This runs before every re-scan so the Scout LLM has the LLM's
    latest project knowledge (architecture, decisions, progress) as
    context during auto-annotation.
    """
    library_dir = Path(codebase_root) / "prune library"
    if not library_dir.exists():
        return {}

    annotations: dict = {}
    for md_file in sorted(library_dir.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue

        current_section: str | None = None
        current_lines: list[str] = []

        for line in text.splitlines():
            if line.startswith("## "):
                if current_section and current_lines:
                    content = "\n".join(current_lines).strip()
                    if content:
                        key = f"prune-library/{md_file.stem}/{current_section}"
                        annotations[key] = content[:400]
                current_section = line[3:].strip()
                current_lines = []
            elif current_section:
                current_lines.append(line)

        if current_section and current_lines:
            content = "\n".join(current_lines).strip()
            if content:
                key = f"prune-library/{md_file.stem}/{current_section}"
                annotations[key] = content[:400]

    return annotations


@app.post("/re-scan")
async def rescan_endpoint():
    """
    Scans the entire project for new/deleted/renamed files and rebuilds the skeleton.

    Pipeline (same whether triggered from dashboard or MCP watchdog):
      Step 1 — Read prune library docs → push as Scout annotations
      Step 2 — StorageManager.rescan_project():
                  clear skeleton + metadata
                  preserve user_annotations (includes prune library sections)
                  rebuild skeleton.json, folder_map.json, project_metadata.json
      Step 3 — Rebuild PruningEngine with fresh data
      Step 4 — Background: auto-annotate all files (Scout uses prune library context)
                  → auto_annotations.json complete = Knowledge Graph ready
    """
    global indexer, skeleton, pruner, _scan_status

    _scan_status.update({
        "stage": "loading_library", "message": "Reading prune library docs...",
        "files_found": 0, "symbols_found": 0,
        "annotated": 0, "total_to_annotate": 0,
        "started_at": time.time(), "finished_at": None,
    })

    # ── Step 1: Feed prune library into Scout annotations ────────────
    lib_annotations = _load_prune_library_annotations(CODEBASE_ROOT)
    if lib_annotations:
        for path_key, note in lib_annotations.items():
            storage.update_annotation(path_key, note)
        print(f"[re-scan] Loaded {len(lib_annotations)} prune library sections as Scout annotations")
    else:
        print("[re-scan] No prune library content found — scanning without library context")

    # ── Step 2: Full project rescan ──────────────────────────────────
    _scan_status.update({
        "stage": "scanning",
        "message": f"Indexing files in {CODEBASE_ROOT}...",
    })
    indexer = SkeletalIndexer(CODEBASE_ROOT, INDEX_PATH)
    scan_result = storage.rescan_project(indexer)

    # ── Step 3: Rebuild PruningEngine with fresh data ───────────────
    skeleton = indexer.load()
    if not skeleton:
        skeleton = indexer.index_and_save()

    _scan_status.update({
        "stage": "building_map",
        "message": f"Building folder map — {skeleton.file_count} files, {skeleton.total_symbols} symbols indexed",
        "files_found": skeleton.file_count,
        "symbols_found": skeleton.total_symbols,
    })

    pruner = PruningEngine(
        skeleton, CODEBASE_ROOT,
        annotations=storage.get_all_annotations(),
        scout=scout,
        folder_map=storage.folder_map,
    )

    await _broadcast({
        "type": "skeleton_updated",
        "total_symbols": skeleton.total_symbols,
        "file_count": skeleton.file_count,
    })

    # ── Step 4: Auto-annotate all files in background ────────────────
    if pruner and pruner.auto_annotator:
        reparsed = getattr(storage, "reparsed_files", set())
        asyncio.create_task(_annotate_all_files_background(pruner, reparsed))
        print(f"[re-scan] Auto-annotation started — {len(reparsed)} stale + new files to re-annotate")

    # ── Step 5: Rebuild terminal_context.md ─────────────────────────
    try:
        _build_kb_context()
        _refresh_prompt_assist_shared_context(reason="re-scan")
        print("[re-scan] terminal_context.md updated")
    except Exception as e:
        print(f"[re-scan] terminal_context.md update failed: {e}")

    return scan_result


async def _annotate_all_files_background(engine, reparsed_files: set = None) -> None:
    """
    Annotate all code files in the skeleton after a re-scan.
    - New files: annotated fresh
    - Reparsed files (changed since last scan): old annotation invalidated, re-annotated
    - Unchanged files: cache hit, skipped
    """
    try:
        from pruner.auto_annotator import AutoAnnotator as _AA
    except ImportError:
        try:
            from auto_annotator import AutoAnnotator as _AA
        except ImportError:
            return

    annotator = engine.auto_annotator
    folder_map = engine.folder_map or {}
    reparsed_files = reparsed_files or set()

    # Note: stale annotation invalidation handled by file-level hash in AutoAnnotator._should_regenerate()
    # No manual cache deletion needed — hash mismatch triggers regeneration automatically

    entries_by_file: dict = {}
    for entry in engine.skeleton.entries:
        entries_by_file.setdefault(entry.file_path, []).append(entry)

    # Collect only actual code files — skip markdown/docs (Scenario C)
    from indexer.models import SymbolKind
    _doc_kinds = {SymbolKind.HEADING, SymbolKind.SECTION, SymbolKind.FILE_REF}
    _skip_exts = {'.md', '.txt', '.rst', '.json', '.yaml', '.yml', '.toml', '.xml', '.html', '.css'}
    code_files = {
        fp: ents for fp, ents in entries_by_file.items()
        if os.path.splitext(fp)[1].lower() not in _skip_exts          # skip docs by extension
        and not all(e.kind in _doc_kinds for e in ents)               # skip pure markdown symbol kinds
    }

    if not code_files:
        print("[auto_annotator] All files already annotated, skipping batch.")
        _scan_status.update({
            "stage": "complete",
            "message": "Knowledge Graph ready — all files already annotated.",
            "finished_at": time.time(),
        })
        return

    total = len(code_files)
    print(f"[auto_annotator] Starting background annotation for {total} new files...")
    _scan_status.update({
        "stage": "annotating",
        "message": f"Auto-annotating {total} files for Knowledge Graph...",
        "total_to_annotate": total,
        "annotated": 0,
    })

    file_specs = []
    for fp, ents in code_files.items():
        symbols = _AA.build_file_data_context(fp, ents)
        # Include ALL code files: symbol-rich (Scenario A) AND zero-symbol (Scenario B)
        # Scenario C (pure markdown/unsupported) already excluded above
        file_specs.append({
            "file_path": fp,
            "symbols": symbols,  # empty string triggers Scenario B in annotator
        })

    total = len(file_specs)
    done = 0
    BATCH = 20
    for i in range(0, total, BATCH):
        batch = file_specs[i:i + BATCH]
        await asyncio.get_event_loop().run_in_executor(
            None, lambda b=batch: annotator.lazy_annotate_batch(b, folder_map)
        )
        done += len(batch)
        print(f"[auto_annotator] Annotated {done}/{total} files...")
        _scan_status.update({
            "annotated": done,
            "message": f"Auto-annotating... {done}/{total} files",
        })
        await asyncio.sleep(0.5)

    # Save scan timestamp only after ALL annotations complete
    from datetime import datetime, timezone
    completed_at = datetime.now(timezone.utc).isoformat()
    _save_last_scan_time(completed_at, skeleton.file_count, skeleton.total_symbols)

    _scan_status.update({
        "stage": "complete",
        "message": f"Knowledge Graph ready — {done} files annotated.",
        "annotated": done,
        "finished_at": time.time(),
    })

    # Broadcast updated timestamp to dashboard
    await _broadcast({
        "type": "skeleton_updated",
        "total_symbols": skeleton.total_symbols,
        "file_count": skeleton.file_count,
        "indexed_at": completed_at,
    })
    print(f"[auto_annotator] Batch annotation complete: {done} files annotated.")


@app.get("/scan-status")
async def scan_status_endpoint():
    """Live scan progress — polled by MCP server terminal during re-scan."""
    return _scan_status


class ApiKeySetup(BaseModel):
    anthropic_api_key: str = ""
    openai_api_key:    str = ""
    groq_api_key:      str = ""
    codebase_root:     str = ""


@app.post("/api/setup")
async def setup_endpoint(body: ApiKeySetup):
    """
    Save API keys and project root to the discovered project .env file.
    Called from the dashboard setup screen on first run.
    Keys never touch the binary — stored in the project tree.
    """
    env_file = _find_env_file(Path(CODEBASE_ROOT)) or (Path(CODEBASE_ROOT) / ".env")
    env_file.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    if body.anthropic_api_key.strip():
        lines.append(f"ANTHROPIC_API_KEY={body.anthropic_api_key.strip()}")
    if body.openai_api_key.strip():
        lines.append(f"OPENAI_API_KEY={body.openai_api_key.strip()}")
    if body.groq_api_key.strip():
        lines.append(f"GROQ_API_KEY={body.groq_api_key.strip()}")
    if body.codebase_root.strip():
        lines.append(f"PRUNE_CODEBASE_ROOT={body.codebase_root.strip()}")

    if not lines:
        return JSONResponse({"error": "No values provided"}, status_code=400)

    # Merge with existing .env — update keys that are provided, keep the rest
    existing: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    for line in lines:
        k, _, v = line.partition("=")
        existing[k.strip()] = v.strip()

    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n",
        encoding="utf-8",
    )

    print(f"[setup] API keys saved to {env_file}", flush=True)
    return {
        "status":   "saved",
        "env_file": str(env_file),
        "keys_set": list(existing.keys()),
        "best_practices": [
            {
                "title": "Disable built-in codebase context in your IDE",
                "detail": (
                    "Cursor, Continue.dev, and similar tools have a built-in "
                    "'Add codebase context' or '@codebase' toggle. "
                    "Turn it OFF when using PruneTool. "
                    "If both are active, the model receives duplicate context — "
                    "your full files from the IDE plus PruneTool's pruned version — "
                    "which wastes tokens and can produce conflicting answers."
                ),
                "affected_tools": ["Cursor", "Continue.dev", "GitHub Copilot Chat", "Cody"],
            },
            {
                "title": "Point only one AI endpoint at a time to the proxy",
                "detail": (
                    "Set your IDE's API base URL to http://localhost:8080/v1. "
                    "Remove or disable any other custom endpoint you had before. "
                    "Running two proxies in parallel will split requests unpredictably."
                ),
            },
            {
                "title": "Run a Project Scan after setup",
                "detail": (
                    "Open http://localhost:8000 and click 'Scan Project' once. "
                    "This builds the symbol index and folder map that the proxy "
                    "uses to prune context. Without a scan, the proxy passes "
                    "requests through without any context injection."
                ),
            },
        ],
    }


@app.get("/api/setup/status")
async def setup_status_endpoint():
    """Check which keys are configured — returns key names only, never values."""
    env_file = _find_env_file(Path(CODEBASE_ROOT))
    if env_file is None or not env_file.exists():
        return {"configured": False, "keys": []}
    keys = []
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k:
                keys.append(k)
    return {"configured": bool(keys), "keys": keys}


@app.get("/context-version")
async def context_version_endpoint():
    """
    Returns current context version and per-section hashes.
    MCP describe_project calls this to check if LLM context is stale.
    """
    return {
        "version":    _context_version["version"],
        "updated_at": _context_version["updated_at"],
        "sections":   _context_version["sections"],
    }


class RescanNeededBody(BaseModel):
    reason: str = "Prune library updated"

@app.post("/rescan-needed")
async def rescan_needed_endpoint(body: RescanNeededBody):
    """
    Called by the MCP watchdog when prune library docs change.
    Broadcasts a notification to all connected dashboard clients via WebSocket
    so the user can manually trigger a rescan.
    """
    msg = {"type": "rescan_needed", "reason": body.reason}
    dead = []
    for ws in connected_websockets:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_websockets.remove(ws)
    return {"notified": len(connected_websockets)}





@app.post("/search")
async def search_endpoint(body: SearchRequestBody):
    """Search the skeletal index."""
    if not skeleton:
        raise HTTPException(status_code=503, detail="Index not loaded")

    results = skeleton.search(body.query, top_k=body.top_k)
    return {
        "results": [e.to_dict() for e in results],
        "count": len(results),
    }


def _save_last_scan_time(indexed_at: str, file_count: int, total_symbols: int):
    """Persist last scan timestamp to .prunetool/last_scan.json."""
    import json as _json
    path = os.path.join(CODEBASE_ROOT, ".prunetool", "last_scan.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({
                "indexed_at": indexed_at,
                "file_count": file_count,
                "total_symbols": total_symbols,
            }, f)
    except OSError as e:
        print(f"[gateway] Could not save last_scan.json: {e}")


def _load_last_scan_time() -> dict:
    """Load last scan timestamp from .prunetool/last_scan.json."""
    import json as _json
    path = os.path.join(CODEBASE_ROOT, ".prunetool", "last_scan.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except (OSError, ValueError):
        return {}


@app.get("/skeleton")
async def skeleton_endpoint():
    """Get the current skeletal index summary."""
    if not skeleton:
        raise HTTPException(status_code=503, detail="Index not loaded")

    # Return summary, not full entries (could be large)
    files: dict[str, int] = {}
    for entry in skeleton.entries:
        files[entry.file_path] = files.get(entry.file_path, 0) + 1

    last_scan = _load_last_scan_time()
    return {
        "root_path": skeleton.root_path,
        "file_count": skeleton.file_count,
        "total_symbols": skeleton.total_symbols,
        "indexed_at": last_scan.get("indexed_at") or skeleton.indexed_at,
        "files": files,
    }


@app.get("/mindmap")
async def mindmap_endpoint():
    """
    Get the complete project mindmap showing hierarchical structure.
    
    Returns a tree with:
    - Modules (files)
    - Classes and interfaces
    - Methods and functions
    - Dependencies between modules
    """
    if not skeleton:
        raise HTTPException(status_code=503, detail="Index not loaded")

    generator = MindmapGenerator(skeleton, CODEBASE_ROOT)
    mindmap_root = generator.generate()

    return mindmap_root.to_dict()


@app.get("/mindmap/summary")
async def mindmap_summary_endpoint():
    """
    Get a high-level summary of the project structure.
    
    Returns statistics about modules, classes, functions, and dependencies.
    Good for quick architecture overview.
    """
    if not skeleton:
        raise HTTPException(status_code=503, detail="Index not loaded")

    generator = MindmapGenerator(skeleton, CODEBASE_ROOT)
    mindmap_root = generator.generate()
    summary = generate_mindmap_summary(mindmap_root)

    return summary


@app.get("/graph")
async def graph_endpoint():
    """
    Get the folder dependency graph in react-flow format.

    Returns nodes (folders) and edges (import dependencies) ready for
    @xyflow/react rendering. Built from folder_map.json which is
    generated on scan by reading first 40 lines of every file.
    """
    if not storage or not storage.folder_map:
        raise HTTPException(status_code=503, detail="Folder map not built yet. Run /re-scan first.")

    folder_map = storage.folder_map
    folders = dict(folder_map.get("folders", {}))
    raw_annotations = storage.get_all_annotations()

    # Ensure EVERY folder from the skeleton appears in the graph
    # (folder_mapper skips .dart_tool, etc. but skeleton indexes them)
    if skeleton:
        for entry in skeleton.entries:
            rel_dir = os.path.dirname(entry.file_path).replace("\\", "/") or "(root)"
            if rel_dir not in folders:
                folders[rel_dir] = {
                    "files": [],
                    "file_count": 0,
                    "extensions": {},
                    "imports_from": [],
                    "imported_by": [],
                }
            # Track files for skeleton-only folders
            fname = os.path.basename(entry.file_path)
            if fname not in folders[rel_dir].get("files", []):
                folders[rel_dir].setdefault("files", []).append(fname)
                folders[rel_dir]["file_count"] = len(folders[rel_dir]["files"])
                ext = os.path.splitext(fname)[1]
                if ext:
                    folders[rel_dir].setdefault("extensions", {})[ext] = folders[rel_dir].get("extensions", {}).get(ext, 0) + 1

    # Build folder→annotations map: match both file-level and folder-level annotations
    # User might annotate "lib/main.dart" (file) or "lib/core/services" (folder)
    # We aggregate all annotations that belong to a folder onto that folder's node
    folder_annotations: dict[str, list[str]] = {}
    for anno_path, note in raw_annotations.items():
        # Normalize backslashes to forward slashes
        norm_path = anno_path.replace("\\", "/")
        # Check if it's a file path (has extension) → use parent folder
        if "." in norm_path.split("/")[-1]:
            folder_key = "/".join(norm_path.split("/")[:-1]) or "(root)"
            file_name = norm_path.split("/")[-1]
            folder_annotations.setdefault(folder_key, []).append(f"{file_name}: {note}")
        else:
            # Direct folder annotation
            folder_annotations.setdefault(norm_path, []).append(note)

    nodes = []
    edges = []

    for folder_path, info in folders.items():
        # Node label = last path segment (or full if short)
        parts = folder_path.split("/")
        label = parts[-1] if len(parts) > 1 else folder_path
        parent_path = "/".join(parts[:-1]) if len(parts) > 1 else ""

        # Get all annotations for this folder (could be multiple files annotated)
        anno_list = folder_annotations.get(folder_path, [])
        annotation_text = " | ".join(anno_list) if anno_list else ""

        nodes.append({
            "id": folder_path,
            "type": "folderNode",
            "position": {"x": 0, "y": 0},  # Layout computed on frontend via dagre
            "data": {
                "label": label,
                "fullPath": folder_path,
                "parentPath": parent_path,
                "fileCount": info.get("file_count", 0),
                "extensions": info.get("extensions", {}),
                "files": info.get("files", []),
                "importsFrom": info.get("imports_from", []),
                "importedBy": info.get("imported_by", []),
                "annotation": annotation_text,
            },
        })

    # Edges from the pre-computed edge list (with weights)
    for edge in folder_map.get("edges", []):
        src = edge["from"]
        dst = edge["to"]
        edges.append({
            "id": f"e-{src}-{dst}",
            "source": src,
            "target": dst,
            "data": {"weight": edge.get("weight", 1)},
            "animated": False,
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": folder_map.get("stats", {}),
    }


@app.get("/annotations")
async def get_annotations_endpoint():
    """Get all module annotations."""
    if not annotations_manager:
        raise HTTPException(status_code=503, detail="Annotations service not initialized")

    return annotations_manager.to_dict()


@app.post("/annotations")
async def set_annotation_endpoint(body: AnnotationSetBody):
    """
    Save a user-defined context pin in real-time.

    Updates both StorageManager (authority) and legacy AnnotationsManager.
    Immediately refreshes the pruner so the Scout sees the new context.
    """
    global pruner

    if not storage:
        raise HTTPException(status_code=503, detail="Storage manager not initialized")

    # StorageManager is the authority — saves to disk immediately
    success = storage.update_annotation(body.file_path, body.annotation)

    # Sync to legacy annotations manager (used by cache_stabilizer)
    if annotations_manager:
        annotations_manager.set_annotation(body.file_path, body.annotation)

    # Refresh pruner so Scout sees updated annotations immediately
    if success and skeleton:
        pruner = PruningEngine(
            skeleton, CODEBASE_ROOT,
            annotations=storage.get_all_annotations(),
            scout=scout,
            folder_map=storage.folder_map,
        )

    return {
        "status": "ok" if success else "error",
        "file_path": body.file_path,
        "annotation": body.annotation,
        "total_annotations": len(storage.user_annotations),
    }


class AutoAnnotationSetBody(BaseModel):
    file_path: str = Field(..., description="Relative file path")
    annotation: str = Field(default="", description="User-edited annotation text")


@app.get("/auto-annotations")
async def get_auto_annotations():
    """Return all auto-generated (and user-edited) file annotations from auto_annotations.json."""
    if not pruner or not pruner.auto_annotator:
        raise HTTPException(status_code=503, detail="Auto-annotator not initialized")

    annotations = pruner.auto_annotator.all_annotations()

    # Build folder → files → annotation structure for the Project tab
    folders: dict = {}
    for file_path, annotation in annotations.items():
        fp = file_path.replace("\\", "/")
        folder = "/".join(fp.split("/")[:-1]) or "(root)"
        filename = fp.split("/")[-1]
        folders.setdefault(folder, {})[filename] = {"file_path": fp, "annotation": annotation}

    return {
        "annotations": annotations,
        "by_folder": folders,
        "total": len(annotations),
    }


@app.post("/auto-annotations")
async def set_auto_annotation(body: AutoAnnotationSetBody):
    """Save a user-edited annotation directly into auto_annotations.json."""
    if not pruner or not pruner.auto_annotator:
        raise HTTPException(status_code=503, detail="Auto-annotator not initialized")

    annotator = pruner.auto_annotator
    fp = body.file_path.strip()
    if not fp:
        raise HTTPException(status_code=400, detail="file_path is required")

    if body.annotation.strip():
        annotator._cache[fp] = body.annotation.strip()
    elif fp in annotator._cache:
        del annotator._cache[fp]

    annotator._dirty = True
    annotator._save_cache()

    return {"status": "ok", "file_path": fp, "annotation": body.annotation.strip()}


@app.get("/api/burned-stats")
async def burned_stats_endpoint():
    """Return live Bifrost token metrics."""
    return burned_stats


@app.post("/api/mcp-log")
async def mcp_log_endpoint(request: Request):
    """Receive log messages from mcp_stdio.py and print them to this terminal."""
    try:
        body = await request.json()
        level = body.get("level", "info").upper()
        msg   = body.get("msg", "")
        tool  = body.get("tool", "")
        prefix = f"[stdio/{tool}]" if tool else "[stdio]"
        print(f"{prefix} {level}  {msg}", flush=True)
        if tool == "describe_project":
            _refresh_prompt_assist_shared_context(reason="describe_project")
    except Exception:
        pass
    return {"ok": True}


# ── LLM config read/write ────────────────────────────────────────────

def _parse_llm_finder_gateway(path: str) -> list:
    """Parse llms_prunetoolfinder.js and return models list."""
    import re as _re
    try:
        text = open(path, encoding="utf-8").read()
        if _re.search(r"(?m)^\s*provider:\s*", text):
            text = _re.sub(r"(?m)^(\s*)provider:\s*", r"\1// provider: ", text, count=1)
        text = _re.sub(r'(?<!:)(?<!\\)//[^\n]*', "", text)
        text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.DOTALL)
        text = _re.sub(r"^\s*module\.exports\s*=\s*", "", text.strip())
        text = text.rstrip(";").strip()
        text = _re.sub(r",\s*([}\]])", r"\1", text)
        text = _re.sub(r'(?<=[{,])\s*(\w+)\s*:', lambda m: f' "{m.group(1)}":', text)
        text = _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        data = json.loads(text)
        return data.get("models", [])
    except Exception:
        return []


@app.get("/api/llm-config")
async def get_llm_config():
    """Return models from llms_prunetoolfinder.js."""
    finder = os.path.join(CODEBASE_ROOT, "llms_prunetoolfinder.js")
    models = _parse_llm_finder_gateway(finder)
    return {"models": models, "path": finder}


@app.post("/api/llm-config")
async def save_llm_config(request: Request):
    """
    Write updated models back to llms_prunetoolfinder.js.
    Body: { "models": [ {id, label, model, complexity, dailyTokenGoal}, ... ] }
    """
    body = await request.json()
    models = body.get("models", [])
    finder = os.path.join(CODEBASE_ROOT, "llms_prunetoolfinder.js")

    # Read current file
    try:
        current = open(finder, encoding="utf-8").read()
    except Exception:
        current = ""

    # Rebuild models array block
    model_lines = ["  models: [\n"]
    for m in models:
        goal = int(m.get("dailyTokenGoal") or 0)
        model_lines.append(
            f'    {{ id: "{m["id"]}", label: "{m["label"]}", '
            f'model: "{m["model"]}", complexity: "{m["complexity"]}", '
            f'dailyTokenGoal: {goal} }},\n'
        )
    model_lines.append("  ]\n")
    models_block = "".join(model_lines)

    # Replace only the models: [ ... ] section, preserving everything else
    # (autoBuffer, header comments, etc.)
    import re as _re
    new_content = _re.sub(
        r'models:\s*\[.*?\]',
        models_block.strip(),
        current,
        flags=_re.DOTALL,
    )
    # Fallback: if regex didn't match, just rewrite the whole file
    if new_content == current and models:
        new_content = current

    try:
        with open(finder, "w", encoding="utf-8") as f:
            f.write(new_content)
        return {"status": "ok", "models_saved": len(models)}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/model-usage")
async def model_usage_endpoint(period: str = "today"):
    """
    Aggregate token_log.jsonl by model for a given time period.
    period: today | 7d | 30d | all
    """
    token_log_path = os.path.join(CODEBASE_ROOT, "token_log.jsonl")
    if not os.path.exists(token_log_path):
        return {"models": [], "total_tokens": 0, "period": period}

    now = time.time()
    cutoff = {
        "1h":    now - 1  * 3600,
        "3h":    now - 3  * 3600,
        "today": now - (now % 86400),           # midnight UTC
        "2d":    now - 2  * 86400,
        "7d":    now - 7  * 86400,
        "30d":   now - 30 * 86400,
        "all":   0,
    }.get(period, 0)

    from collections import defaultdict
    model_tokens          = defaultdict(int)
    model_sessions        = defaultdict(list)
    model_input_tokens    = defaultdict(int)
    model_output_tokens   = defaultdict(int)
    model_cached_tokens   = defaultdict(int)

    try:
        with open(token_log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e    = json.loads(line)
                    ts   = float(e.get("ts", 0))
                    if ts < cutoff:
                        continue
                    mdl  = e.get("model", "unknown") or "unknown"
                    # Support both old (tokens) and new (input_tokens + output_tokens) format
                    inp  = int(e.get("input_tokens",  0))
                    out  = int(e.get("output_tokens", 0))
                    if inp == 0 and out == 0:
                        inp = int(e.get("tokens", 0))
                    cached = int(e.get("cached_input_tokens", 0))
                    # effective_tokens accounts for cache discount (cached = 0.1x cost)
                    effective = int(e.get("effective_tokens") or (inp + out - int(cached * 0.9)))
                    model_tokens[mdl]         += effective
                    model_input_tokens[mdl]   += inp
                    model_output_tokens[mdl]  += out
                    model_cached_tokens[mdl]  += cached
                    model_sessions[mdl].append(ts)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return {"models": [], "total_tokens": 0, "period": period}

    models = []
    for mdl, tokens in sorted(model_tokens.items(), key=lambda x: -x[1]):
        tss = sorted(model_sessions[mdl])
        active_secs = 0
        for i in range(1, len(tss)):
            gap = tss[i] - tss[i - 1]
            if gap <= 300:
                active_secs += gap

        models.append({
            "model":          mdl,
            "tokens":         tokens,   # effective (cache-adjusted) — used for goal %
            "input_tokens":   model_input_tokens[mdl],
            "output_tokens":  model_output_tokens[mdl],
            "cached_tokens":  model_cached_tokens[mdl],
            "calls":         len(tss),
            "active_secs":   int(active_secs),
            "active_mins":   round(active_secs / 60, 1),
            "active_hrs":    round(active_secs / 3600, 2),
            "first_seen":    tss[0]  if tss else None,
            "last_seen":     tss[-1] if tss else None,
        })

    return {"models": models, "total_tokens": sum(model_tokens.values()), "period": period}


def _read_terminal_context_snapshot(max_chars: int = 12000) -> str:
    """Read shared terminal_context.md produced by PruneTool scan."""
    ctx_path = Path(CODEBASE_ROOT) / ".prunetool" / "terminal_context.md"
    if not ctx_path.exists():
        return ""
    try:
        raw = ctx_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    compact = " ".join(raw.split())
    return compact[:max_chars]


def _refresh_prompt_assist_shared_context(reason: str = "") -> None:
    """Refresh in-memory prompt-assist cache from terminal_context.md."""
    text = _read_terminal_context_snapshot()
    if not text:
        return
    _prompt_assist_shared_context["text"] = text
    _prompt_assist_shared_context["updated_at"] = time.time()
    _prompt_assist_shared_context["source"] = str(Path(CODEBASE_ROOT) / ".prunetool" / "terminal_context.md")
    _prompt_assist_shared_context["last_reason"] = reason or "refresh"


def _infer_prompt_intent(user_input: str) -> tuple[str, str, str]:
    """Infer task type and prompt posture from rough user input."""
    text = (user_input or "").strip().lower()
    if any(word in text for word in ["bug", "fix", "error", "fail", "issue", "broken", "crash"]):
        return (
            "bug investigation / possible code fix",
            "Identify root cause first, then apply the smallest safe fix.",
            "minimal-fix",
        )
    if any(word in text for word in ["add", "implement", "create", "build", "support"]):
        return (
            "feature implementation",
            "Find the entry point first and follow existing project patterns.",
            "implementation",
        )
    if any(word in text for word in ["explain", "understand", "walk through", "how does", "why does"]):
        return (
            "code explanation",
            "Explain behavior first, then cite concrete files and modules.",
            "explain-first",
        )
    if any(word in text for word in ["refactor", "cleanup", "simplify", "restructure", "rename"]):
        return (
            "refactor",
            "Preserve behavior unless a change is explicitly required.",
            "safe-refactor",
        )
    if any(word in text for word in ["test", "coverage", "spec"]):
        return (
            "test work",
            "Focus on a narrow test scope that proves behavior.",
            "test-first",
        )
    return (
        "general engineering task",
        "Clarify assumptions, keep scope narrow, and cite concrete files.",
        "balanced",
    )


def _read_recent_library_notes(limit: int = 2) -> list[str]:
    """Return short snippets from recently updated prune library notes."""
    lib_dir = Path(CODEBASE_ROOT) / "prune library"
    if not lib_dir.exists():
        return []

    notes: list[str] = []
    ordered = sorted(lib_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    for md in ordered:
        try:
            raw = md.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not raw:
            continue
        compact = " ".join(raw.split())
        notes.append(f"{md.stem}: {compact[:220]}")
    return notes


def _estimate_tokens(text: str) -> int:
    """Rough token estimate used only for reporting."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _top_project_areas(limit: int = 4) -> list[str]:
    """Return the top indexed folders from cached project metadata."""
    if storage and storage.project_metadata and storage.project_metadata.directory_tree:
        ordered = sorted(
            storage.project_metadata.directory_tree.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        return [name for name, _ in ordered[:limit]]

    if storage and storage.folder_map:
        return list((storage.folder_map.get("folders") or {}).keys())[:limit]

    return []


def _build_prompt_assist_payload(user_input: str, model: str, mode: str) -> dict:
    """Build prompt suggestion using cached index, annotations, and library notes."""
    if not skeleton:
        raise HTTPException(status_code=503, detail="Project context is not ready. Run Scan Project first.")

    if not _prompt_assist_shared_context.get("text"):
        _refresh_prompt_assist_shared_context(reason="prompt_assist_request")

    intent, guidance, style = _infer_prompt_intent(user_input)
    hits = skeleton.search(user_input, top_k=10) if user_input.strip() else []

    seen_files: set[str] = set()
    relevant_context: list[str] = []
    relevant_files: list[str] = []
    for entry in hits:
        rel_path = entry.file_path.replace("\\", "/")
        if rel_path in seen_files:
            continue
        seen_files.add(rel_path)
        label = entry.name
        if entry.kind.value not in {"heading", "section", "file_ref"}:
            label = f"{entry.name} ({entry.kind.value})"
        relevant_context.append(f"{rel_path} - {label}")
        relevant_files.append(rel_path)
        if len(relevant_context) >= 4:
            break

    annotation_notes: list[str] = []
    if storage:
        annotations = storage.get_all_annotations()
        for rel_path in relevant_files:
            note = annotations.get(rel_path)
            if not note:
                continue
            annotation_notes.append(f"{rel_path}: {' '.join(note.split())[:180]}")
            if len(annotation_notes) >= 2:
                break

    project_areas = _top_project_areas(limit=4)
    recent_notes = _read_recent_library_notes(limit=2)
    recent_queries = [item.get("query", "") for item in prune_history[-3:] if item.get("query")]

    if relevant_context:
        focus_line = "Focus on " + ", ".join(x.split(" - ", 1)[0] for x in relevant_context[:3]) + "."
    elif project_areas:
        focus_line = "Focus on likely modules: " + ", ".join(project_areas[:3]) + "."
    else:
        focus_line = "Use indexed project context to identify the right modules."

    shared_hint = "Use the cached describe_project context already loaded in this session."
    if not _prompt_assist_shared_context.get("text"):
        shared_hint = "If available, call describe_project and use that shared context."

    suggested_prompt = " ".join([
        user_input.strip().rstrip(".") + ".",
        f"This is a {intent.lower()} in the current project.",
        focus_line,
        guidance,
        shared_hint,
        "Use current project context and mention exact files or modules used.",
        "Return a concise outcome summary.",
    ]).strip()

    summary_bits = [
        f"Target project: {CODEBASE_ROOT}",
        f"Indexed files: {skeleton.file_count}",
        f"Indexed symbols: {skeleton.total_symbols}",
    ]
    if project_areas:
        summary_bits.append("Top areas: " + ", ".join(project_areas))

    shared_context_text = _prompt_assist_shared_context.get("text", "") or ""
    estimated_input_tokens = (
        _estimate_tokens(user_input)
        + _estimate_tokens(shared_context_text[:400])
        + sum(_estimate_tokens(item) for item in relevant_context)
        + sum(_estimate_tokens(item) for item in annotation_notes)
        + sum(_estimate_tokens(item) for item in recent_notes)
    )
    estimated_output_tokens = _estimate_tokens(suggested_prompt)

    return {
        "project_root": CODEBASE_ROOT,
        "intent": intent,
        "mode": mode,
        "model_used": model,
        "prompt_style": style,
        "suggested_prompt": suggested_prompt,
        "project_summary": " | ".join(summary_bits),
        "relevant_context": relevant_context,
        "annotation_notes": annotation_notes,
        "recent_notes": recent_notes,
        "recent_queries": recent_queries,
        "cache_warm": bool(storage and (storage.folder_map or storage.get_all_annotations())),
        "shared_context_loaded": bool(_prompt_assist_shared_context.get("text")),
        "shared_context_excerpt": _prompt_assist_shared_context.get("text", "")[:600],
        "shared_context_updated_at": _prompt_assist_shared_context.get("updated_at"),
        "generation_report": {
            "selected_preset": model,
            "mode": mode,
            "backend_llm_calls": 0,
            "backend_models_used": [],
            "actual_llm_tokens": 0,
            "estimated_prompt_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "estimated_total_tokens": estimated_input_tokens + estimated_output_tokens,
            "implementation": "deterministic prompt builder over cached project context",
            "note": "Prompt Assist does not call an LLM yet; the dropdown is a preset label only.",
        },
        "history_count": len(prune_history),
    }


@app.get("/api/prompt-assist/status")
async def prompt_assist_status():
    """Status + cache metadata for prompt assist UI."""
    if not _prompt_assist_shared_context.get("text"):
        _refresh_prompt_assist_shared_context(reason="status_poll")
    return {
        "connected": skeleton is not None,
        "project_root": CODEBASE_ROOT,
        "cache_warm": bool(storage and (storage.folder_map or storage.get_all_annotations())),
        "indexed_files": skeleton.file_count if skeleton else 0,
        "indexed_symbols": skeleton.total_symbols if skeleton else 0,
        "shared_context_loaded": bool(_prompt_assist_shared_context.get("text")),
        "shared_context_updated_at": _prompt_assist_shared_context.get("updated_at"),
        "shared_context_source": _prompt_assist_shared_context.get("source"),
        "shared_context_reason": _prompt_assist_shared_context.get("last_reason"),
        "shared_context_excerpt": _prompt_assist_shared_context.get("text", "")[:400],
        "top_areas": _top_project_areas(limit=4),
        "recent_notes": _read_recent_library_notes(limit=2),
    }


@app.post("/api/prompt-assist")
async def prompt_assist_endpoint(body: PromptAssistBody):
    """Generate a context-aware prompt suggestion from rough user text."""
    text = body.user_input.strip()
    if not text:
        raise HTTPException(status_code=400, detail="user_input is required")
    return _build_prompt_assist_payload(text, body.model, body.mode)


@app.get("/stats")
async def stats_endpoint():
    """Get gateway statistics."""
    cache_stats = cache_stabilizer.get_cache_stats() if cache_stabilizer else {}
    return {
        "skeleton": {
            "file_count": skeleton.file_count if skeleton else 0,
            "total_symbols": skeleton.total_symbols if skeleton else 0,
        },
        "cache": cache_stats,
        "history": prune_history[-10:],
        "history_count": len(prune_history),
    }


@app.get("/config")
async def get_config():
    """Get current configuration."""
    return {
        "codebase_root": CODEBASE_ROOT,
        "index_path": INDEX_PATH,
        "cache": cache_stabilizer.config.__dict__ if cache_stabilizer else {},
    }


@app.post("/config")
async def update_config(body: ConfigBody):
    """Update gateway configuration."""
    if cache_stabilizer:
        if body.cache_type:
            cache_stabilizer.config.cache_type = body.cache_type
        if body.provider:
            cache_stabilizer.config.provider = body.provider
        if body.max_system_tokens:
            cache_stabilizer.config.max_system_tokens = body.max_system_tokens
        if body.max_code_tokens:
            cache_stabilizer.config.max_code_tokens = body.max_code_tokens

    return {"status": "updated", "config": cache_stabilizer.config.__dict__}


# -------------------------------------------------------------------
# KB Context — writes CLAUDE.md on gateway startup so VS Code
# terminal (Claude Code or any LLM) has full project context.
# -------------------------------------------------------------------


def _build_kb_context() -> dict:
    """
    Read .prunetool/ JSON files + prune library/*.md.
    Writes full content to .prunetool/terminal_context.md so any LLM
    (Claude Code, etc.) can read the actual knowledge — not just file paths.
    Also updates CLAUDE.md with a PruneTool section so Claude Code picks
    it up automatically on startup.
    """
    files_available = []
    context_blocks  = []

    # ── 1. Knowledge Graph (folder_map.json) — primary structure ─────
    fm_path = Path(CODEBASE_ROOT) / ".prunetool" / "folder_map.json"
    if fm_path.exists():
        try:
            fm      = json.loads(fm_path.read_text(encoding="utf-8"))
            folders = list(fm.get("folders", {}).keys())
            edges   = fm.get("edges", [])
            files_available.append(str(fm_path))
            # Pull per-folder file lists if present
            folder_detail = fm.get("folders", {})
            folder_lines = []
            for f in sorted(folders):
                detail = folder_detail.get(f, {})
                file_count = len(detail.get("files", []))
                folder_lines.append(f"  - {f}  ({file_count} files)" if file_count else f"  - {f}")
            edge_lines = "\n".join(
                f"  - {e.get('from', e.get('source',''))} → {e.get('to', e.get('target',''))}  (weight: {e.get('weight','')})"
                for e in edges
            )
            # Project stats from skeleton.json (summary only, no bloat)
            sk_path = Path(INDEX_PATH)
            stats_line = ""
            if sk_path.exists():
                try:
                    sk = json.loads(sk_path.read_text(encoding="utf-8"))
                    entries = sk.get("entries", [])
                    stats_line = (
                        f"- Root: {sk.get('root_path', str(CODEBASE_ROOT))}\n"
                        f"- Files indexed: {len(set(e.get('file_path','') for e in entries))}\n"
                        f"- Symbols indexed: {len(entries)}\n"
                    )
                    files_available.append(str(sk_path))
                except Exception:
                    pass
            context_blocks.append(
                f"## Knowledge Graph — Folder Map\n"
                f"{stats_line}"
                f"### Folders ({len(folders)} total)\n"
                + "\n".join(folder_lines) + "\n\n"
                f"### Cross-folder Import Relationships ({len(edges)} edges)\n"
                f"{edge_lines}\n"
            )
        except Exception:
            pass
    else:
        context_blocks.append("## Knowledge Graph\n- Not scanned yet — click Scan Project\n")

    # ── 2. Auto Annotations — used by Scout internally, not inlined for LLM ──
    # Annotations are shallow for complex files (Claude's assessment).
    # Scout still uses them at query time via the pruning engine.
    # We only register the file path so it shows in the KB panel.
    aa_path = Path(CODEBASE_ROOT) / ".prunetool" / "auto_annotations.json"
    if aa_path.exists():
        files_available.append(str(aa_path))

    # ── 3. Prune Library docs — sorted by last modified, newest first ──
    lib_dir  = Path(CODEBASE_ROOT) / "prune library"
    lib_docs = []
    if lib_dir.exists():
        ordered = sorted(lib_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
        for md in ordered:
            lib_docs.append(md.name)
            files_available.append(str(md))
            try:
                mtime   = time.strftime("%Y-%m-%d %H:%M", time.localtime(md.stat().st_mtime))
                content = md.read_text(encoding="utf-8")
                context_blocks.append(f"## {md.stem}  [prune library/{md.name}]\n{content}\n_(updated {mtime})_\n")
            except Exception:
                pass

    # ── 4. README.md ──────────────────────────────────────────────────
    for name in ("README.md", "readme.md"):
        readme = Path(CODEBASE_ROOT) / name
        if readme.exists():
            files_available.append(str(readme))
            try:
                content = readme.read_text(encoding="utf-8")[:5000]
                context_blocks.append(f"## README.md\n{content}\n")
            except Exception:
                pass
            break

    # ── Write terminal_context.md with FULL content ───────────────────
    ctx_path = Path(CODEBASE_ROOT) / ".prunetool" / "terminal_context.md"
    full_context = (
        "# PruneTool Knowledge Base — Terminal Session Context\n"
        "_Auto-generated on terminal session start. Contains full KB for LLM context._\n\n"
        + "\n---\n\n".join(context_blocks)
    )
    try:
        ctx_path.write_text(full_context, encoding="utf-8")
    except Exception:
        pass

    # ── Update CLAUDE.md so Claude Code reads this automatically ──────
    _update_claude_md(ctx_path)

    # ── Write MCP config so Claude Code finds the PruneTool MCP server ─
    _ensure_mcp_config()

    summary_lines = [b.splitlines()[0].lstrip("# ") for b in context_blocks if b.strip()]

    return {
        "summary":      "\n".join(summary_lines),
        "files_list":   files_available,
        "lib_docs":     lib_docs,
        "context_path": str(ctx_path),
    }


def _update_claude_md(ctx_path: Path):
    """
    Write/update a PruneTool section in CLAUDE.md in the project root.
    Inlines the FULL terminal_context.md content so Claude Code has it
    immediately without needing to read any extra files.
    """
    claude_md = Path(CODEBASE_ROOT) / "CLAUDE.md"
    marker_start = "<!-- PRUNETOOL:START -->"
    marker_end   = "<!-- PRUNETOOL:END -->"

    # Read the actual context content to inline it (cap at 80K chars ~ 20K tokens)
    MAX_INLINE = 80_000
    try:
        kb_content = ctx_path.read_text(encoding="utf-8")
        if len(kb_content) > MAX_INLINE:
            kb_content = kb_content[:MAX_INLINE] + f"\n\n... (truncated — full context at `{ctx_path}`)\n"
    except Exception:
        kb_content = "(context not yet generated)"

    new_section = (
        f"{marker_start}\n"
        f"# IMPORTANT — PruneTool Knowledge Base (Read This First)\n\n"
        f"> **This project uses PruneTool. The knowledge base below is your PRIMARY context source.**\n"
        f"> Before answering any question about this codebase, treat the sections below as ground truth.\n"
        f"> Priority order: Knowledge Graph → File Annotations → Prune Library → README.\n\n"
        f"## Auto-Save Rule (follow without being asked)\n"
        f"Every 30 minutes of a session, AND at the end of every session:\n"
        f"1. Summarize what was discussed, built, or decided\n"
        f"2. Append a dated entry to `prune library/library.md`\n"
        f"3. Update the Current Status section in `prune library/PROGRESS.md`\n"
        f"4. Do this silently — no need to announce it, just do it\n\n"
        f"The prune library is your persistent memory across sessions. Keep it updated.\n\n"
        f"---\n\n"
        f"{kb_content}\n"
        f"{marker_end}"
    )

    try:
        if claude_md.exists():
            existing = claude_md.read_text(encoding="utf-8")
            if marker_start in existing:
                # Replace existing section
                import re
                updated = re.sub(
                    rf"{re.escape(marker_start)}.*?{re.escape(marker_end)}",
                    new_section,
                    existing,
                    flags=re.DOTALL,
                )
                claude_md.write_text(updated, encoding="utf-8")
            else:
                # Append section
                claude_md.write_text(existing.rstrip() + "\n\n" + new_section + "\n", encoding="utf-8")
        else:
            # Create fresh CLAUDE.md
            claude_md.write_text(new_section + "\n", encoding="utf-8")
    except Exception:
        pass


def _ensure_mcp_config():
    """
    Write MCP server config into .claude/settings.local.json so Claude Code
    automatically connects to the PruneTool MCP server (describe_project,
    get_surgical_context, analyze_complexity, report_tokens).
    """
    import json as _json
    claude_dir = Path(CODEBASE_ROOT) / ".claude"
    settings_path = claude_dir / "settings.local.json"
    try:
        claude_dir.mkdir(exist_ok=True)
        data = {}
        if settings_path.exists():
            try:
                data = _json.loads(settings_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        # Only write if not already configured
        servers = data.get("mcpServers", {})
        if "prunetool" not in servers:
            servers["prunetool"] = {"type": "http", "url": "http://localhost:8765/mcp"}
            data["mcpServers"] = servers
            settings_path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
            print(f"[gateway] MCP config written → {settings_path}")
        else:
            print(f"[gateway] MCP config already present in {settings_path}")
    except Exception as e:
        print(f"[gateway] Could not write MCP config: {e}")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket for live updates (skeleton changes, prune results)."""
    await ws.accept()
    connected_websockets.append(ws)

    try:
        # Send initial state
        if skeleton:
            await ws.send_json({
                "type": "init",
                "skeleton": {
                    "total_symbols": skeleton.total_symbols,
                    "file_count": skeleton.file_count,
                },
                "history": prune_history[-10:],
            })

        while True:
            data = await ws.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if ws in connected_websockets:
            connected_websockets.remove(ws)


@app.get("/")
async def serve_ui():
    """Serve the Webview UI."""
    index_path = os.path.join(UI_DIST_PATH, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)

    # Fallback: inline minimal UI if not built
    return HTMLResponse(FALLBACK_HTML)


# -------------------------------------------------------------------
# Fallback UI (shown when React app hasn't been built yet)
# -------------------------------------------------------------------

FALLBACK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Pruning Gateway</title>
    <style>
        body { font-family: system-ui; background: #0d1117; color: #c9d1d9; padding: 2rem; }
        h1 { color: #58a6ff; }
        .status { background: #161b22; padding: 1rem; border-radius: 8px; margin: 1rem 0; }
        code { color: #f0883e; }
    </style>
</head>
<body>
    <h1>Context-Aware Pruning Gateway</h1>
    <div class="status">
        <p>Gateway is running. Build the UI with:</p>
        <code>cd prunetool/ui && npm install && npm run build</code>
    </div>
    <div class="status">
        <p>API Endpoints:</p>
        <ul>
            <li><code>POST /prune</code> — Run pruning pipeline</li>
            <li><code>POST /index</code> — Re-index codebase</li>
            <li><code>POST /search</code> — Search skeleton</li>
            <li><code>GET /skeleton</code> — View skeleton summary</li>
            <li><code>GET /stats</code> — View statistics</li>
            <li><code>GET /docs</code> — OpenAPI docs</li>
        </ul>
    </div>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*70)
    print("Context-Aware Pruning Gateway")
    print("="*70)
    print(f"Host: http://127.0.0.1:8000")
    print(f"WebSocket: ws://127.0.0.1:8000/ws")
    print(f"API Docs: http://127.0.0.1:8000/docs")
    print(f"\nStarting FastAPI server...")
    print("="*70 + "\n")
    
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )

"""
prune_cli.py â€” PruneTool interactive CLI
=========================================
Usage:
  prune chat               Start interactive chat (auto-routes models)
  prune model <alias>      Lock to a specific model alias
  prune models             List configured models + daily usage
  prune status             Show gateway status + active model

The Broker picks the right LLM on every prompt:
  1. Groq llama-instant classifies prompt complexity (simple/medium/heavy)
  2. Checks daily token usage against dailyTokenGoal (warns at 90%, pivots at 95%)
  3. Checks pruned context size against model maxContext
  4. Falls back through fallback_order if primary model is unavailable
"""

from __future__ import annotations

import json
import os
import sys
import time
import datetime
import hashlib
import socket
import shutil as _shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

# â”€â”€ Paths â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_HERE = Path(__file__).resolve().parent
_ENV_SKIP_DIRS = {
    ".git", ".venv", "node_modules", "dist", "build", "cache", "__pycache__",
    ".next", ".dart_tool", ".firebase",
}


def _project_prunetool_dir() -> Path:
    root = _project_root_from_env()
    return (root if root is not None else Path.cwd()) / ".prunetool"


def _runtime_prunetool_dir() -> Path:
    root = _project_root_from_env()
    return (root if root is not None else Path.cwd()) / ".prunetool"


def _runtime_file(name: str) -> Path:
    return _runtime_prunetool_dir() / name


def _project_root_from_env(env: dict | None = None) -> Path | None:
    source = env or os.environ
    raw_root = (source.get("PRUNE_CODEBASE_ROOT") or "").strip()
    if not raw_root:
        return None
    return Path(raw_root).expanduser()


def _project_llm_config_path(env: dict | None = None) -> Path | None:
    root = _project_root_from_env(env)
    if root is None:
        return None
    return root / ".prunetool" / "llms_prunetoolfinder.js"


def _llm_config_paths(env: dict | None = None) -> list[Path]:
    # Prefer the target project's config, then the shipped default next to the binary.
    paths: list[Path] = []
    project_path = _project_llm_config_path(env)
    if project_path is not None:
        paths.append(project_path)
    paths.append(_HERE / "llms_prunetoolfinder.js")
    return paths


def _parse_env_file(path: Path) -> dict:
    env: dict = {}
    try:
        # Use utf-8-sig to automatically strip BOM if present
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                env[k] = v
    except Exception as e:
        print(f"[prune] WARNING: Could not read {path}: {e}", file=sys.stderr)
    return env


def _find_env_file(search_root: Path | None = None) -> Path | None:
    env_hint = os.environ.get("PRUNE_ENV_FILE", "").strip()
    if env_hint:
        hinted = Path(env_hint)
        if hinted.exists():
            return hinted

    root = search_root or Path.cwd()
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
        data = _parse_env_file(candidate)
        if data.get("PRUNE_CODEBASE_ROOT"):
            return candidate

    return min(candidates, key=lambda p: len(p.relative_to(root).parts))

GATEWAY_URL = "http://localhost:8000"
GATEWAY_TIMEOUT = 5.0


# â”€â”€ Env loader â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _load_env() -> dict:
    """
    Load .env from the project directory (current working directory).
    This is where prune.exe chat is being run from.
    NO fallback to a separate home-directory .env â€” project config only.
    """
    env_file = _find_env_file()
    if not env_file:
        return {}
    os.environ.setdefault("PRUNE_ENV_FILE", str(env_file))
    env = _parse_env_file(env_file)
    # Apply parsed env values to os.environ so they're available globally
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return env


def _get_key(provider: str, env: dict) -> Optional[str]:
    key_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "groq":      "GROQ_API_KEY",
        "gemini":    "GEMINI_API_KEY",
    }
    env_key = key_map.get(provider.lower())
    if not env_key:
        return None
    return env.get(env_key) or os.environ.get(env_key)


# â”€â”€ LLM Config loader â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
import re as _re

def _infer_provider(model_id: str) -> str:
    m = model_id.lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt-", "o1", "o3", "o4", "text-davinci")):
        return "openai"
    if m.startswith("gemini"):
        return "gemini"
    return "groq"


def _parse_js_config(text: str) -> dict:
    """Strip JS module syntax and comments, then parse the object as JSON."""
    # Remove line comments
    text = _re.sub(r"//[^\n]*", "", text)
    # Remove block comments
    text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.DOTALL)
    # Extract the object literal after module.exports =
    m = _re.search(r"module\.exports\s*=\s*(\{.*\})", text, flags=_re.DOTALL)
    if not m:
        raise ValueError("No module.exports found")
    obj = m.group(1).strip().rstrip(";")
    # Remove trailing commas before } or ]
    obj = _re.sub(r",\s*([}\]])", r"\1", obj)
    # Quote bare object keys so JSON can parse the JS object literal.
    obj = _re.sub(r'(?<=[{,])\s*(\w+)\s*:', lambda m: f' "{m.group(1)}":', obj)
    return json.loads(obj)


# Known context windows â€” users never need to set these manually.
# Keyed by model ID prefix (longest match wins).
_MODEL_MAX_CONTEXT: list[tuple[str, int]] = [
    ("claude",          200_000),
    ("gemini",        1_000_000),
    ("gpt-4o",          128_000),
    ("gpt-4",           128_000),
    ("gpt-3.5",          16_385),
    ("o1",              200_000),
    ("o3",              200_000),
    ("o4",              200_000),
    ("llama-3.1-405",   131_072),
    ("llama-3.1",       131_072),
    ("llama-3.3",       128_000),
    ("llama-3",         128_000),
    ("mixtral",          32_768),
]

def _lookup_max_context(model_id: str) -> int:
    m = model_id.lower()
    for prefix, ctx in _MODEL_MAX_CONTEXT:
        if m.startswith(prefix):
            return ctx
    return 128_000  # safe default


# â”€â”€ Live context window cache â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_CTX_CACHE_TTL  = 86_400  # 24 hours


def _load_context_cache() -> dict:
    cache_file = _runtime_file("model_contexts.json")
    if not cache_file.exists():
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if time.time() - data.get("_fetched_at", 0) > _CTX_CACHE_TTL:
            return {}
        return data
    except Exception:
        return {}


def _save_context_cache(ctx_map: dict):
    runtime_dir = _runtime_prunetool_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    ctx_map["_fetched_at"] = time.time()
    _runtime_file("model_contexts.json").write_text(json.dumps(ctx_map, indent=2), encoding="utf-8")


def _fetch_provider_contexts(env: dict) -> dict:
    """
    Fetch live context window sizes from provider /v1/models endpoints.
    Returns {model_id: context_window} for all models found.
    Silently skips any provider whose API call fails.
    """
    results: dict = {}

    # OpenAI + Groq + Gemini â€” all speak OpenAI-compatible /v1/models
    openai_like = [
        ("openai", "https://api.openai.com/v1/models",   "OPENAI_API_KEY",  "context_window"),
        ("groq",   "https://api.groq.com/openai/v1/models", "GROQ_API_KEY", "context_window"),
    ]
    for provider, url, key_name, field in openai_like:
        api_key = env.get(key_name) or os.environ.get(key_name)
        if not api_key:
            continue
        try:
            resp = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=8.0)
            for m in resp.json().get("data", []):
                ctx = m.get(field)
                if m.get("id") and ctx:
                    results[m["id"]] = int(ctx)
        except Exception:
            pass

    # Anthropic â€” /v1/models returns context_window per model
    anthropic_key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        try:
            resp = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01"},
                timeout=8.0,
            )
            for m in resp.json().get("data", []):
                ctx = m.get("context_window")
                if m.get("id") and ctx:
                    results[m["id"]] = int(ctx)
        except Exception:
            pass

    # Gemini â€” /v1beta/models uses inputTokenLimit
    gemini_key = env.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        try:
            resp = httpx.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={gemini_key}",
                timeout=8.0,
            )
            for m in resp.json().get("models", []):
                model_id = m.get("name", "").replace("models/", "")
                ctx = m.get("inputTokenLimit")
                if model_id and ctx:
                    results[model_id] = int(ctx)
        except Exception:
            pass

    return results


def _get_live_context(model_id: str, live: dict) -> Optional[int]:
    """Exact match first, then check if any live key is a prefix/suffix match."""
    if model_id in live:
        return live[model_id]
    # providers sometimes return versioned IDs like "gpt-4o-2024-08-06"
    # match by stripping date suffixes
    m = model_id.lower()
    for live_id, ctx in live.items():
        if m.startswith(live_id.lower()) or live_id.lower().startswith(m):
            return ctx
    return None


def _normalize_js_models(config: dict, live: dict | None = None) -> dict:
    """Convert .js model entries to the internal format the broker expects."""
    complexity_map = {"simple": "simple", "medium": "medium",
                      "complex": "heavy", "heavy": "heavy"}
    live = live or {}
    normalized = []
    for m in config.get("models", []):
        model_api_id = m.get("model", m.get("id", ""))
        provider = m.get("provider") or _infer_provider(model_api_id)
        raw_cx = m.get("complexity", "medium")
        if isinstance(raw_cx, str):
            raw_cx = [raw_cx]
        complexity = [complexity_map.get(c, c) for c in raw_cx]
        # Priority: 1) user set in .js  2) live API  3) hardcoded table
        max_ctx = (
            m.get("maxContext")
            or _get_live_context(model_api_id, live)
            or _lookup_max_context(model_api_id)
        )
        normalized.append({
            "id":            m.get("id", model_api_id),
            "model":         model_api_id,
            "label":         m.get("label", m.get("id", "")),
            "provider":      provider,
            "complexity":    complexity,
            "dailyTokenGoal": m.get("dailyTokenGoal", 0),
            "maxContext":    max_ctx,
            "priority":      m.get("priority", 1),
        })
    result = dict(config)
    result["models"] = normalized
    return result


# Fallback model list if live fetch fails
_PROVIDER_MODELS_FALLBACK = {
    "anthropic": [
        {"model": "claude-haiku-4-5-20251001", "label": "Claude Haiku"},
        {"model": "claude-sonnet-4-6",         "label": "Claude Sonnet"},
        {"model": "claude-opus-4-6",           "label": "Claude Opus"},
    ],
    "openai": [
        {"model": "gpt-4o-mini", "label": "GPT-4o Mini"},
        {"model": "gpt-4o",      "label": "GPT-4o"},
        {"model": "o3-mini",     "label": "o3 Mini"},
    ],
    "gemini": [
        {"model": "gemini-2.0-flash",   "label": "Gemini Flash"},
        {"model": "gemini-2.0-pro-exp", "label": "Gemini Pro"},
    ],
    "groq": [
        {"model": "llama-3.1-8b-instant",    "label": "Llama 3.1 8B"},
        {"model": "llama-3.3-70b-versatile", "label": "Llama 3.3 70B"},
    ],
}

# Known aggregator endpoints â€” any OpenAI-compatible provider
_AGGREGATOR_ENDPOINTS = {
    "groq":       ("https://api.groq.com/openai/v1/models",    "GROQ_API_KEY"),
    "openai":     ("https://api.openai.com/v1/models",          "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/models",       "OPENROUTER_API_KEY"),
    "together":   ("https://api.together.xyz/v1/models",        "TOGETHER_API_KEY"),
    "ollama":     ("http://localhost:11434/api/tags",            ""),
}


def _guess_complexity(model_id: str) -> str:
    """Guess complexity tier from model name â€” size hints in model ID."""
    mid = model_id.lower()
    if any(x in mid for x in ["opus", "o1", "o3", "72b", "70b", "671b", "405b", "large", "pro", "ultra", "max"]):
        return "complex"
    if any(x in mid for x in ["sonnet", "4o", "medium", "32b", "34b", "mixtral", "gemini-pro"]):
        return "medium"
    return "simple"


def _complexity_legend_lines() -> list[str]:
    return [
        "// Complexity legend:",
        "//   simple = typo fix, rename, add one line, small bug, one-file tweak",
        "//   medium = new function, small feature, explain one file",
        "//   heavy  = architecture, refactor multiple files, explain the whole system",
        "// Edit the complexity value on each model to control which prompts it handles.",
    ]


def _fetch_live_models(provider: str, env: dict) -> list[dict]:
    """
    Fetch live model list from provider or aggregator.
    Returns list of {model, label} dicts.
    """
    # Anthropic â€” separate endpoint
    if provider == "anthropic":
        api_key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        cli = _shutil.which("claude")
        if not api_key and not cli:
            return []
        if api_key:
            try:
                resp = httpx.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                    timeout=8.0,
                )
                models = [{"model": m["id"], "label": m.get("display_name", m["id"])}
                          for m in resp.json().get("data", []) if m.get("id")]
                if models:
                    return models
            except Exception:
                pass
        return _PROVIDER_MODELS_FALLBACK.get("anthropic", [])

    # Gemini
    if provider == "gemini":
        api_key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
        if api_key:
            try:
                resp = httpx.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                    timeout=8.0,
                )
                models = [{"model": m["name"].split("/")[-1], "label": m.get("displayName", m["name"])}
                          for m in resp.json().get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", [])]
                if models:
                    return models
            except Exception:
                pass
        return _PROVIDER_MODELS_FALLBACK.get("gemini", [])

    # OpenAI-compatible aggregators (groq, openai, openrouter, together, ollama)
    url, key_name = _AGGREGATOR_ENDPOINTS.get(provider, (None, None))
    if not url:
        return []

    api_key = env.get(key_name) or os.environ.get(key_name, "") if key_name else ""

    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        resp = httpx.get(url, headers=headers, timeout=8.0)
        data = resp.json()
        # Ollama uses {"models": [{"name": ...}]}
        if provider == "ollama":
            return [{"model": m["name"], "label": m["name"]} for m in data.get("models", [])]
        # Standard OpenAI format
        return [{"model": m["id"], "label": m.get("name", m["id"])}
                for m in data.get("data", []) if m.get("id")]
    except Exception:
        return _PROVIDER_MODELS_FALLBACK.get(provider, [])

def _detect_available_providers(env: dict) -> list[str]:
    """Return list of providers the user has access to (CLI or API key)."""
    available = []
    if _shutil.which("claude"):
        available.append("anthropic")
    elif env.get("ANTHROPIC_API_KEY"):
        available.append("anthropic")
    if _shutil.which("gemini"):
        available.append("gemini")
    elif env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"):
        available.append("gemini")
    if env.get("OPENAI_API_KEY"):
        available.append("openai")
    if env.get("GROQ_API_KEY"):
        available.append("groq")
    return available


def _generate_llm_config(env: dict, selected_providers: list[str] | None = None) -> dict:
    """
    First-time setup: detect available providers and auto-generate the
    project-local .prunetool/llms_prunetoolfinder.js with correct models.
    """
    config_path = _project_llm_config_path(env)
    if config_path is None:
        print("[prune] ERROR: PRUNE_CODEBASE_ROOT is not set in .env.")
        env_file = _find_env_file()
        target_env = env_file if env_file else (Path.cwd() / ".env")
        print(f"  Add it to {target_env} so PruneTool knows which project folder to use.")
        print("  Example: PRUNE_CODEBASE_ROOT=C:\\path\\to\\your\\project")
        return _default_config()

    available = selected_providers or _detect_available_providers(env)
    if not available:
        # No provider access detected. Still generate a usable config from the
        # built-in fallback model table so the project has a local config file.
        available = ["groq", "anthropic", "gemini", "openai"]

    key_map = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
               "gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}

    print("\n  [prune] No local model config found â€” auto-building one now.")
    print("  " + "=" * 40)
    print("  Detected providers:")
    for p in available:
        via = "CLI" if _shutil.which("claude" if p == "anthropic" else p) else "API key"
        if not _shutil.which("claude" if p == "anthropic" else p) and not env.get(key_map.get(p, "")):
            via = "fallback"
        print(f"    - {p}  (via {via})")

    # Collect curated models from all selected providers
    final_models = []
    access_lines = []

    for provider in available:
        cli_name = "claude" if provider == "anthropic" else provider
        has_cli  = bool(_shutil.which(cli_name))
        has_api  = bool(env.get(key_map.get(provider, "")))
        cli_status = "âœ“ detected" if has_cli else "not detected"
        env_file = _find_env_file()
        env_label = env_file if env_file else (Path.cwd() / ".env")
        api_status = "âœ“ detected" if has_api else f"not set (add {key_map.get(provider, 'API_KEY')} to {env_label})"
        access_lines.append(f'//   {provider}:')
        access_lines.append(f'//     CLI â†’ {cli_name} CLI {cli_status}')
        access_lines.append(f'//     API â†’ {key_map.get(provider, "API_KEY")} {api_status}')

        for m in _PROVIDER_MODELS_FALLBACK.get(provider, []):
            alias      = m["model"].split("/")[-1].replace(":", "-").replace(".", "-")
            complexity = _guess_complexity(m["model"])
            final_models.append({
                "id": alias,
                "label": m["label"],
                "provider": provider,
                "model": m["model"],
                "complexity": complexity,
                "dailyTokenGoal": 50000,
            })

    if not final_models:
        print(f"  No models found. Edit {config_path} manually.")
        return

    provider_str = ",".join(available)
    grouped_models: dict[str, list[dict]] = {}
    for model in final_models:
        grouped_models.setdefault(model["provider"], []).append(model)

    # Write the config file
    lines = [
        f'// provider: {provider_str}   <- edit this first line to choose providers',
        f'// If you change providers later, update this line and run prune chat again.',
        f'//',
        f'// Access methods:',
        *access_lines,
        f'//',
        f'// PruneTool uses CLI first, API key as fallback.',
        f'// To switch providers: update the provider line above and re-run prune chat.',
        *_complexity_legend_lines(),
        "module.exports = {",
        "  models: [",
    ]
    for provider in available:
        provider_models = grouped_models.get(provider, [])
        if not provider_models:
            continue
        lines.append(f"    // {provider}")
        for m in provider_models:
            lines.extend([
                "    {",
                f'      id: "{m["id"]}",',
                f'      label: "{m["label"]}",',
                f'      provider: "{m["provider"]}",',
                f'      model: "{m["model"]}",',
                f'      complexity: "{m["complexity"]}",',
                f'      dailyTokenGoal: {m["dailyTokenGoal"]},',
                "    },",
            ])
        lines.append("")
    lines += ["  ]", "};", ""]

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n  [prune] Config saved to {config_path}")
    print(f"  Models configured: {', '.join(m['id'] for m in final_models)}")
    print(f"  Daily token limit: 50,000 per model (edit the file to change)")
    print()
    generated = _default_config()
    generated["models"] = final_models
    return _normalize_js_models(generated)


def _load_llm_config(env: dict | None = None) -> dict:
    # Load live context cache (or fetch fresh if stale)
    live = _load_context_cache()
    if not live and env:
        live = _fetch_provider_contexts(env)
        if live:
            _save_context_cache(live)
            print(f"[prune] Fetched live context windows for {len(live)} models.")

    config_path = _project_llm_config_path(env)
    if env is not None:
        if config_path is None:
            env_file = _find_env_file()
            env_label = env_file if env_file else (Path.cwd() / ".env")
            print("[prune] ERROR: PRUNE_CODEBASE_ROOT is missing from .env.")
            print(f"  Set it to your project folder path in {env_label}")
            print("  Example: PRUNE_CODEBASE_ROOT=C:\\path\\to\\your\\project")
            return _default_config()
        if not config_path.exists() or not config_path.read_text(encoding="utf-8").strip():
            return _generate_llm_config(env)

    for path in _llm_config_paths(env):
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".js":
                if path == config_path and "// Complexity legend:" not in text:
                    text = "\n".join(_complexity_legend_lines()) + "\n" + text.lstrip()
                    path.write_text(text, encoding="utf-8")
                if path == config_path and _re.search(r"(?m)^\s*provider:\s*", text):
                    text = _re.sub(r"(?m)^\s*provider:\s*.*(?:\r?\n|$)", "", text, count=1)
                return _normalize_js_models(_parse_js_config(text), live)
            return json.loads(text)
        except Exception:
            pass
    print("[prune] WARNING: No usable llms_prunetoolfinder.js found. Using defaults.")
    return _default_config()


def _read_config_providers(env: dict | None = None) -> list[str]:
    """Read the '// provider: <name[,name...]>' comment from llms_prunetoolfinder.js."""
    config_path = _project_llm_config_path(env or os.environ)
    if not config_path.exists():
        return []
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("// provider:") or line.startswith("provider:"):
            rest = line.split("provider:", 1)[1].strip()
            provider_text = rest.split("<-", 1)[0].strip()
            provider_text = provider_text.split("(", 1)[0].strip()
            provider_text = provider_text.rstrip(",")
            return [item.strip().lower() for item in provider_text.split(",") if item.strip()]
    return []


def _ping_provider(provider: str, env: dict) -> tuple[bool, str]:
    """
    Check if the provider is reachable â€” CLI or API key.
    Returns (ok, message).
    """
    cli_name = "claude" if provider == "anthropic" else provider
    key_map  = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                "gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}

    # Try CLI auth first for subscription-backed providers.
    auth_checks = {
        "anthropic": ("claude", ["auth", "status"]),
        "openai": ("codex", ["auth", "status"]),
    }
    auth_cli, auth_args = auth_checks.get(provider, (None, None))
    if auth_cli and _shutil.which(auth_cli):
        try:
            result = subprocess.run([auth_cli, *auth_args], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return True, f"{auth_cli} auth status OK"
            output = (result.stdout or result.stderr or "").strip().splitlines()
            if output:
                return False, f"{auth_cli} auth status failed: {output[0][:160]}"
            return False, f"{auth_cli} auth status failed"
        except Exception:
            pass

    # Fall back to CLI presence/version.
    if _shutil.which(cli_name):
        try:
            result = subprocess.run([cli_name, "--version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return True, f"{cli_name} CLI reachable"
        except Exception:
            pass

    # Try API key
    api_key = env.get(key_map.get(provider, ""))
    if api_key:
        try:
            ping_urls = {
                "anthropic": ("https://api.anthropic.com/v1/models", {"x-api-key": api_key, "anthropic-version": "2023-06-01"}),
                "openai":    ("https://api.openai.com/v1/models",    {"Authorization": f"Bearer {api_key}"}),
                "groq":      ("https://api.groq.com/openai/v1/models",{"Authorization": f"Bearer {api_key}"}),
                "gemini":    (f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}", {}),
            }
            url, headers = ping_urls.get(provider, (None, None))
            if url:
                resp = httpx.get(url, headers=headers, timeout=6.0)
                if resp.status_code == 200:
                    return True, f"{provider} API key valid"
        except Exception:
            pass
        return False, f"{provider} API key set but could not reach {provider} API â€” check your connection or key"

    env_file = _find_env_file()
    env_label = env_file if env_file else (Path.cwd() / ".env")
    return False, f"{provider} not reachable â€” no CLI found and no API key set in {env_label}"


def _check_provider_or_ask(env: dict) -> bool:
    """
    Verify the configured providers in llms_prunetoolfinder.js are reachable.
    The providers must be listed in the first-line comment:
      // provider: anthropic,groq
    Returns True if OK to proceed.
    """
    providers = _read_config_providers(env)
    if not providers:
        env_file = _find_env_file()
        env_label = env_file if env_file else (Path.cwd() / ".env")
        print("\n  [prune] No providers listed in llms_prunetoolfinder.js.")
        print("  Add a first-line provider comment, then run prune chat again:")
        print("    // provider: anthropic,groq")
        print(f"  The project .env is read from: {env_label}")
        return False

    print(f"\n  [prune] Using providers from llms_prunetoolfinder.js: {', '.join(providers)}")
    print(f"\n  [prune] Checking providers: {', '.join(providers)}...", end=" ", flush=True)
    failures: list[str] = []
    for provider in providers:
        ok, msg = _ping_provider(provider, env)
        if ok:
            print(f"OK ({provider}: {msg})")
            return True
        failures.append(f"{provider}: {msg}")

    print("FAILED")
    for msg in failures:
        print(f"  [prune] {msg}")
    env_file = _find_env_file()
    env_label = env_file if env_file else (Path.cwd() / ".env")
    print(f"  Fix: install one of the configured providers or add keys to {env_label}")
    return False

    provider = _read_config_provider()

    if not provider:
        print("\n  [prune] No preferred provider set in the project llms_prunetoolfinder.js.")
        print("  Auto mode needs a provider to route queries.")
        available = _detect_available_providers(env)
        if not available:
            env_file = _find_env_file()
            env_label = env_file if env_file else (Path.cwd() / ".env")
            print(f"  No providers detected. Add PRUNE_CODEBASE_ROOT to {env_label} and set your API keys there.")
            print("  e.g. ANTHROPIC_API_KEY=sk-ant-...")
            return False
        provider = available[0]
        # Update the config file with chosen provider
        config_path = _project_llm_config_path(env)
        if config_path is None:
            env_file = _find_env_file()
            env_label = env_file if env_file else (Path.cwd() / ".env")
            print(f"  Set PRUNE_CODEBASE_ROOT in {env_label} first.")
            return False
        if config_path.exists():
            text = config_path.read_text(encoding="utf-8")
            if "// provider:" not in text:
                text = f"// provider: {provider}   â† your preferred provider (anthropic | openai | gemini | groq)\n" + text
                config_path.write_text(text, encoding="utf-8")
        print(f"  Provider set to: {provider}")

    print(f"\n  [prune] Checking provider: {provider}...", end=" ", flush=True)
    ok, msg = _ping_provider(provider, env)
    if ok:
        print(f"OK ({msg})")
        return True
    else:
        print(f"FAILED")
        print(f"  [prune] {msg}")
        env_file = _find_env_file()
        env_label = env_file if env_file else (Path.cwd() / ".env")
        print(f"  Fix: install the {provider} CLI or add the API key to {env_label}")
        if not sys.stdin.isatty():
            return False
        choice = input("  Continue anyway? (y/n): ").strip().lower()
        return choice == "y"


def _default_config() -> dict:
    return {
        "router_model": "llama-3.1-8b-instant",
        "router_provider": "groq",
        "fallback_order": ["groq", "anthropic", "openai", "gemini"],
        "models": [
            {"id": "llama-3.1-8b-instant", "label": "Groq Llama 8B",  "provider": "groq",      "complexity": ["simple"],          "maxContext": 128000, "dailyTokenGoal": 500000, "priority": 1},
            {"id": "claude-sonnet-4-6",    "label": "Claude Sonnet",   "provider": "anthropic", "complexity": ["medium", "heavy"], "maxContext": 200000, "dailyTokenGoal": 50000,  "priority": 1},
            {"id": "gemini-2.0-flash",     "label": "Gemini Flash",    "provider": "gemini",    "complexity": ["simple", "medium"],"maxContext": 1000000,"dailyTokenGoal": 150000, "priority": 2},
        ]
    }


# â”€â”€ Daily stats â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class DailyStats:
    def __init__(self):
        self._data: dict = self._load()

    def _load(self) -> dict:
        today = str(datetime.date.today())
        stats_file = _runtime_file("daily_stats.json")
        if stats_file.exists():
            try:
                data = json.loads(stats_file.read_text(encoding="utf-8"))
                if data.get("date") == today:
                    return data
            except Exception:
                pass
        return {"date": today, "usage": {}}

    def _save(self):
        runtime_dir = _runtime_prunetool_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        _runtime_file("daily_stats.json").write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def tokens_used(self, model_id: str) -> int:
        return self._data["usage"].get(model_id, {}).get("total", 0)

    def record(self, model_id: str, tokens_in: int, tokens_out: int):
        today = str(datetime.date.today())
        if self._data.get("date") != today:
            self._data = {"date": today, "usage": {}}
        usage = self._data["usage"].setdefault(model_id, {"in": 0, "out": 0, "total": 0})
        usage["in"]    += tokens_in
        usage["out"]   += tokens_out
        usage["total"] += tokens_in + tokens_out
        self._save()

    def usage_pct(self, model_id: str, daily_goal: int) -> float:
        if daily_goal <= 0:
            return 0.0
        return self.tokens_used(model_id) / daily_goal * 100


# â”€â”€ Broker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class Broker:
    WARN_PCT  = 90.0
    BLOCK_PCT = 95.0

    def __init__(self, config: dict, env: dict, stats: DailyStats):
        self.config  = config
        self.env     = env
        self.stats   = stats
        self.models  = config.get("models", [])
        self.fallback_order = config.get("fallback_order", ["groq", "anthropic", "openai", "gemini"])

    def available_models(self) -> list[dict]:
        return [m for m in self.models if _get_key(m["provider"], self.env)]

    def classify_complexity(self, prompt: str, context_tokens: int,
                        file_count: int = 0, active_folders: list = None) -> str:
        """
        Classify complexity from Scout structure (file/folder spread).
        Falls back to keyword heuristic if Scout returned nothing.
        Groq LLM classifier removed â€” structural analysis is more accurate.
        """
        if file_count > 0 or active_folders:
            return _classify_by_structure(file_count, active_folders or [])
        return self._classify_heuristic(prompt, context_tokens)

    def _classify_via_groq(self, prompt: str, context_tokens: int, api_key: str) -> str:
        router_model = self.config.get("router_model", "llama-3.1-8b-instant")
        system = (
            "You are a task complexity classifier for a coding AI. "
            "Classify the user's coding request as exactly one word: simple, medium, or heavy.\n"
            "- simple: typo fix, rename, add one line, small bug\n"
            "- medium: new function, small feature, explain one file\n"
            "- heavy: architecture, refactor multiple files, explain entire system, design decisions\n"
            f"Context size: {context_tokens} tokens (>1000 tokens suggests medium/heavy).\n"
            "Reply with ONLY the single word: simple, medium, or heavy."
        )
        try:
            resp = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": router_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": prompt[:500]},
                    ],
                    "max_tokens": 5,
                    "temperature": 0,
                },
                timeout=8.0,
            )
            word = resp.json()["choices"][0]["message"]["content"].strip().lower()
            if word in ("simple", "medium", "heavy"):
                return word
        except Exception:
            pass
        return self._classify_heuristic(prompt, context_tokens)

    def _classify_heuristic(self, prompt: str, context_tokens: int) -> str:
        heavy_kw = {"architecture", "entire", "refactor", "design", "explain", "why does",
                    "how does", "overview", "all files", "whole", "system", "pipeline"}
        low = prompt.lower()
        words = len(prompt.split())
        if any(kw in low for kw in heavy_kw) or context_tokens > 2000:
            return "heavy"
        if words > 30 or context_tokens > 500:
            return "medium"
        return "simple"

    def pick(self, complexity: str, context_tokens: int) -> tuple[Optional[dict], list[str]]:
        """
        Returns (chosen_model_dict, list_of_warning_messages).
        Tries candidates for the complexity tier in priority order.
        Falls back across tiers if nothing available.
        """
        warnings: list[str] = []
        candidates = [m for m in self.available_models() if complexity in m.get("complexity", [])]
        candidates.sort(key=lambda m: m.get("priority", 99))

        for model in candidates:
            model_id  = model["id"]
            goal      = model.get("dailyTokenGoal", 0)
            max_ctx   = model.get("maxContext", 128000)
            pct       = self.stats.usage_pct(model_id, goal) if goal else 0.0
            used      = self.stats.tokens_used(model_id)

            if pct >= self.BLOCK_PCT:
                warnings.append(
                    f"[prune] {model['label']} at {pct:.0f}% daily goal "
                    f"({used:,}/{goal:,} tokens) â€” skipping."
                )
                continue

            if context_tokens > max_ctx:
                warnings.append(
                    f"[prune] {model['label']} maxContext {max_ctx:,} < "
                    f"context {context_tokens:,} tokens â€” skipping."
                )
                continue

            if pct >= self.WARN_PCT:
                warnings.append(
                    f"[prune] Warning: {model['label']} at {pct:.0f}% daily goal. "
                    f"Continuing â€” will pivot when it hits 95%."
                )

            return model, warnings

        # Nothing matched â€” try any available model with enough context
        warnings.append(
            f"[prune] No {complexity} model available. Falling back to any model with headroom."
        )
        for model in self.available_models():
            if context_tokens <= model.get("maxContext", 128000):
                used = self.stats.tokens_used(model["id"])
                goal = model.get("dailyTokenGoal", 0)
                pct  = self.stats.usage_pct(model["id"], goal) if goal else 0.0
                if pct < self.BLOCK_PCT:
                    return model, warnings

        return None, warnings + ["[prune] ERROR: All models exhausted or over daily limit."]


# â”€â”€ Gateway helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _gateway_up() -> bool:
    try:
        httpx.get(f"{GATEWAY_URL}/health", timeout=GATEWAY_TIMEOUT)
        return True
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _listener_pids(port: int) -> list[int]:
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    except Exception:
        return []
    pids: list[int] = []
    needle = f":{port}"
    for line in out.splitlines():
        if needle not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            pid = int(parts[-1])
            if pid not in pids:
                pids.append(pid)
    return pids


def _process_name(pid: int) -> str:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True,
            errors="replace",
        ).strip()
        if out and "No tasks are running" not in out:
            return out.split(",", 1)[0].strip().strip('"')
    except Exception:
        pass
    return ""


def _safe_to_stop_process(name: str) -> bool:
    return name.lower() in {"prunetool.exe", "prune.exe", "python.exe", "pythonw.exe"}


def _what_owns_port(port: int) -> str:
    """Best-effort hint for what commonly occupies a port."""
    known = {
        8000: "a local gateway/dev server (FastAPI/Django/Rails default)",
        8080: "a local proxy or web server (common default)",
        3000: "a Node/React dev server",
        5000: "a Flask dev server",
    }
    return known.get(port, "another process")


def _ensure_ports_ready_for_gateway() -> bool:
    gateway_port = int(os.environ.get("GATEWAY_PORT", 8000))
    proxy_port = int(os.environ.get("PRUNE_PROXY_PORT", 8080))

    for port, role in ((gateway_port, "Gateway"), (proxy_port, "Proxy")):
        if _port_free(port):
            continue

        pids = _listener_pids(port)
        names = [(pid, _process_name(pid)) for pid in pids]
        stoppable = [pid for pid, name in names if _safe_to_stop_process(name)]

        if stoppable:
            for pid in stoppable:
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True, text=True)
                except Exception:
                    pass
            time.sleep(1.0)
            if _port_free(port):
                continue

        print(f"\n[prune] ERROR: Port {port} is already in use.")
        print(f"  Likely cause: {_what_owns_port(port)}")
        if names:
            proc_list = ", ".join(f"{name or 'unknown'} (PID {pid})" for pid, name in names)
            print(f"  Blocking process(es): {proc_list}")
        else:
            print("  Blocking process(es): unknown")
        print(f"  PruneTool needs port {port} for the {role}.")
        print("  Stop the process using that port, then run `prune.exe chat` again.\n")
        return False

    return True


def _project_index_ready() -> bool:
    """
    Check if .prunetool/last_scan.json exists â€” written at end of every
    successful scan. If it's there, all other index files are guaranteed to exist.
    """
    croot = Path(os.environ.get("PRUNE_CODEBASE_ROOT", Path.cwd()))
    return (croot / ".prunetool" / "last_scan.json").exists()


def _last_scan_info() -> dict:
    """Return last_scan.json contents, or empty dict if not found."""
    croot = Path(os.environ.get("PRUNE_CODEBASE_ROOT", Path.cwd()))
    path  = croot / ".prunetool" / "last_scan.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _trigger_project_scan() -> bool:
    """POST /re-scan to gateway. Returns True if accepted."""
    try:
        resp = httpx.post(f"{GATEWAY_URL}/re-scan", json={}, timeout=10.0)
        return resp.status_code == 200
    except Exception:
        return False


def _wait_for_scan(timeout: float = 180.0):
    """Poll /scan-status and print live progress until stage == complete."""
    deadline = time.time() + timeout
    last_msg = ""
    while time.time() < deadline:
        time.sleep(3)
        try:
            data  = httpx.get(f"{GATEWAY_URL}/scan-status", timeout=5.0).json()
            stage = data.get("stage", "idle")
            files = data.get("files_found", 0)
            syms  = data.get("symbols_found", 0)
            ann   = data.get("annotated", 0)
            total = data.get("total_to_annotate", 0)

            if stage == "scanning":
                msg = f"  Indexing files... {files} found"
            elif stage == "building_map":
                msg = f"  Building folder map... {files} files, {syms} symbols"
            elif stage == "annotating":
                pct = int(ann / total * 100) if total else 0
                msg = f"  Annotating files... {ann}/{total} ({pct}%)"
            elif stage == "complete":
                print(f"  Scan complete â€” {files} files, {syms} symbols indexed.\n")
                return
            else:
                msg = f"  {stage}..."

            if msg != last_msg:
                print(msg, flush=True)
                last_msg = msg
        except Exception:
            pass
    print("  Scan timed out â€” context may be partial.\n")


def _load_project_context() -> str:
    """
    Read terminal_context.md from the project's .prunetool/ folder.
    Returns the content as a string, or empty string if not found.
    """
    croot = Path(os.environ.get("PRUNE_CODEBASE_ROOT", Path.cwd()))
    ctx_path = croot / ".prunetool" / "terminal_context.md"
    if ctx_path.exists():
        return ctx_path.read_text(encoding="utf-8", errors="replace")
    return ""


def _get_pruned_context(prompt: str) -> tuple[str, int, int, list[str]]:
    """Returns (context_text, token_estimate, file_count, active_folder_ids)."""
    try:
        resp = httpx.post(
            f"{GATEWAY_URL}/prune",
            json={"query": prompt},
            timeout=15.0,
        )
        data     = resp.json()
        ctx      = data.get("assembled_prompt", data.get("context", ""))
        toks     = data.get("cache_info", {}).get("total_tokens", len(ctx) // 4)
        files    = len(data.get("pruned_files", []))
        folders  = data.get("active_folder_ids", [])
        return ctx, toks, files, folders
    except Exception:
        return "", 0, 0, []


def _classify_by_structure(file_count: int, active_folders: list[str]) -> str:
    """
    Classify query complexity from Scout results â€” no LLM call needed.
    Counts how many distinct folders Scout selected for this query.

    1 folder, 1-2 files  â†’ simple
    2-3 folders           â†’ medium
    4+ folders            â†’ heavy
    """
    folder_count = len(active_folders)
    if folder_count >= 4 or file_count >= 6:
        return "heavy"
    if folder_count >= 2 or file_count >= 3:
        return "medium"
    return "simple"


# â”€â”€ CLI backend (for subscription users without API keys) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# Maps provider â†’ (cli_command, prompt_flag, model_flag)
_PROVIDER_CLI: dict[str, tuple[str, str, str]] = {
    "anthropic": ("claude",  "-p", "--model"),
    "gemini":    ("gemini",  "-p", "--model"),
}


def _find_cli(provider: str) -> Optional[str]:
    """Return full path to provider CLI if installed, else None."""
    cli_name = _PROVIDER_CLI.get(provider, (None,))[0]
    if not cli_name:
        return None
    return _shutil.which(cli_name)


def _detect_backends(env: dict, config: dict) -> dict[str, str]:
    """
    For each model return how it can be called: "api", "cli", or "none".
    Used by model picker to show/hide models and label their access method.
    """
    result = {}
    provider_cache: dict[str, str] = {}
    for m in config.get("models", []):
        provider = m["provider"]
        if provider not in provider_cache:
            ok, _ = _ping_provider(provider, env)
            if ok:
                provider_cache[provider] = "cli" if _find_cli(provider) else "api"
            else:
                provider_cache[provider] = "none"
        result[m["id"]] = provider_cache[provider]
    return result
def _call_cli(model: dict, messages: list, stats: DailyStats) -> tuple[int, int]:
    """
    Call provider CLI (claude / gemini) with -p flag.
    Builds a plain-text prompt from the message history and streams output.
    Returns (tokens_in, tokens_out).
    """
    provider  = model["provider"]
    alias_id  = model["id"]
    model_id  = model.get("model", model["id"])
    cli_path  = _find_cli(provider)
    _, p_flag, m_flag = _PROVIDER_CLI[provider]

    if not cli_path:
        print(f"[prune] ERROR: {provider} CLI not found.")
        return 0, 0

    # Build single text prompt: system context + conversation history
    parts = []
    for msg in messages:
        role    = msg["role"]
        content = msg.get("content", "")
        if role == "system":
            parts.append(f"[System]\n{content}\n")
        elif role == "user":
            parts.append(f"[User]\n{content}\n")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}\n")
    full_prompt = "\n".join(parts)

    tokens_in  = len(full_prompt) // 4
    tokens_out = 0

    cmd = [cli_path, p_flag, full_prompt, m_flag, model_id]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        for char in iter(lambda: proc.stdout.read(1), ""):
            print(char, end="", flush=True)
            tokens_out += 1
        proc.wait()
        if proc.returncode != 0:
            err = proc.stderr.read()
            if err:
                print(f"\n[prune] CLI error: {err[:200]}")
    except Exception as e:
        print(f"\n[prune] CLI call failed: {e}")

    print()
    tokens_out = tokens_out // 4  # chars to rough token estimate
    stats.record(alias_id, tokens_in, tokens_out)
    return tokens_in, tokens_out


# â”€â”€ LLM call (streaming) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
PROVIDER_ENDPOINTS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai":    "https://api.openai.com/v1/chat/completions",
    "groq":      "https://api.groq.com/openai/v1/chat/completions",
    "gemini":    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}


def _stream_response(model: dict, messages: list, env: dict, stats: DailyStats):
    """Stream response to stdout. Returns (tokens_in, tokens_out)."""
    provider  = model["provider"]
    model_id  = model.get("model", model["id"])  # real API model ID
    alias_id  = model["id"]                       # used for stats tracking
    api_key   = _get_key(provider, env)
    endpoint  = PROVIDER_ENDPOINTS.get(provider)

    # Check CLI first â€” subscription users don't have API keys
    if _find_cli(provider):
        return _call_cli(model, messages, stats)

    if not api_key:
        print(f"[prune] ERROR: No {provider} CLI found and no API key set.")
        print(f"         Option 1: Install the {provider} CLI and log in.")
        print(f"         Option 2: Add {provider.upper()}_API_KEY to {Path.cwd() / '.env'}")
        return 0, 0

    if not endpoint:
        print(f"[prune] ERROR: Unknown provider '{provider}'.")
        return 0, 0

    headers = {"Content-Type": "application/json"}
    if provider == "anthropic":
        headers["x-api-key"]         = api_key
        headers["anthropic-version"] = "2023-06-01"
        # Anthropic uses different message format
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_msgs  = [m for m in messages if m["role"] != "system"]
        payload = {
            "model":      model_id,
            "max_tokens": 4096,
            "system":     system_msg,
            "messages":   user_msgs,
            "stream":     True,
        }
    else:
        headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model":    model_id,
            "messages": messages,
            "stream":   True,
        }

    tokens_in  = sum(len(m.get("content", "")) // 4 for m in messages)
    tokens_out = 0

    try:
        with httpx.stream("POST", endpoint, headers=headers, json=payload, timeout=120.0) as resp:
            if resp.status_code != 200:
                body = resp.read().decode()
                print(f"\n[prune] API error {resp.status_code}: {body[:300]}")
                return tokens_in, 0

            for line in resp.iter_lines():
                if not line or line == "data: [DONE]":
                    continue
                if line.startswith("data: "):
                    line = line[6:]
                try:
                    chunk = json.loads(line)
                    # Anthropic
                    if "delta" in chunk and "text" in chunk.get("delta", {}):
                        text = chunk["delta"]["text"]
                        print(text, end="", flush=True)
                        tokens_out += len(text) // 4
                    # OpenAI/Groq/Gemini
                    elif "choices" in chunk:
                        delta = chunk["choices"][0].get("delta", {})
                        text  = delta.get("content", "")
                        if text:
                            print(text, end="", flush=True)
                            tokens_out += len(text) // 4
                except Exception:
                    pass

    except httpx.ReadTimeout:
        print("\n[prune] Request timed out.")
    except Exception as e:
        print(f"\n[prune] Stream error: {e}")

    print()  # newline after response
    stats.record(alias_id, tokens_in, tokens_out)
    return tokens_in, tokens_out


# â”€â”€ Active model persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _get_active_model_alias() -> str:
    active_file = _runtime_file("active_model.txt")
    if active_file.exists():
        return active_file.read_text(encoding="utf-8").strip()
    return "auto"


def _set_active_model_alias(alias: str):
    runtime_dir = _runtime_prunetool_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _runtime_file("active_model.txt").write_text(alias, encoding="utf-8")


def _get_previous_model_alias() -> str:
    prev_file = _runtime_file("previous_model.txt")
    if prev_file.exists():
        return prev_file.read_text(encoding="utf-8").strip()
    return ""


def _set_previous_model_alias(alias: str):
    runtime_dir = _runtime_prunetool_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _runtime_file("previous_model.txt").write_text(alias, encoding="utf-8")


def _normalize_model_key(value: str) -> str:
    value = value.lower().strip()
    value = _re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _resolve_alias(alias: str, config: dict, env: dict) -> Optional[dict]:
    """Resolve a model by id, label, model name, or a unique fuzzy token."""
    target = _normalize_model_key(alias)
    target_tokens = [tok for tok in target.split("-") if tok]
    models = config.get("models", [])

    exact_matches = []
    scored_matches = []
    for idx, model in enumerate(models):
        fields = [
            model.get("id", ""),
            model.get("model", ""),
            model.get("label", ""),
            model.get("provider", ""),
        ]
        normalized = [_normalize_model_key(str(field)) for field in fields if field]
        if target in normalized:
            exact_matches.append((idx, model))
            continue
        key_tokens = set()
        for key in normalized:
            key_tokens.update(tok for tok in key.split("-") if tok)
        overlap = len(set(target_tokens) & key_tokens)
        prefix_bonus = 1 if any(key.startswith(target) or target.startswith(key) for key in normalized) else 0
        contains_bonus = 1 if any(target in key for key in normalized) else 0
        score = overlap * 10 + prefix_bonus * 3 + contains_bonus * 2
        if score:
            scored_matches.append((score, -idx, model))

    if exact_matches:
        return exact_matches[0][1]
    if scored_matches:
        scored_matches.sort(reverse=True)
        return scored_matches[0][2]

    # Legacy shortcuts for older muscle-memory commands.
    alias_map = {
        "sonnet":  "claude-sonnet-4-6",
        "opus":    "claude-opus-4-6",
        "haiku":   "claude-haiku-4-5-20251001",
        "gpt-4o":  "gpt-4o",
        "codex":   "gpt-4.1",
        "groq":    "llama-3.3-70b-versatile",
        "gemini":  "gemini-2.0-flash",
    }
    model_id = alias_map.get(target, alias)
    for m in models:
        if m["id"] == model_id or m.get("model") == model_id:
            return m
    return None


# â”€â”€ Model picker (Copilot-style) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _model_picker(config: dict, env: dict, stats: DailyStats) -> str:
    """
    Show a numbered model list and let the user pick one.
    Returns the chosen model alias, or "auto" if user just hits Enter.
    """
    backends  = _detect_backends(env, config)
    available = [m for m in config.get("models", []) if backends.get(m["id"]) != "none"]

    if not available:
        print("  [prune] No models available.")
        print(f"          Install a provider CLI (claude / gemini) or add API keys to {Path.cwd() / '.env'}")
        return "auto"

    print("\n  Select a model (or press Enter for auto-routing):\n")
    print(f"  {'#':<4} {'Model':<24} {'Via':<6} {'For':<10} {'Used today':>12}  {'Limit':>10}  {'%':>6}")
    print("  " + "-" * 80)

    for i, m in enumerate(available, 1):
        used    = stats.tokens_used(m["id"])
        goal    = m.get("dailyTokenGoal", 0)
        pct     = stats.usage_pct(m["id"], goal) if goal else 0.0
        tiers   = "/".join(m.get("complexity", []))
        via     = backends.get(m["id"], "?")   # "cli" or "api"
        bar     = f"{pct:>5.1f}%"
        limit_flag = "  [near limit]" if pct >= 90 else ""
        print(f"  {i:<4} {m['label']:<24} {via:<6} {tiers:<10} {used:>12,}  {goal:>10,}  {bar}{limit_flag}")

    print(f"\n  {len(available)+1:<4} {'auto':<24} {'(let PruneTool decide per prompt)'}")
    print()

    while True:
        try:
            raw = input("  Pick [1-{}/Enter=auto]: ".format(len(available))).strip()
        except (EOFError, KeyboardInterrupt):
            return "auto"

        if raw == "":
            print("  -> Auto-routing enabled. PruneTool will pick the best model per prompt.\n")
            return "auto"

        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(available):
                chosen = available[idx - 1]
                print(f"  -> {chosen['label']}  (switch anytime with /model <name>)\n")
                return chosen["id"]
            if idx == len(available) + 1:
                print("  -> Auto-routing enabled.\n")
                return "auto"

        print(f"  Please enter a number between 1 and {len(available)+1}, or press Enter.")


# â”€â”€ Commands â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def cmd_models(config: dict, env: dict, stats: DailyStats):
    print("\nConfigured models:\n")
    print(f"  {'Label':<22} {'Provider':<12} {'Complexity':<22} {'Used today':>12}  {'Goal':>10}  {'%':>6}  Context")
    print("  " + "-" * 100)
    for m in config.get("models", []):
        key_ok    = bool(_get_key(m["provider"], env))
        used      = stats.tokens_used(m["id"])
        goal      = m.get("dailyTokenGoal", 0)
        pct       = stats.usage_pct(m["id"], goal) if goal else 0.0
        max_ctx   = m.get("maxContext", 0)
        tiers     = "/".join(m.get("complexity", []))
        key_flag  = " " if key_ok else " [NO KEY]"
        bar = "#" * int(pct / 10) + "." * (10 - int(pct / 10))
        print(f"  {m['label']:<22} {m['provider']:<12} {tiers:<22} {used:>12,}  {goal:>10,}  {pct:>5.1f}%  {max_ctx:,}{key_flag}")
    active = _get_active_model_alias()
    print(f"\n  Active model: {active}\n")


def cmd_model(alias: str, config: dict, env: dict):
    current = _get_active_model_alias()
    if alias == "auto":
        if current and current != "auto":
            _set_previous_model_alias(current)
        _set_active_model_alias("auto")
        print(f"[prune] Auto-routing enabled. Groq will classify each prompt.")
        return
    if alias in ("auto exit", "auto-exit", "exit auto"):
        previous = _get_previous_model_alias()
        if previous:
            restored = _resolve_alias(previous, config, env)
            if restored:
                _set_active_model_alias(restored["id"])
                print(f"[prune] Auto-routing disabled. Restored {restored['label']} ({restored['id']}) via {restored['provider']}")
                return
        available = [m for m in config.get("models", []) if _get_key(m["provider"], env)]
        fallback = available[0] if available else (config.get("models", []) or [None])[0]
        if fallback:
            _set_active_model_alias(fallback["id"])
            print(f"[prune] Auto-routing disabled. Restored {fallback['label']} ({fallback['id']}) via {fallback['provider']}")
            return
        print("[prune] Auto-routing disabled, but no model is available to restore.")
        sys.exit(1)
    model = _resolve_alias(alias, config, env)
    if not model:
        print(f"[prune] Unknown model '{alias}'. Run 'prune models' to see options.")
        sys.exit(1)
    if not _get_key(model["provider"], env):
        env_file = _find_env_file()
        env_label = env_file if env_file else (Path.cwd() / ".env")
        print(f"[prune] No API key for {model['provider']}. Add {model['provider'].upper()}_API_KEY to {env_label}")
        sys.exit(1)
    _set_active_model_alias(model["id"])
    print(f"[prune] Locked to {model['label']} ({model['id']}) via {model['provider']}")


def cmd_status(config: dict, env: dict):
    active = _get_active_model_alias()
    gw_up  = _gateway_up()
    print(f"\n  Gateway   : {'UP  (context injection active)' if gw_up else 'DOWN (plain LLM mode â€” run prunetool.exe first)'}")
    print(f"  Model     : {active}")
    configured = [m["provider"] for m in config.get("models", []) if _get_key(m["provider"], env)]
    print(f"  Keys      : {', '.join(set(configured)) or 'none'}")
    print()


def _scan_age_seconds() -> Optional[float]:
    """
    Returns how many seconds ago the last scan ran.
    Returns None if last_scan.json doesn't exist or can't be parsed.
    """
    info = _last_scan_info()
    indexed_at = info.get("indexed_at")
    if not indexed_at:
        return None
    try:
        from datetime import timezone
        ts = datetime.datetime.fromisoformat(indexed_at)
        # make aware if naive
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.datetime.now(timezone.utc)
        return (now - ts).total_seconds()
    except Exception:
        return None


def _cmd_describe(gw_up: bool) -> str:
    """
    /describe handler â€” checks scan freshness, optionally rescans,
    then loads terminal_context.md into session cache.
    Returns the project_context string (empty string on failure).
    """
    if not gw_up:
        print("[prune] Gateway is not running â€” cannot load project context.")
        print("        Start prunetool.exe first, then type describe_project again.")
        return ""

    # â”€â”€ No index at all â†’ auto scan â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if not _project_index_ready():
        croot = os.environ.get("PRUNE_CODEBASE_ROOT", str(Path.cwd()))
        print(f"[prune] No project index found for: {croot}")
        print("[prune] Running project scan now...\n")
        if _trigger_project_scan():
            _wait_for_scan()
        else:
            print("[prune] Could not trigger scan â€” open http://localhost:8000 and click Scan Project.")
            return ""

    # Check last_scan.json first, and rescan only if the scan is stale.
    age = _scan_age_seconds()
    info = _last_scan_info()
    file_count = info.get("file_count", "?")
    sym_count  = info.get("total_symbols", "?")

    if age is not None and age > 3600:
        hours = int(age // 3600)
        mins  = int((age % 3600) // 60)
        age_str = f"{hours}h {mins}m ago" if hours else f"{mins}m ago"
        print(f"[prune] Last scan was {age_str}  ({file_count} files, {sym_count:,} symbols)")
        print("[prune] Scan is older than 1 hour, so PruneTool will rescan before loading context.")
        try:
            answer = input("[prune] Rescan project before loading context? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer == "y":
            print("[prune] Rescanning...\n")
            if _trigger_project_scan():
                _wait_for_scan()
            else:
                print("[prune] Scan failed â€” loading existing context.")
    else:
        if age is not None:
            mins = int(age // 60)
            print(f"[prune] Project index is fresh ({mins}m old) â€” {file_count} files, {sym_count:,} symbols")

    # â”€â”€ Load context â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print("[prune] Loading project context... ", end="", flush=True)
    ctx = _load_project_context()
    if ctx:
        tok_est = len(ctx) // 4
        print(f"done (~{tok_est:,} tokens)  â€” project context is now active for this session.\n")
    else:
        print("failed â€” terminal_context.md not found. Try rescanning.")
    return ctx


def _launch_gateway_window():
    """
    Open a new terminal window running prunetool.exe so the user can see
    gateway logs live. prunetool.exe sits next to prune.exe in the same folder.
    """
    # Find prunetool.exe next to this binary (dist folder) or next to this script
    candidates = [
        Path(sys.executable).parent / "prunetool.exe",   # inside PyInstaller dist
        Path(__file__).resolve().parent / "prunetool.exe",  # dev mode
    ]
    gateway_exe = next((p for p in candidates if p.exists()), None)

    if not gateway_exe:
        print("[prune] Could not find prunetool.exe â€” start it manually.")
        return

    # Use `start` so Windows opens the gateway in its own terminal window.
    # Pass --gateway to run ONLY the gateway server (not both gateway + proxy).
    # The /WAIT flag keeps the terminal window open until the gateway exits.
    # Environment variables are passed to ensure project config is available.
    subprocess.Popen(
        f'start "PruneTool Gateway" /WAIT cmd /c "{gateway_exe} --gateway"',
        shell=True,
        env=os.environ.copy(),
    )


def cmd_chat(config: dict, env: dict, stats: DailyStats):
    broker = Broker(config, env, stats)
    gw_up  = _gateway_up()

    print("\n  PruneTool Chat")
    print("  " + "=" * 40)

    # â”€â”€ Step 1: ensure gateway is running â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if not gw_up:
        if not _ensure_ports_ready_for_gateway():
            return
        print("  [prune] Gateway not running â€” starting it now...\n")
        _launch_gateway_window()
        for i in range(20):
            time.sleep(1)
            if _gateway_up():
                print("  [prune] Gateway ready.\n")
                gw_up = True
                break
            print(f"  [prune] Waiting for gateway{'.' * ((i % 3) + 1)}   ", end="\r")
        if not gw_up:
            print("\n  [prune] Gateway did not start â€” continuing without codebase context.\n")

    # â”€â”€ Step 2: ensure a project index exists (first-time only) â”€â”€â”€â”€â”€â”€
    if gw_up:
        if not _project_index_ready():
            croot = os.environ.get("PRUNE_CODEBASE_ROOT", str(Path.cwd()))
            print(f"  [prune] No project index found for: {croot}")
            print(f"  [prune] Running first-time project scan...\n")
            if _trigger_project_scan():
                _wait_for_scan()
            else:
                print("  [prune] Could not trigger scan â€” open http://localhost:8000 and click Scan Project.\n")

    # â”€â”€ Step 3: verify provider is reachable â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if not _check_provider_or_ask(env):
        return

    # Copilot-style model picker on every session start
    chosen_alias = _model_picker(config, env, stats)
    _set_active_model_alias(chosen_alias)
    active_alias = chosen_alias

    if active_alias == "auto":
        print("  Type describe_project, /model <name>, /model auto exit, /models, /status, /clear, /quit or /exit\n")
    else:
        print("  Type describe_project, /model auto, /model auto exit, /models, /status, /clear, /quit or /exit\n")

    history: list[dict] = []

    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[prune] Bye.")
            break

        if not prompt:
            continue

        # Inline commands inside chat
        if prompt.startswith("/model "):
            alias = prompt.split(None, 1)[1].strip()
            cmd_model(alias, config, env)
            active_alias = _get_active_model_alias()
            continue
        if prompt in ("/quit", "/exit", "/q"):
            print("[prune] Bye.")
            break
        if prompt == "/models":
            cmd_models(config, env, stats)
            continue
        if prompt == "/status":
            cmd_status(config, env)
            continue
        if prompt == "/clear":
            history.clear()
            print("[prune] Conversation history cleared.")
            continue

        if prompt == "describe_project":
            ctx = _cmd_describe(gw_up)
            if ctx:
                # Inject into history ONCE â€” LLM remembers for whole session
                history.append({"role": "user",      "content": f"## Project Context\n{ctx}"})
                history.append({"role": "assistant", "content": "Got it. I now have full context of your project â€” folder structure, symbols, and annotations loaded. Ask me anything about your codebase."})
            continue

        # â”€â”€ Get pruned context â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ctx_text, ctx_tokens, file_count, active_folders = ("", 0, 0, [])
        if gw_up:
            ctx_text, ctx_tokens, file_count, active_folders = _get_pruned_context(prompt)

        # â”€â”€ Pick model â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if active_alias == "auto":
            complexity = broker.classify_complexity(prompt, ctx_tokens, file_count, active_folders)
            chosen, warnings = broker.pick(complexity, ctx_tokens)
            for w in warnings:
                print(w)
            if not chosen:
                print("[prune] No model available. Check your API keys and daily limits.")
                continue
            print(f"[auto -> {chosen['label']}]  complexity={complexity}  folders={len(active_folders)}  files={file_count}")
        else:
            chosen = _resolve_alias(active_alias, config, env)
            if not chosen:
                print(f"[prune] Model '{active_alias}' not found in config. Run /models.")
                continue
            complexity = "medium"
            # Still check context vs maxContext
            max_ctx = chosen.get("maxContext", 128000)
            if ctx_tokens > max_ctx:
                print(f"[prune] Warning: context ({ctx_tokens:,} tokens) exceeds {chosen['label']} limit ({max_ctx:,}). Response may be truncated.")

        # â”€â”€ Build messages â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        system_parts = [
            "You are a coding assistant with deep knowledge of the user's codebase.",
            "Answer concisely and directly. Refer to specific files and line numbers when relevant.",
        ]
        # Per-prompt pruned snippets â€” only relevant code for this question
        if ctx_text:
            system_parts.append(
                f"\n## Relevant Code for This Question (pruned by Scout)\n{ctx_text}"
            )

        messages = [{"role": "system", "content": "\n".join(system_parts)}]
        messages += history  # project_context lives here after /describe â€” sent once
        messages.append({"role": "user", "content": prompt})

        # â”€â”€ Stream response â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        print(f"\n{chosen['label']}> ", end="", flush=True)
        tok_in, tok_out = _stream_response(chosen, messages, env, stats)

        # Keep history (last 10 turns to avoid ballooning)
        history.append({"role": "user",      "content": prompt})
        history.append({"role": "assistant", "content": "[see above]"})
        if len(history) > 20:
            history = history[-20:]

        if tok_out > 0:
            print(f"  [{chosen['label']} | +{tok_in+tok_out:,} tokens today]\n")


# â”€â”€ Entry point â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def main():
    args = sys.argv[1:]

    if not args or args[0] in ("--help", "-h"):
        print("""
PruneTool CLI

  prune chat              Start interactive chat (auto-routes models)
  prune model <alias>     Lock to a model  (sonnet / opus / haiku / groq / gemini / gpt-4o / auto)
  prune models            List all models, keys, and daily usage
  prune status            Show gateway + active model status

Inside chat:
  describe_project        Load project context into this session
  /model <alias>          Switch model mid-session
  /model auto exit        Exit auto-routing and restore the previous model
  /models                 Show model list
  /status                 Show status
  /clear                  Clear conversation history
  /quit / /exit / /q      Exit and shut down the session
""")
        return

    env    = _load_env()
    config = _load_llm_config(env)
    stats  = DailyStats()

    cmd = args[0].lower()

    if cmd == "chat":
        cmd_chat(config, env, stats)

    elif cmd == "model":
        if len(args) < 2:
            print("Usage: prune model <alias>  (sonnet / haiku / groq / gemini / gpt-4o / auto)")
            sys.exit(1)
        cmd_model(args[1], config, env)

    elif cmd == "models":
        cmd_models(config, env, stats)

    elif cmd == "status":
        cmd_status(config, env)

    else:
        print(f"[prune] Unknown command '{cmd}'. Run 'prune --help' for usage.")
        sys.exit(1)


if __name__ == "__main__":
    main()


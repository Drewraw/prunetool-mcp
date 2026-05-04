# PruneTool

Codebase-aware AI chat for developers — works with your existing Claude, Gemini, or OpenAI subscription. No API key required.

PruneTool indexes your project, picks only the relevant code for each question, and routes your prompt to the right AI model automatically — based on complexity and your daily token budget.

## Download

**[Download PruneTool v1.2 for Windows](https://github.com/Drewraw/prunetool-mcp/releases/latest)**

Unzip and run. No Python, no Node.js, no installs.

---

## Quick Start

### 1. Download and unzip

Download `prunetool-v1.2-windows.zip` from the [releases page](https://github.com/Drewraw/prunetool-mcp/releases/latest) and unzip anywhere.

```
prunetool-app/
  prunetool.exe    ← gateway server + dashboard
  prune.exe        ← AI chat CLI
  _internal/       ← bundled runtime (Python, libs, grammars)
```

### 2. Copy the unzipped PruneTool package into your target project and create a `.env` file

PruneTool searches for `.env` inside the project tree, so this works too:

- `C:\Newexpw\new\experiment\functions\.env`

The `.env` file must include your project root:

```env
PRUNE_CODEBASE_ROOT=C:\Users\kunda\Intellij Workspace\Projects\SpringBootRestApplication
```

Optional API keys (only needed if you don't have a Claude/Gemini CLI installed):

```env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk_...
```

### 3. Start chatting

``` eg:
PS C:\Users\kunda\Intellij Workspace\Projects\SpringBootRestApplication> .\prune.exe chat
```

PruneTool should do:
- Open the gateway server in a new terminal window automatically
- Wait for the gateway to be ready (up to 20s)
- Show a model picker — choose your AI or press Enter for auto-routing
- Create `.prunetool\llms_prunetoolfinder.js` if it does not exist
- Read the provider list from the first line of that file

Then inside chat, type `describe_project` to load your project context:

```
you> describe_project
 create .prunetool folder and inside creates necessary .js files 
[prune] Project index found — 449 files, 10,523 symbols (scanned 2026-04-29)
[prune] Loading project context... done (~5,200 tokens)
— project context is now active for this session.
```

Now ask anything about your codebase.

---

## No API Key? No Problem

PruneTool works with your existing subscriptions via the provider CLIs.

| You have | What to install |
|---|---|
| Claude Pro ($20/mo) | [Claude CLI](https://claude.ai/download) — log in once |
| Gemini Advanced | `npm install -g @google/gemini-cli` — log in once |
| OpenAI / Anthropic API key | Add to `~/.prunetool/.env` |

PruneTool auto-detects which CLIs are installed and uses them first. If you have both a CLI and an API key, the CLI takes priority.

---

## How It Works

### Step 1 — Project scan (runs once, then stays updated)

`prunetool.exe` is the gateway + proxy server. When it starts:

```
prunetool.exe starts
        ↓
Starts gateway on http://localhost:8000  (scanner + /prune API)
Starts proxy   on http://localhost:8080  (OpenAI-compatible IDE endpoint)
        ↓
First run?  → opens http://localhost:8000/#/setup in browser
            → paste API key + set project folder
        ↓
No index yet?  → auto-scans your project once (~15-60s), then never again
Already indexed?  → loads instantly
```

The scan pipeline (triggered automatically on first run, or manually via dashboard):

```
POST /re-scan
        ↓
   → builds skeleton.json          (every function, class, enum with line numbers)
   → builds folder_map.json        (which folders import from which)
   → writes terminal_context.md    (combined snapshot for /describe)
   → writes last_scan.json         (timestamp, file count, symbol count)
   → [background] auto_annotations.json  (one-sentence AI summary per file via Groq)
        ↓
File watcher runs in background
   → detects code file changes
   → rebuilds skeleton + folder_map + terminal_context automatically
        ↓
Watchdog monitors prune library/ folder
   → detects when you save session notes
   → dashboard shows rescan badge so you can click Project Scan
```

### Step 2 — Every prompt you send

```
you type a question
        ↓
Scout model (Groq Llama 8B — fast, ~$0.001/query)
   1. Pre-filters: scores all symbols by keyword overlap → top 1,500
      (max 5 symbols per file — prevents large files crowding out others)
   2. Each symbol shown with: file path, line number, purpose hint, enum values
   3. Scout picks the ~5-10 most relevant files for your question
   4. Extracts only the relevant sections from those files
   5. Assembles compact context: ~3-8K tokens instead of 100K+
        ↓
Complexity classifier (folder spread — no extra API call)
   - 1 folder selected        → simple  → fast cheap model (Haiku, Gemini Flash)
   - 2-3 folders selected     → medium  → balanced model (Sonnet, GPT-4o)
   - 4+ folders selected      → heavy   → powerful model (Opus, o1)
   - checks daily token budget → warns at 90%, switches model at 95%
        ↓
Your chosen LLM gets: pruned context + your question
        ↓
Answer streamed back to your terminal
```

### Why this matters

| | Without PruneTool | With PruneTool |
|---|---|---|
| Context sent per query | ~100K tokens (whole codebase) | ~3-8K tokens (relevant only) |
| Scout cost (Groq) | — | ~$0.001 per query |
| Claude API savings | — | ~$22/month at 50 queries/day |
| Model awareness | none — you explain everything | full — codebase always loaded |

---

## Model Configuration

PruneTool reads the provider list from the first line of `llms_prunetoolfinder.js`:

```js
// provider: anthropic,groq
```

If that line is missing, PruneTool tells you to add it and rerun `prune chat`.

Generated `llms_prunetoolfinder.js`:

```js
// provider: anthropic,groq
// Edit this first line to choose providers.
//
// Access methods:
//   anthropic:
//     CLI → claude CLI ✓ detected
//     API → ANTHROPIC_API_KEY not set
//   groq:
//     CLI → groq CLI not detected
//     API → GROQ_API_KEY ✓ detected
//
// PruneTool uses CLI first, API key as fallback.
// Complexity legend:
//   simple = typo fix, rename, add one line, small bug, one-file tweak
//   medium = new function, small feature, explain one file
//   heavy  = architecture, refactor multiple files, explain the whole system
module.exports = {
  models: [
    { id: "claude-haiku-4-5-20251001", label: "Claude Haiku",      model: "claude-haiku-4-5-20251001", complexity: "simple",  dailyTokenGoal: 50000 },
    { id: "claude-sonnet-4-6",         label: "Claude Sonnet",      model: "claude-sonnet-4-6",         complexity: "medium",  dailyTokenGoal: 50000 },
    { id: "claude-opus-4-6",           label: "Claude Opus",        model: "claude-opus-4-6",           complexity: "complex", dailyTokenGoal: 50000 },
    { id: "llama-3-1-8b-instant",      label: "Llama 3.1 8B",      model: "llama-3.1-8b-instant",      complexity: "simple",  dailyTokenGoal: 50000 },
    { id: "llama-3-3-70b-versatile",   label: "Llama 3.3 70B",     model: "llama-3.3-70b-versatile",   complexity: "medium",  dailyTokenGoal: 50000 },
  ]
};
```

- `complexity` — auto-guessed from model name:
  - `simple` = small one-file changes
  - `medium` = focused feature work or one-file explanation
  - `heavy` = architecture, refactor, or cross-file reasoning
- `dailyTokenGoal` — PruneTool warns at 90%, switches model at 95% (default: 50,000)
- Context window fetched live from provider APIs at startup, cached 24 hours
- On every startup, PruneTool pings the providers listed in the first line before showing the model picker

---

## Chat Commands

```
prune.exe chat              Start chat (gateway auto-opens, model picker appears)

```
work start Inside prune.exe chat:
```
describe_project    Load project context into this session
/model <llm model>      choose and Switch model in session. llm model wont change until end of session.
/model auto          Switch to auto-routing by llama instant from .env
/model auto exit     stops auto mode of choosing llm by llama instant from .env
/models              Show active provider models as per auth list and usage
/status              Show gateway status
/clear               Clear conversation history
/quit                Exit
```

### How `describe_project` works

```
you> describe_project
    ↓
No index?            → runs auto scan first (first time only)
Index < 1 hour old   → loads immediately
Index > 1 hour old   → "Last scan was 3h ago. Rescan? (y/n)"

[prune] Loading project context... done (~5,200 tokens)
— project context is now active for this session.
```

Project context is injected into the conversation **once** as part of your chat history — not resent on every message. The LLM remembers it for the whole session (~50 tokens per message overhead, not 5,200).

The same `describe_project` command is also used by AI agents connecting via MCP (Claude Code, Codex CLI) — they call it automatically on connect. One command, same result, whether typed by a human or called by an AI.

---

## What Gets Stored on Your Machine

```
~/.prunetool/
  .env                    your API keys and project path
  daily_stats.json        token usage per model (resets daily)
  model_contexts.json     cached context window sizes (24h TTL)
  active_model.txt        last selected model
  llms_prunetoolfinder.js your model configuration

<your-project>/
  .prunetool/
    last_scan.json        scan timestamp, file count, symbol count
    skeleton.json         symbol index (every function, class, enum)
    folder_map.json       folder dependency graph
    auto_annotations.json one-line AI summary per file
    annotations.json      user-written folder notes
    project_metadata.json file counts, directory tree
    terminal_context.md   combined snapshot loaded by describe_project
  prune library/
    library.md            session knowledge (written by /save docs)
    PROGRESS.md           current status and next steps
```

Nothing is sent anywhere except your LLM provider. No telemetry.

---

## MCP Integration (for Claude Code, Codex CLI, etc.)

PruneTool also runs an MCP server on port 8765 for AI agents that support the Model Context Protocol.

HTTP transport:

```json
{
  "mcpServers": {
    "prunetool": {
      "url": "http://localhost:8765/mcp"
    }
  }
}
```

stdio transport (Codex CLI):

```bash
codex mcp add prunetool -- /path/to/prunetool.exe mcp
```

MCP tools available to agents:

- `session_start` — initialize session and model tracking
- `describe_project` — full project context (index, annotations, prune library)
- `analyze_complexity` — suggest appropriate model tier
- `report_tokens` — record usage after each response
- `save_docs` — persist session knowledge to prune library

---

## Dashboard

Open `http://localhost:8000` after starting PruneTool to see:

- Token usage and daily model-budget charts
- Folder dependency graph
- Indexed files and symbol browser
- Live scan progress
- Prompt Assist — generates optimized prompts from rough intent

---

## Gateway API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/prune` | Full pipeline: Scout → extract → assemble |
| POST | `/scout-select` | Scout only: pick relevant files |
| POST | `/re-scan` | Rebuild index + annotations |
| GET | `/scan-status` | Live scan progress |
| POST | `/search` | Keyword search over symbol index |
| GET | `/skeleton` | Index summary |
| GET | `/graph` | Folder dependency graph |
| GET | `/annotations` | Folder annotations |
| POST | `/annotations` | Save annotation |
| GET | `/context-version` | Current index version hash (for delta describe) |
| WS | `/ws` | Live index update stream |

---

## Project Structure

```
prunetool/
  server/gateway.py         FastAPI gateway — all HTTP endpoints
  mcp_server.py             HTTP MCP server (port 8765)
  mcp_stdio.py              stdio MCP entry point
  proxy_server.py           OpenAI-compatible local proxy (port 8080)
  prune_cli.py              AI chat CLI (prune.exe)
  prunetool_main.py         Binary entry point (prunetool.exe)
  start_mcp.py              Dev startup script
  llms_prunetoolfinder.js   Shipped default model config
  indexer/
    skeletal_indexer.py     Tree-sitter + regex code parser
    folder_mapper.py        Import graph builder
  pruner/
    pruning_engine.py       Scout ranking + file extraction
    scout.py                Groq/Ollama symbol ranker
    auto_annotator.py       Batch file annotation via Groq
  ui/                       React + Vite dashboard
```

--

#### License

Proprietary. All rights reserved.

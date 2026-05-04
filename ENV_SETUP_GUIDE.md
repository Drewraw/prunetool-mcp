# PruneTool .env Configuration Guide

## Quick Setup

Your `.env` file should be in your **project directory** (where you run `prune.exe chat` from).

For example, if you run:
```powershell
PS C:\Users\kunda\Intellij Workspace\Projects\SpringBootRestApplication> .\prune.exe chat
```

Then create `.env` in: `C:\Users\kunda\Intellij Workspace\Projects\SpringBootRestApplication\.env`

### ✅ Correct Format

```env
# Your project directory (required for prunetool.exe, optional for prune.exe)
PRUNE_CODEBASE_ROOT=.

# Or absolute path if needed
PRUNE_CODEBASE_ROOT=C:\Users\kunda\Intellij Workspace\Projects\SpringBootRestApplication

# API keys (at least one required)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk_...
```

### ❌ DON'T do this

```env
# WRONG - Don't use system-wide config location
# ~/.prunetool/.env is no longer used by prune.exe

# WRONG - Don't add quotes
PRUNE_CODEBASE_ROOT="C:\Users\kunda\Intellij Workspace"

# WRONG - Don't use single quotes  
PRUNE_CODEBASE_ROOT='C:\Users\kunda\Intellij Workspace'

# WRONG - Don't escape spaces
PRUNE_CODEBASE_ROOT=C:\Users\kunda\Intellij\ Workspace
```

## Paths with Spaces

Your path `C:\Users\kunda\Intellij Workspace` is **perfectly fine** — just write it as-is without any quotes or escaping.

## .env Location Behavior

| Command | .env Location | Behavior |
|---------|---------------|----------|
| `.\prune.exe chat` (from project) | `./.env` in cwd | Reads from project directory |
| `prunetool.exe` (from project) | `./.env` in cwd | Reads from project directory |
| `prunetool.exe` (from anywhere) | `./.env` in cwd | Reads from current directory |

**Key point:** Both executables read from the current working directory where you run them from.

## Example Setup

1. Create `.env` in your project root:

```bash
C:\Users\kunda\Intellij Workspace\Projects\SpringBootRestApplication\.env
```

2. Add your configuration:

```env
OPENAI_API_KEY=sk-proj-...your-key-here...
PRUNE_CODEBASE_ROOT=.
```

3. Run from that directory:

```bash
PS C:\Users\kunda\Intellij Workspace\Projects\SpringBootRestApplication> .\prune.exe chat
```

## Verify Setup

Run this from your project directory:

```bash
prune.exe chat
```

You should see:
```
[prune] Checking provider: openai...OK
[prune] Loaded 3 models from llms_prunetoolfinder.js
```

If you see an ERROR, check:
1. ✅ `.env` file exists in your project directory
2. ✅ `PRUNE_CODEBASE_ROOT` path exists (or use `.` for current directory)
3. ✅ API key is valid (test it with `curl` or the provider's CLI)

## Multi-Project Setup

You can have different `.env` files in different projects:

```
ProjectA/
  .env                           (OPENAI_API_KEY=...)
  src/
  ...

ProjectB/
  .env                           (GROQ_API_KEY=...)
  src/
  ...
```

When you `cd` to each project and run `prune.exe chat`, it automatically uses that project's `.env`.

## Troubleshooting

| Error | Solution |
|-------|----------|
| `PRUNE_CODEBASE_ROOT does not exist` | Check the path exists in File Explorer, or use `.` for current directory |
| `Could not read .env` | Check file permissions, encoding should be UTF-8 |
| `No project index found` | First startup takes 15-60s — let it finish scanning |
| `All models exhausted` | Add API keys to `.env` file in your project |



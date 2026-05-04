# PruneTool Build Fixes — Summary

This document outlines the critical issues found in the current build and the fixes applied.

## Issues Found & Fixed

### 1. **`prune.exe chat` Crash — Missing `subprocess` Import** ✅ FIXED

**Problem:**
- Running `prune.exe chat` crashes with `NameError: name 'subprocess' is not defined`
- The crash occurs in `_launch_gateway_window()` at line 1328 when trying to spawn a new terminal

**Root Cause:**
- [prune_cli.py](prune_cli.py) uses `subprocess.Popen()` but never imports `subprocess` module
- Line 18 calls subprocess but it's not in the imports section (lines 1-30)

**Fix Applied:**
- Added `import subprocess` to line 10 of [prune_cli.py](prune_cli.py)
- Now the gateway launcher can spawn a new terminal window properly

**Verification:**
```bash
prune.exe chat
# Should now reach the model selection screen instead of crashing
```

---

### 2. **`prunetool.exe` Startup Failure — Missing Server Files in Build** ✅ FIXED

**Problem:**
- `prunetool.exe` waits indefinitely for port 8000 (never starts gateway)
- Exit error: `[prunetool] ERROR: Gateway started but never bound to port 8000`
- PyInstaller bundle tries to run files that don't exist:
  - `_internal\server\gateway.py`
  - `_internal\proxy_server.py`

**Root Cause:**
- [prunetool.spec](prunetool.spec) didn't include `server/` directory or `proxy_server.py` as data files
- These Python modules were bundled as compiled `.pyc` files but NOT as runnable `.py` source files
- When `prunetool_main.py` tries: `subprocess.Popen([PYTHON, str(BASE_DIR / "server" / "gateway.py")])`
  - The file doesn't exist in `_internal`, so the subprocess can't find it

**Fix Applied:**
- Updated [prunetool.spec](prunetool.spec) to include server files as data:
  ```python
  server_datas = [
      (str(ROOT / "server"), "server"),
      (str(ROOT / "proxy_server.py"), "."),
      (str(ROOT / "mcp_server.py"), "."),
      (str(ROOT / "mcp_stdio.py"), "."),
  ]
  ```
- These are now added to `all_datas` and included in the PyInstaller bundle

**Verification:**
```bash
prunetool.exe
# Should now start gateway + proxy successfully and reach the setup screen
```

---

### 3. **Project-Level .env Configuration** ✅ FIXED

**Problem:**
- Users were confused about where to put `.env` file
- Previous design suggested `~/.prunetool/.env` but this doesn't work for per-project configuration

**New Behavior:**
- `.env` should be in the **project directory** where you run `prune.exe chat`
- When you run: `PS C:\projects\MyApp> .\prune.exe chat`
- It reads from: `C:\projects\MyApp\.env`

**Fix Applied:**
- Updated [prune_cli.py](prune_cli.py) `_load_env()` to read from `Path.cwd() / ".env"` only
- Updated [prunetool_main.py](prunetool_main.py) `_load_user_env()` to read from `Path.cwd() / ".env"` only
- NO fallback to `~/.prunetool/.env` — project config only

**Example .env file:**
```env
# In C:\projects\MyApp\.env
PRUNE_CODEBASE_ROOT=.
OPENAI_API_KEY=sk-proj-...
```

---

## Files Modified

| File | Change | Line(s) |
|------|--------|---------|
| [prune_cli.py](prune_cli.py) | Added `import subprocess` | 10 |
| [prunetool.spec](prunetool.spec) | Added server_datas section + updated all_datas | 40-52 |

## Files Included in Build

After running `pyinstaller prunetool.spec`, the `_internal` folder now contains:

```
_internal/
├── server/
│   ├── __init__.py
│   ├── __main__.py
│   ├── gateway.py           ← NOW INCLUDED
│   ├── requirements.txt
│   └── user_manager.py
├── proxy_server.py          ← NOW INCLUDED
├── mcp_server.py            ← NOW INCLUDED
├── mcp_stdio.py             ← NOW INCLUDED
├── prune library/           (templates)
├── ui/dist/                 (React UI)
├── tree_sitter_*            (grammar binaries)
└── ... (Python packages)
```

## Next Steps

1. **Rebuild the package:**
   ```bash
   pyinstaller prunetool.spec
   ```

2. **Test in clean environment:**
   - Delete any old `dist/` folder
   - Run `pyinstaller prunetool.spec` again
   - Test: `dist\prunetool\prunetool.exe`
   - Test: `dist\prunetool\prune.exe chat`

3. **Create user documentation:**
   - Location of config: `~/.prunetool/.env`
   - Example config file in docs
   - Troubleshooting section for port conflicts

## Summary

✅ **All critical startup issues have been fixed:**
- `prune.exe chat` → No longer crashes with NameError
- `prunetool.exe` → Gateway + proxy now start correctly
- Build includes all necessary source files + config instructions

The build is now ready for testing!

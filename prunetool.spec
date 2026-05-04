# prunetool.spec — PyInstaller build spec
# Produces a single-folder dist (onedir) on Windows/Mac/Linux.
# Run with:
#   pyinstaller prunetool.spec
#
# Output: dist/prunetool/prunetool.exe  (Windows)
#         dist/prunetool/prunetool      (Mac/Linux)

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPEC).resolve().parent   # C:/prunetool

# ── Tree-sitter grammar binaries ─────────────────────────────────────
# Each grammar package ships a _binding.pyd (Windows) or _binding.so.
# We collect the entire package directory so PyInstaller copies the .pyd
# alongside the package __init__.py.
GRAMMAR_PACKAGES = [
    "tree_sitter_python",
    "tree_sitter_javascript",
    "tree_sitter_typescript",
    "tree_sitter_go",
    "tree_sitter_rust",
    "tree_sitter_java",
    "tree_sitter",
]

grammar_datas   = []
grammar_binaries = []
for pkg in GRAMMAR_PACKAGES:
    grammar_datas    += collect_data_files(pkg)
    grammar_binaries += collect_dynamic_libs(pkg)

# ── UI dist (React build) ─────────────────────────────────────────────
ui_datas = [
    (str(ROOT / "ui" / "dist"), "ui/dist"),
]

# ── Prune library starter templates ──────────────────────────────────
lib_datas = [
    (str(ROOT / "prune library"), "prune library"),
]

# ── Server files (gateway + proxy) ────────────────────────────────────
# These must be included as data files so prunetool.exe can run them
# via subprocess from _internal/server/gateway.py and _internal/proxy_server.py
server_datas = [
    (str(ROOT / "server"), "server"),
    (str(ROOT / "proxy_server.py"), "."),
    (str(ROOT / "mcp_server.py"), "."),
    (str(ROOT / "mcp_stdio.py"), "."),
]

# ── All datas combined ────────────────────────────────────────────────
all_datas = grammar_datas + ui_datas + lib_datas + server_datas

# ── Hidden imports ────────────────────────────────────────────────────
# Modules loaded dynamically that PyInstaller can't auto-detect.
hidden = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "fastapi",
    "fastapi.middleware.cors",
    "starlette.routing",
    "httpx",
    "watchfiles",
    "watchdog",
    "watchdog.observers",
    "watchdog.observers.polling",
    "tiktoken",
    "tiktoken_ext",
    "tiktoken_ext.openai_public",
    "dotenv",
    "pydantic",
    "tree_sitter",
] + GRAMMAR_PACKAGES

a = Analysis(
    [str(ROOT / "prunetool_main.py"), str(ROOT / "prune_cli.py")],
    pathex=[str(ROOT)],
    binaries=grammar_binaries,
    datas=all_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "numpy", "pandas",
        "scipy", "PIL", "cv2", "torch", "tensorflow",
        "firebase_admin",   # optional — exclude for smaller binary
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe_main = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="prunetool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,
)

# Second binary: prune.exe — the CLI chat interface
a_cli = Analysis(
    [str(ROOT / "prune_cli.py")],
    pathex=[str(ROOT)],
    binaries=grammar_binaries,
    datas=[(str(ROOT / "llms_prunetoolfinder.js"), ".")],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "numpy", "pandas",
        "scipy", "PIL", "cv2", "torch", "tensorflow",
        "firebase_admin",
    ],
    noarchive=False,
    optimize=0,
)

pyz_cli = PYZ(a_cli.pure)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="prune",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,
)

coll = COLLECT(
    exe_main,
    exe_cli,
    a.binaries,
    a.datas,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="prunetool",
)

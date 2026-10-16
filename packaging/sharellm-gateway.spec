# Windows one-file/windowed preparation ONLY; no build performed.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).resolve().parent
src = root / 'src'
datas = [
    (str(src / 'gateway/ui/dashboard.html'), 'gateway/ui'),
    (str(root / 'config.example.yaml'), '.'),
]
binaries = []
hiddenimports = [
    'sqlite3', '_sqlite3', 'gateway.engine.registry.models',
    'uvicorn.loops.asyncio', 'uvicorn.protocols.http.h11_impl',
    'uvicorn.lifespan.on',
]
for package in (
    'fastapi', 'starlette', 'uvicorn', 'httpx', 'httpcore',
    'pydantic', 'pydantic_core', 'yaml', 'webview', 'certifi',
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
hiddenimports += collect_submodules('webview.platforms')
a = Analysis(
    [str(src / 'gateway/desktop/launcher.py')], pathex=[str(src)],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name='ShareLLM-Gateway',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, uac_admin=False,
)
# No COLLECT => one-file. Never bundle config.yaml, credentials, or user DBs.
# pywebview backend DLL/assets depend on the future Windows build environment.

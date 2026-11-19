# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build definition for the standalone Live Translate binary."""

from PyInstaller.utils.hooks import collect_all

datas = [("assets", "assets")]
binaries = []
hiddenimports = [
    "evdev",
    "speech_recognition",
    "speech_recognition.recognizers.google",
    "deep_translator",
    "groq",
    "requests",
    "dotenv",
]

# Provider libraries load some of their submodules dynamically.  Collecting
# them explicitly keeps every mode (free and Groq) working in the frozen app.
for package in ("deep_translator", "groq"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "doctest",
        "pytest",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="live-translate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

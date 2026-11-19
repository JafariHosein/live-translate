#!/usr/bin/env bash
# Build a single-file standalone executable in dist/live-translate.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [[ ! -x .venv/bin/python ]]; then
    echo "error: .venv is missing. Create it first:" >&2
    echo "  python -m venv .venv" >&2
    echo "  .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if ! .venv/bin/python -c "import PyInstaller" >/dev/null 2>&1; then
    echo "Installing build dependencies..."
    .venv/bin/pip install -r requirements-build.txt
fi

echo "Cleaning previous build..."
rm -rf build dist

echo "Building dist/live-translate (one file)..."
.venv/bin/pyinstaller --noconfirm --clean live-translate.spec

chmod +x dist/live-translate
echo
echo "Built: $HERE/dist/live-translate"
ls -lh dist/live-translate

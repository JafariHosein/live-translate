#!/usr/bin/env bash
# Package dist/live-translate as a GitHub-release tarball.
#
#   ./build.sh
#   ./packaging/make-release.sh
#
# Creates:
#   dist/live-translate-<version>-linux-x86_64.tar.gz
#   dist/live-translate-<version>-linux-x86_64.tar.gz.sha256
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
    echo "error: .venv is missing; run ./build.sh first" >&2
    exit 1
fi
if [[ ! -x dist/live-translate ]]; then
    echo "error: dist/live-translate is missing; run ./build.sh first" >&2
    exit 1
fi

VERSION="$(.venv/bin/python -c 'from config import APP_VERSION; print(APP_VERSION)')"
NAME="live-translate-${VERSION}-linux-x86_64"
STAGE_ROOT="$ROOT/dist/release"
STAGE="$STAGE_ROOT/$NAME"
ARCHIVE="$STAGE_ROOT/$NAME.tar.gz"

rm -rf "$STAGE" "$ARCHIVE" "$ARCHIVE.sha256"
mkdir -p "$STAGE/packaging" "$STAGE/docs"

install -Dm755 dist/live-translate "$STAGE/live-translate"
install -Dm755 packaging/install-release.sh "$STAGE/install.sh"
install -Dm644 README.md "$STAGE/README.md"
install -Dm644 LICENSE "$STAGE/LICENSE"
install -Dm644 CHANGELOG.md "$STAGE/CHANGELOG.md"
install -Dm644 .env.example "$STAGE/.env.example"

install -Dm644 packaging/live-translate.desktop \
    "$STAGE/packaging/live-translate.desktop"
install -Dm644 packaging/live-translate.svg \
    "$STAGE/packaging/live-translate.svg"
cp -a packaging/kwin packaging/gnome "$STAGE/packaging/"
cp -a docs/screenshots "$STAGE/docs/"

tar -C "$STAGE_ROOT" -czf "$ARCHIVE" "$NAME"
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"

echo "Created:"
ls -lh "$ARCHIVE" "$ARCHIVE.sha256"

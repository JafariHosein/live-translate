#!/usr/bin/env bash
# Install the standalone Live Translate build for the current user.
#
#   ./install.sh
#
# Installs:
#   ~/.local/bin/live-translate
#   ~/.local/share/applications/live-translate.desktop
#   ~/.local/share/icons/hicolor/scalable/apps/live-translate.svg
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"

if [[ ! -x "$HERE/live-translate" ]]; then
    echo "error: live-translate was not found next to install.sh" >&2
    exit 1
fi

install -Dm755 "$HERE/live-translate" "$BIN_DIR/live-translate"
install -Dm644 "$HERE/packaging/live-translate.svg" \
    "$DATA_HOME/icons/hicolor/scalable/apps/live-translate.svg"

mkdir -p "$DATA_HOME/applications"
cat > "$DATA_HOME/applications/live-translate.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Live Translate
GenericName=English → Persian Voice Translator
Comment=Push-to-talk English-to-Persian translation overlay
Exec=$BIN_DIR/live-translate
TryExec=$BIN_DIR/live-translate
Icon=live-translate
Terminal=false
Categories=AudioVideo;Audio;
Keywords=translate;translation;persian;farsi;voice;speech;overlay;subtitle;
StartupNotify=false
StartupWMClass=live-translate
EOF

update-desktop-database "$DATA_HOME/applications" >/dev/null 2>&1 || true
gtk-update-icon-cache "$DATA_HOME/icons/hicolor" >/dev/null 2>&1 || true

cat <<EOF
Installed Live Translate:

  $BIN_DIR/live-translate
  $DATA_HOME/applications/live-translate.desktop

Run it with:

  live-translate

If '$BIN_DIR' is not on your PATH, use the absolute path above or add it to
your shell profile.

The global Right Alt hotkey needs membership in the 'input' group:

  sudo usermod -aG input "\$USER"

Then log out and back in. The in-app Hold to Talk button works without this.
For always-on-top setup on KDE, Hyprland or GNOME, see README.md.
EOF

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo
    echo "note: $BIN_DIR is not currently on PATH." >&2
fi

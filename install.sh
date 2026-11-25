#!/usr/bin/env bash
# Install Inky for the current user.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
ICON_ROOT="${HOME}/.local/share/icons/hicolor"

say() { printf '  %s\n' "$*"; }

echo "Inky installer"
echo

missing=()
command -v python3 >/dev/null || missing+=("python3")
command -v tmux >/dev/null || missing+=("tmux")
python3 - <<'PY' 2>/dev/null || missing+=("python3-gi + gir1.2-gtk-3.0 + gir1.2-vte-2.91")
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Vte
PY

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing dependencies: ${missing[*]}"
  echo "  Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 tmux"
  echo "  Arch:          sudo pacman -S python-gobject gtk3 vte3 tmux"
  exit 1
fi

mkdir -p "$BIN_DIR" "$APP_DIR"
chmod +x "$ROOT/bin/inky"
ln -sf "$ROOT/bin/inky" "$BIN_DIR/inky"
say "linked $BIN_DIR/inky"

# InkyLogo.png ships at every hicolor size a menu, dock or task switcher asks for.
for size in 512 256 128 64 48; do
  mkdir -p "$ICON_ROOT/${size}x${size}/apps"
  cp "$ROOT/assets/logo-${size}.png" "$ICON_ROOT/${size}x${size}/apps/inky.png"
done
rm -f "$ICON_ROOT/scalable/apps/inky.svg"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$ICON_ROOT" 2>/dev/null || true
sed "s|@ROOT@|$ROOT|g" "$ROOT/inky.desktop" > "$APP_DIR/inky.desktop"
say "installed desktop entry and icons (48-512 px)"

if ! printf '%s' "$PATH" | grep -q "$BIN_DIR"; then
  echo
  echo "$BIN_DIR is not on your PATH. Add this to your shell rc:"
  echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo
echo "Run: inky            (window with persistent sessions)"
echo "     inky doctor     (check tmux, VTE, fonts, clipboard)"
echo "     inky themes     (list themes)"
echo "     inky ls         (list running sessions)"

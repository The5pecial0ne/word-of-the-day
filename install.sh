#!/bin/bash
# Sets up wotd on your Mac.
#
#   ./install.sh             install (or reinstall after editing anything)
#   ./install.sh uninstall   stop the popups; your words.db stays put
#
# What it does: copies everything into ~/wotd, grabs your first batch of
# words, builds the little wake listener, and tells macOS to keep it running.

set -euo pipefail

APP_DIR="$HOME/wotd"
LABEL="com.wotd.wordoftheday"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE="$(cd "$(dirname "$0")" && pwd)"

stop_agent() {
    # Fine if it isn't running yet, hence the "|| true".
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
}

if [[ "${1:-}" == "uninstall" ]]; then
    stop_agent
    rm -f "$PLIST"
    echo "wotd is switched off. Your words are still in $APP_DIR/words.db"
    echo "(delete that folder too if you want everything gone)."
    exit 0
fi

# Make sure Python and Apple's Swift compiler are there. Both come with the
# Command Line Tools; on a fresh Mac /usr/bin/python3 is only a placeholder.
PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c "import sqlite3" 2>/dev/null \
        || ! xcrun --find swiftc >/dev/null 2>&1; then
    echo "Apple's developer tools aren't ready yet. Run:  xcode-select --install"
    echo "then run this script again."
    exit 1
fi

mkdir -p "$APP_DIR" "$HOME/Library/LaunchAgents"
if [[ "$HERE" != "$APP_DIR" ]]; then
    cp "$HERE/wotd.py" "$HERE/wotd-listener.swift" "$HERE/install.sh" "$APP_DIR/"
    cp "$HERE/README.md" "$APP_DIR/" 2>/dev/null || true
fi

echo "Building the wake listener (takes a few seconds the first time)..."
xcrun swiftc -O -swift-version 5 "$APP_DIR/wotd-listener.swift" -o "$APP_DIR/wotd-listener"

# First batch of words, so the very first popup has something to show.
"$PYTHON" "$APP_DIR/wotd.py" import 30

# The background job. KeepAlive means macOS restarts the listener if it
# ever stops, and RunAtLoad starts it when you log in.
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP_DIR/wotd-listener</string>
        <string>$PYTHON</string>
        <string>$APP_DIR/wotd.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>$APP_DIR/wotd.log</string>
</dict>
</plist>
EOF

stop_agent
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# A short "wotd" command for the terminal, added only once.
if ! grep -q "alias wotd=" "$HOME/.zshrc" 2>/dev/null; then
    echo "alias wotd='$PYTHON $APP_DIR/wotd.py'" >> "$HOME/.zshrc"
    echo "Added the 'wotd' command. Open a new Terminal window to use it."
fi

echo
echo "All set. Your first word should pop up in a few seconds."
echo "From now on it appears whenever you wake or unlock your Mac."

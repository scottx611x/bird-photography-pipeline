#!/bin/bash
# Install lr_host.py as a macOS LaunchAgent so it runs automatically at login.
# Run once: bash ~/bird-photography-pipeline/install_lrhost.sh

set -e

PLIST="$HOME/Library/LaunchAgents/com.bird.lrhost.plist"
PYTHON="$HOME/.pyenv/versions/3.12.11/bin/python3"
SCRIPT="$HOME/bird-photography-pipeline/lr_host.py"
LOG="$HOME/.bird_host.log"

cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.bird.lrhost</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
EOF

# Unload if already loaded (ignore errors)
launchctl unload "$PLIST" 2>/dev/null || true

launchctl load "$PLIST"

echo "lr_host.py installed as a LaunchAgent."
echo "It will now start automatically at login and restart if it crashes."
echo "Log: $LOG"
echo ""
echo "To check status:  launchctl list | grep bird"
echo "To stop:          launchctl unload $PLIST"

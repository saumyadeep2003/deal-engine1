#!/usr/bin/env bash
# Remove the LaunchAgent. Data, outputs and .env are left alone.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.thirdbase.dealengine"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
printf '\033[1mService removed.\033[0m\n'
echo "Kept: $DIR/data/engine.db, $DIR/output/, $DIR/.env"
echo "Run ./deploy/install.sh to put it back."

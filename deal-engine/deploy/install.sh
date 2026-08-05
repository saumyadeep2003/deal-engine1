#!/usr/bin/env bash
# Install the deal engine as an always-on macOS LaunchAgent.
#
#   ./deploy/install.sh
#
# Idempotent: safe to re-run after a code change or a Python upgrade.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.thirdbase.dealengine"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
VENV="$DIR/.venv"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "This installer is for macOS. On Linux use the systemd unit in deploy/dealengine.service."

# ---- 1. Python -------------------------------------------------------------
say "1/6  Checking Python"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    maj=${v%%.*}; min=${v##*.}
    if [[ "$maj" -eq 3 && "$min" -ge 11 ]]; then PY="$c"; break; fi
  fi
done
[[ -n "$PY" ]] || die "Need Python 3.11+. Install with:  brew install python@3.12"
echo "     using $PY ($($PY -c 'import sys;print(sys.version.split()[0])'))"

# ---- 2. venv + deps --------------------------------------------------------
say "2/6  Creating virtualenv and installing dependencies"
[[ -d "$VENV" ]] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$DIR/requirements.txt"
echo "     $($VENV/bin/python -c 'import fastapi, openpyxl, apscheduler; print("deps ok")')"

# ---- 3. .env ---------------------------------------------------------------
say "3/6  Secrets"
if [[ ! -f "$DIR/.env" ]]; then
  cp "$DIR/.env.example" "$DIR/.env"
  chmod 600 "$DIR/.env"
  warn "     created .env from .env.example — fill in RESEND_API_KEY / ZAI_API_KEY when ready."
  warn "     The service runs fine without them: judgment reads [STUB], digests render to files."
else
  chmod 600 "$DIR/.env"
  echo "     .env already present (left untouched)"
fi

# ---- 4. first run ----------------------------------------------------------
say "4/6  Seeding the database (first run only)"
mkdir -p "$DIR/logs" "$DIR/output/digests" "$DIR/output/briefs" "$DIR/output/alerts"
if [[ ! -f "$DIR/data/engine.db" ]]; then
  ( cd "$DIR" && "$VENV/bin/python" db/seed.py )
else
  echo "     data/engine.db exists — keeping it"
fi

# ---- 5. LaunchAgent --------------------------------------------------------
say "5/6  Installing LaunchAgent"
mkdir -p "$PLIST_DIR"
sed -e "s|__PYTHON__|$VENV/bin/python|g" -e "s|__DIR__|$DIR|g" \
    "$DIR/deploy/$LABEL.plist.template" > "$PLIST"
plutil -lint "$PLIST" >/dev/null || die "generated plist is invalid"

# bootout first so a re-run reloads cleanly (ignore "not loaded")
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl enable "gui/$UID/$LABEL"

# ---- 6. wait for health ----------------------------------------------------
say "6/6  Waiting for the service to answer"
PORT=$(grep -E '^DEAL_ENGINE_PORT=' "$DIR/.env" 2>/dev/null | cut -d= -f2 || true)
PORT=${PORT:-8787}
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo
    say "Running.  Dashboard: http://127.0.0.1:$PORT"
    echo "  ./dealctl status    service state, health, licence posture"
    echo "  ./dealctl logs      follow the log"
    echo "  ./dealctl open      open the dashboard"
    echo "  ./dealctl stop      stop until next login"
    echo "  ./deploy/uninstall.sh   remove the service (keeps data)"
    exit 0
  fi
  sleep 1
done
warn "Service did not answer on port $PORT within 30s. Check:"
echo "  tail -40 $DIR/logs/engine.err.log"
exit 1

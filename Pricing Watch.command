#!/bin/bash
# Pricing Watch -- double-click launcher (macOS).
#
# Starts the private Pricing Watch dashboard from the repository this file lives in, then opens
# it in your browser. It serves on 127.0.0.1 only (this Mac, nobody else), uses the repo's own
# .venv when there is one, and loads ./.env if you keep one (see .env.example).
#
# Stop it with Ctrl-C or by closing this window.
#
# Optional settings (environment, or in ./.env):
#   PRICING_WATCH_PORT       port to serve on (default 8765; the next free one is used if busy)
#   PRICING_WATCH_PYTHON     Python to use instead of ./.venv/bin/python or python3
#   PRICING_WATCH_OPS_TOKEN  password for the Operations page (16+ characters)
#   PRICING_WATCH_NO_BROWSER=1   do not open a browser tab
#   PRICING_WATCH_LAUNCHER_DRY_RUN=1   check and print what would happen; start nothing
#
# macOS note: a file you cloned with git double-clicks fine. If you downloaded a zip and macOS
# refuses, right-click the file -> Open once. If it says "permission denied": chmod +x on it.

set -u

say() { printf '%s\n' "$*"; }

pause() {
  # Keep the Terminal window open long enough to read a message (never when scripted).
  if [ -t 0 ] && [ -z "${PRICING_WATCH_LAUNCHER_DRY_RUN:-}" ]; then
    printf '\nPress any key to close this window... '
    read -r -n 1 _
  fi
}

fail() {
  say ""
  say "Pricing Watch could not start:"
  for line in "$@"; do say "  $line"; done
  pause
  exit 1
}

# --- where am I? The repo is wherever this file really lives (symlinks followed). --------------
SOURCE="${BASH_SOURCE[0]:-$0}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
REPO="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
cd "$REPO" || fail "Cannot enter the folder this launcher lives in: $REPO"

APP="$REPO/src/balance_pricing_engine/dashboard/app.py"
[ -f "$APP" ] || fail \
  "Expected the dashboard at: src/balance_pricing_engine/dashboard/app.py" \
  "This launcher must stay in the root of the ai-bank-pricing-engine repository."

# --- settings from ./.env (the engine has no dotenv loader; same as `set -a; source .env`) -----
ENV_FILE="none"
if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/.env"
  set +a
  ENV_FILE="loaded"
fi

# --- which Python? ------------------------------------------------------------------------------
PY=""
if [ -n "${PRICING_WATCH_PYTHON:-}" ]; then
  PY="$PRICING_WATCH_PYTHON"
elif [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
else
  for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PY="$(command -v "$candidate")"
      break
    fi
  done
fi
[ -n "$PY" ] || fail \
  "No Python 3 was found." \
  "Install one (https://www.python.org/downloads/ or: brew install python)."

if ! "$PY" -c "import streamlit" >/dev/null 2>&1; then
  fail \
    "The dashboard's dependencies are not installed for: $PY" \
    "One-time setup, in this folder:" \
    "    python3 -m venv .venv" \
    "    .venv/bin/pip install -e \".[dashboard]\"" \
    "(Live collection from the Operations page also needs the browser extra:" \
    "    .venv/bin/pip install -e \".[browser]\" && .venv/bin/python -m playwright install chromium)"
fi

# --- which port? Reuse a running dashboard; otherwise the first free port from the default. ----
PORT_REQUEST="${PRICING_WATCH_PORT:-8765}"
case "$PORT_REQUEST" in
  '' | *[!0-9]*)
    fail "PRICING_WATCH_PORT must be a whole number from 1024 to 65535 (got '$PORT_REQUEST')."
    ;;
esac
if [ "$PORT_REQUEST" -lt 1024 ] || [ "$PORT_REQUEST" -gt 65535 ]; then
  fail "PRICING_WATCH_PORT must be a whole number from 1024 to 65535 (got '$PORT_REQUEST')."
fi

healthy() {
  curl -fsS --max-time 2 "http://127.0.0.1:$1/_stcore/health" 2>/dev/null | grep -q '^ok'
}

pick_port() {
  "$PY" - "$1" <<'PYEOF'
import socket
import sys

start = int(sys.argv[1])
for port in range(start, min(start + 20, 65536)):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
    print(port)
    break
else:
    sys.exit(1)
PYEOF
}

ALREADY_RUNNING=0
if healthy "$PORT_REQUEST"; then
  ALREADY_RUNNING=1
  PORT="$PORT_REQUEST"
else
  PORT="$(pick_port "$PORT_REQUEST")" || fail \
    "No free port found from $PORT_REQUEST to $((PORT_REQUEST + 19))." \
    "Set PRICING_WATCH_PORT to a free port."
fi
URL="http://127.0.0.1:$PORT"

# Run against this checkout's own source, not whatever copy may be installed elsewhere.
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"

CMD=("$PY" -m streamlit run "$APP"
  --server.address 127.0.0.1 --server.port "$PORT"
  --server.headless true --browser.gatherUsageStats false)

OPS_TOKEN_STATE="unset"
[ -n "${PRICING_WATCH_OPS_TOKEN:-}" ] && OPS_TOKEN_STATE="set"

if [ -n "${PRICING_WATCH_LAUNCHER_DRY_RUN:-}" ]; then
  printf 'DRY_RUN %s=%s\n' \
    repo "$REPO" \
    app "$APP" \
    python "$PY" \
    pythonpath "$PYTHONPATH" \
    address "127.0.0.1" \
    port "$PORT" \
    already_running "$ALREADY_RUNNING" \
    env_file "$ENV_FILE" \
    ops_token "$OPS_TOKEN_STATE" \
    command "${CMD[*]}"
  exit 0
fi

open_url() {
  if command -v open >/dev/null 2>&1; then
    open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$1" >/dev/null 2>&1
  fi
}

if [ "$ALREADY_RUNNING" = "1" ]; then
  say "Pricing Watch is already running at $URL -- opening it."
  [ "${PRICING_WATCH_NO_BROWSER:-}" = "1" ] || open_url "$URL"
  exit 0
fi

say "Pricing Watch dashboard"
say "  folder  : $REPO"
say "  address : $URL  (this Mac only)"
say "  python  : $PY"
if [ "$ENV_FILE" = "loaded" ]; then say "  settings: loaded .env"; fi
if [ "$OPS_TOKEN_STATE" = "unset" ]; then
  say "  note    : the Operations page has no password (PRICING_WATCH_OPS_TOKEN is not set);"
  say "            it is reachable from this Mac only."
fi
say ""
say "Press Ctrl-C or close this window to stop."
say ""

OPENER_PID=""
if [ "${PRICING_WATCH_NO_BROWSER:-}" != "1" ]; then
  (
    # Wait (up to a minute) for the server to answer, then open the browser once.
    attempts=0
    while [ "$attempts" -lt 120 ]; do
      if healthy "$PORT"; then
        open_url "$URL"
        exit 0
      fi
      sleep 0.5
      attempts=$((attempts + 1))
    done
  ) &
  OPENER_PID=$!
  trap '[ -n "$OPENER_PID" ] && kill "$OPENER_PID" 2>/dev/null' EXIT
fi

"${CMD[@]}"
STATUS=$?

# 0 = clean stop; 130/143 = Ctrl-C / terminated. Anything else deserves a readable pause.
case "$STATUS" in
  0 | 130 | 143) ;;
  *)
    say ""
    say "The dashboard stopped unexpectedly (exit code $STATUS). The messages above say why."
    pause
    ;;
esac
exit "$STATUS"

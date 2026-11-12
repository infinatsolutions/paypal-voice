#!/usr/bin/env bash
# PayPal Voice backbone: check, install and configure everything, then launch it for testing.
#
#   ./start.sh                 PayPal Voice demo app in your browser, on the built-in PayPal simulator (default)
#   ./start.sh --sandbox       Same, against your PayPal sandbox app (asks for credentials once)
#   ./start.sh --chat          Talk to it in this terminal instead of the browser
#   ./start.sh --demo          Run the scripted demo conversation and exit
#
# Other options: --port N   --skip-tests   --no-open   --reset (rebuild the environment)   --yes   --help
# Safe to run again: steps that are already done are skipped.

set -Eeuo pipefail

# ------------------------------------------------------------------ settings
MIN_PY_MINOR=10          # Python 3.10 or newer
WANT_PY="3.12"           # version to install if none is suitable
PORT=8787                # not 8000: an older PayPal Voice build left a cached copy of itself on that address
MODE="console"           # console | chat | demo
TARGET=""                # "" (keep .env) | mock | sandbox
RUN_TESTS=1
OPEN_BROWSER=1
RESET=0
ASSUME_YES=0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
cd "$ROOT"

# ------------------------------------------------------------------ output helpers
if [[ -t 1 ]]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; N=$'\e[0m'; else B=; G=; Y=; R=; D=; N=; fi
step() { printf "\n${B}▸ %s${N}\n" "$*"; }
ok()   { printf "  ${G}✓${N} %s\n" "$*"; }
info() { printf "  ${D}%s${N}\n" "$*"; }
warn() { printf "  ${Y}!${N} %s\n" "$*"; }
die()  { printf "\n  ${R}✗ %s${N}\n\n" "$*" >&2; exit 1; }
trap 'die "Stopped at line $LINENO: $BASH_COMMAND"' ERR

confirm() {  # confirm "question" -> 0 for yes
  [[ $ASSUME_YES -eq 1 ]] && return 0
  local a; read -r -p "  $1 [Y/n] " a || true
  [[ -z "$a" || "$a" =~ ^[Yy] ]]
}

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# ------------------------------------------------------------------ arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --chat) MODE="chat" ;;
    --demo) MODE="demo" ;;
    --sandbox) TARGET="sandbox" ;;
    --mock) TARGET="mock" ;;
    --port) [[ -n "${2:-}" ]] || die "--port needs a number"; PORT="$2"; shift ;;
    --skip-tests) RUN_TESTS=0 ;;
    --no-open) OPEN_BROWSER=0 ;;
    --reset) RESET=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) usage ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
  shift
done

[[ -f "$ROOT/requirements.txt" && -d "$ROOT/paypal_voice" ]] || die "Run this from the paypal-voice-backbone folder (start.sh must sit next to requirements.txt)."

printf "${B}PayPal Voice backbone · setup and launch${N}\n"

# ------------------------------------------------------------------ 1. system
step "Checking this Mac"
if [[ "$(uname -s)" == "Darwin" ]]; then
  ok "macOS $(sw_vers -productVersion) on $(uname -m)"
else
  warn "This script targets macOS; continuing on $(uname -s)."
fi
command -v curl >/dev/null || die "curl is missing."

# ------------------------------------------------------------------ 2. Python
step "Finding Python 3.$MIN_PY_MINOR or newer"

py_ok() {  # py_ok /path/to/python -> 0 if version >= 3.MIN
  "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PY_MINOR) else 1)" >/dev/null 2>&1
}

find_python() {
  local c
  # /usr/bin/python3 is skipped on purpose: it is 3.9, and without the Command Line Tools it pops an install dialog.
  for c in \
      python3.13 python3.12 python3.11 python3.10 \
      /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 \
      /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/local/bin/python3 \
      /Library/Frameworks/Python.framework/Versions/3.1[0-9]/bin/python3 \
      "$HOME/.local/bin/python$WANT_PY" python3; do
    local p; p="$(command -v "$c" 2>/dev/null || true)"
    [[ -z "$p" || "$p" == "/usr/bin/python3" ]] && continue
    if py_ok "$p"; then echo "$p"; return 0; fi
  done
  return 1
}

PY="$(find_python || true)"
if [[ -z "$PY" ]]; then
  warn "No Python 3.$MIN_PY_MINOR+ found (macOS ships 3.9, which is too old)."
  BREW="$(command -v brew || true)"
  [[ -z "$BREW" && -x /opt/homebrew/bin/brew ]] && BREW=/opt/homebrew/bin/brew
  [[ -z "$BREW" && -x /usr/local/bin/brew ]] && BREW=/usr/local/bin/brew
  if [[ -n "$BREW" ]]; then
    confirm "Install Python $WANT_PY with Homebrew?" || die "Python $WANT_PY+ is required. Install it from python.org and run this again."
    "$BREW" install "python@$WANT_PY"
    PY="$("$BREW" --prefix)/bin/python$WANT_PY"
  else
    info "Homebrew isn't installed. The lightest option is uv (Astral's Python manager): no admin password, installs into ~/.local."
    confirm "Install uv and Python $WANT_PY into your home folder?" || die "Python $WANT_PY+ is required. Install it from python.org and run this again."
    if ! command -v uv >/dev/null && [[ ! -x "$HOME/.local/bin/uv" ]]; then
      curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
    fi
    UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
    "$UV" python install "$WANT_PY"
    PY="$("$UV" python find "$WANT_PY")"
  fi
  py_ok "$PY" || die "Python install didn't produce a usable interpreter at $PY."
fi
ok "$("$PY" --version) at $PY"

# ------------------------------------------------------------------ 3. virtual environment + packages
step "Preparing the project environment (.venv)"
if [[ $RESET -eq 1 && -d "$VENV" ]]; then rm -rf "$VENV"; info "Removed the old .venv"; fi
if [[ -x "$VENV/bin/python" ]] && ! py_ok "$VENV/bin/python"; then
  warn "Existing .venv uses an old or broken Python; rebuilding it."; rm -rf "$VENV"
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PY" -m venv "$VENV" || die "Couldn't create .venv. Try: ./start.sh --reset"
  ok "Created .venv"
else
  ok ".venv already exists ($("$VENV/bin/python" --version))"
fi
VPY="$VENV/bin/python"

REQ_HASH="$( (cat requirements.txt; "$VPY" --version) | shasum -a 256 | cut -c1-16)"
STAMP="$VENV/.requirements-$REQ_HASH"
if [[ ! -f "$STAMP" ]] || ! "$VPY" -c "import fastapi, uvicorn, httpx, pydantic, jsonschema, referencing, pytest" 2>/dev/null; then
  info "Installing packages (first run takes about a minute)…"
  "$VPY" -m pip install --quiet --disable-pip-version-check --upgrade pip
  "$VPY" -m pip install --quiet --disable-pip-version-check -r requirements.txt
  rm -f "$VENV"/.requirements-*; touch "$STAMP"
  ok "Packages installed"
else
  ok "Packages up to date"
fi

# ------------------------------------------------------------------ 4. configuration (.env)
step "Configuring"

env_get() { [[ -f .env ]] && sed -n "s/^$1=//p" .env | tail -1 || true; }
env_set() {  # env_set KEY VALUE  (replace or append; value written verbatim)
  local k="$1" v="$2" tmp; tmp="$(mktemp)"
  if [[ -f .env ]] && grep -q "^$k=" .env; then
    awk -v k="$k" -v v="$v" 'BEGIN{FS=OFS="="} $1==k{print k"="v; next} {print}' .env >"$tmp"
  else
    { [[ -f .env ]] && cat .env; echo "$k=$v"; } >"$tmp"
  fi
  mv "$tmp" .env; chmod 600 .env
}

if [[ ! -f .env ]]; then
  cat > .env <<'EOF'
# PayPal Voice backbone settings (created by start.sh). Never commit this file.
# mock = built-in PayPal simulator (no account needed) | sandbox | live
PAYPAL_ENV=mock
PAYPAL_CLIENT_ID=
PAYPAL_CLIENT_SECRET=
# OWNER (your business account) | PERSONAL (signed-in users) | BOTH
ACCOUNT_MODE=BOTH
PUBLIC_BASE_URL=http://localhost:8787
HOME_COUNTRY=US
# Leave these commented on mock (the simulator turns them on).
# On sandbox/live, set to true only after PayPal approves each one for your app.
#PERSONAL_PAYMENTS_ENABLED=false
#LOGIN_WITH_PAYPAL_ENABLED=false
#PAYOUTS_ENABLED=false
# Extra on-screen tap for money-moving actions
#SECOND_FACTOR=true
MAX_PER_ACTION=500
DAILY_LIMIT=1000
AUDIT_PATH=audit.jsonl
CONTACTS_PATH=contacts.json
CORS_ORIGINS=http://localhost:5173,http://localhost:8080
EOF
  chmod 600 .env
  ok "Created .env (simulator mode)"
fi

check_sandbox_creds() {  # returns 0 if PayPal sandbox issues a token for these credentials
  local id="$1" secret="$2" code
  # Credentials go through stdin, not the command line, so they don't show up in `ps`.
  code="$(printf 'user = "%s:%s"\n' "$id" "$secret" | curl -s -m 20 -o /dev/null -w '%{http_code}' -K - \
          -d grant_type=client_credentials https://api-m.sandbox.paypal.com/v1/oauth2/token 2>/dev/null || true)"
  code="${code:-000}"
  [[ "$code" == "200" ]] && return 0
  [[ "$code" == "401" ]] && warn "PayPal rejected those credentials (401)." || warn "Couldn't reach PayPal sandbox (HTTP $code). Check your internet connection."
  return 1
}

if [[ "$TARGET" == "sandbox" ]]; then
  CID="$(env_get PAYPAL_CLIENT_ID)"; CSEC="$(env_get PAYPAL_CLIENT_SECRET)"
  if [[ -z "$CID" || -z "$CSEC" ]]; then
    info "Get these at developer.paypal.com → Apps & Credentials → Sandbox → your app."
    read -r -p "  Sandbox client ID: " CID
    read -r -s -p "  Sandbox secret (hidden): " CSEC; echo
    [[ -n "$CID" && -n "$CSEC" ]] || die "Both the client ID and secret are needed for --sandbox."
  fi
  info "Checking the credentials with PayPal…"
  check_sandbox_creds "$CID" "$CSEC" || die "Fix the sandbox credentials in .env (or run without --sandbox to use the simulator)."
  env_set PAYPAL_ENV sandbox; env_set PAYPAL_CLIENT_ID "$CID"; env_set PAYPAL_CLIENT_SECRET "$CSEC"
  ok "Sandbox credentials work and are saved in .env (readable only by you)"
elif [[ "$TARGET" == "mock" ]]; then
  env_set PAYPAL_ENV mock
fi

ENV_NOW="$(env_get PAYPAL_ENV)"; ENV_NOW="${ENV_NOW:-mock}"
if [[ "$ENV_NOW" != "mock" && ( -z "$(env_get PAYPAL_CLIENT_ID)" || -z "$(env_get PAYPAL_CLIENT_SECRET)" ) ]]; then
  warn ".env says PAYPAL_ENV=$ENV_NOW but has no credentials; using the simulator for this run."
  ENV_NOW="mock"
fi
[[ "$ENV_NOW" == "live" ]] && warn "LIVE mode: real money can move. Limits: \$$(env_get MAX_PER_ACTION) per action, \$$(env_get DAILY_LIMIT) per day."
export PAYPAL_ENV="$ENV_NOW"

# Pick a free port and keep the PayPal return URLs pointing at it.
port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
if [[ "$MODE" == "console" ]]; then
  START_PORT=$PORT
  while port_busy "$PORT"; do PORT=$((PORT + 1)); [[ $PORT -gt $((START_PORT + 20)) ]] && die "No free port near $START_PORT."; done
  [[ $PORT -ne $START_PORT ]] && warn "Port $START_PORT is in use; using $PORT."
  export PUBLIC_BASE_URL="http://localhost:$PORT"
fi
ok "Mode: $PAYPAL_ENV$([[ $PAYPAL_ENV == mock ]] && echo ' (built-in simulator, nothing real moves)')"

# ------------------------------------------------------------------ 5. tests
if [[ $RUN_TESTS -eq 1 ]]; then
  step "Running the test suite"
  # Tests always use the simulator, from a scratch folder so your .env and audit log are untouched.
  TEST_DIR="$(mktemp -d)"
  if (cd "$TEST_DIR" && env -u PAYPAL_CLIENT_ID -u PAYPAL_CLIENT_SECRET PAYPAL_ENV=mock \
        "$VPY" -m pytest -q -p no:cacheprovider -c "$ROOT/pytest.ini" --rootdir "$ROOT" "$ROOT/tests" >"$TEST_DIR/out.txt" 2>&1); then
    ok "$(grep -Eo '[0-9]+ passed' "$TEST_DIR/out.txt" | tail -1)"
  else
    tail -25 "$TEST_DIR/out.txt"
    rm -rf "$TEST_DIR"
    die "Tests failed (output above). Fix them, or launch anyway with --skip-tests."
  fi
  rm -rf "$TEST_DIR"
else
  step "Skipping tests (--skip-tests)"
fi

# ------------------------------------------------------------------ 6. launch
trap - ERR
case "$MODE" in
  demo)
    step "Scripted demo"
    exec "$VPY" tools/chat.py tools/demo.txt
    ;;
  chat)
    step "Terminal chat · type what you'd say, press Return on an empty line to quit"
    exec "$VPY" tools/chat.py
    ;;
  console)
    URL="http://localhost:$PORT"
    step "Starting the server"
    info "PayPal Voice:  $URL"
    info "API docs:      $URL/docs"
    info "Use Chrome or Safari for the microphone. Press Ctrl+C here to stop."
    if [[ $OPEN_BROWSER -eq 1 ]]; then
      (
        for _ in $(seq 1 60); do
          curl -fsS "$URL/healthz" >/dev/null 2>&1 && { command -v open >/dev/null && open "$URL" || true; exit 0; }
          sleep 0.25
        done
      ) &
    fi
    exec "$VPY" -m uvicorn paypal_voice.server:app --host 127.0.0.1 --port "$PORT"
    ;;
esac

#!/usr/bin/env bash
# Brings up the whole raw2query stack for local development in one shot:
# Postgres (docker), Alembic migrations, the FastAPI API, N worker
# processes, and the Vite frontend dev server. Ctrl+C stops everything it
# started.
#
# Usage:
#   ./run.sh                # start everything
#   ./run.sh --workers 4     # run 4 worker processes instead of the default 1
#   ./run.sh --skip-install   # skip pip/npm install (assume deps already installed)
#   ./run.sh --no-db          # don't touch docker/Postgres (assume it's already running)
#   ./run.sh --keep-db        # on Ctrl+C, leave the db container running

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT_DIR/logs"
VENV_DIR="$ROOT_DIR/.venv"
FRONTEND_DIR="$ROOT_DIR/frontend"

# One worker by default because the LLM provider's request quota is per-key
# but the pacer that respects it (app/pipeline/ratelimit.py) is per-process:
# two workers would each believe they had the whole quota. Raise this only
# alongside LLM_REQUESTS_PER_MINUTE on a tier with real headroom.
WORKERS=1
SKIP_INSTALL=0
START_DB=1
KEEP_DB=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workers) WORKERS="$2"; shift 2 ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --no-db) START_DB=0; shift ;;
    --keep-db) KEEP_DB=1; shift ;;
    -h|--help)
      sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d; s/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "$LOG_DIR"

# ---- helpers ---------------------------------------------------------------

info()  { echo -e "\033[1;34m[run]\033[0m $*"; }
warn()  { echo -e "\033[1;33m[run]\033[0m $*" >&2; }
error() { echo -e "\033[1;31m[run]\033[0m $*" >&2; }

PIDS=()

cleanup() {
  info "Shutting down…"
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${PIDS[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
  if [[ $START_DB -eq 1 && $KEEP_DB -eq 0 ]]; then
    info "Stopping db container (pass --keep-db to leave it running)…"
    (cd "$ROOT_DIR" && docker compose stop db) >/dev/null 2>&1 || true
  fi
  info "Stopped."
}
trap cleanup EXIT INT TERM

port_in_use() {
  # Best-effort check; if neither tool is present just assume it's free.
  if command -v ss >/dev/null 2>&1; then
    ss -ltn "( sport = :$1 )" 2>/dev/null | grep -q ":$1"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -i ":$1" >/dev/null 2>&1
  else
    return 1
  fi
}

# ---- 0. sanity checks -------------------------------------------------------

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  warn "No .env found — copying .env.example. Fill in LLM_API_KEY before extracting documents."
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
fi

if ! grep -qE '^(LLM|GEMINI)_API_KEY=.+' "$ROOT_DIR/.env" 2>/dev/null; then
  warn "LLM_API_KEY is empty in .env — uploads/queries that need the LLM will fail until it's set."
  warn "Everything else (schemas, topics, health, the UI itself) still comes up fine without it."
fi

for cmd in docker python3 curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    error "Required command '$cmd' not found on PATH."
    exit 1
  fi
done

# ---- 1. Postgres (+pgvector) via docker compose -----------------------------

if [[ $START_DB -eq 1 ]]; then
  if port_in_use 5432; then
    warn "Port 5432 already in use — assuming Postgres is already up, skipping docker compose up."
  else
    info "Starting Postgres (docker compose up -d db)…"
    (cd "$ROOT_DIR" && docker compose up -d db)
    info "Waiting for Postgres to report healthy…"
    for _ in $(seq 1 30); do
      status="$(cd "$ROOT_DIR" && docker compose ps db --format '{{.Health}}' 2>/dev/null || true)"
      [[ "$status" == "healthy" ]] && break
      sleep 1
    done
    if [[ "$status" != "healthy" ]]; then
      error "Postgres did not become healthy in time — check 'docker compose logs db'."
      exit 1
    fi
  fi
else
  info "Skipping db startup (--no-db)."
fi

# ---- 2. Python env + backend deps ------------------------------------------

if [[ ! -d "$VENV_DIR" ]]; then
  info "Creating Python venv at .venv…"
  if ! python3 -m venv "$VENV_DIR" 2>"$LOG_DIR/venv-create.log"; then
    # Some systems ship python3 without ensurepip (no python3-venv/pip
    # apt package). Fall back to a bare venv + bootstrap pip ourselves
    # instead of requiring sudo apt-get.
    warn "python3 -m venv failed (likely no ensurepip on this system) — retrying without bundled pip…"
    rm -rf "$VENV_DIR"
    python3 -m venv --without-pip "$VENV_DIR"
  fi
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if ! python -m pip --version >/dev/null 2>&1; then
  info "Bootstrapping pip inside the venv…"
  curl -sSL https://bootstrap.pypa.io/get-pip.py -o "$LOG_DIR/get-pip.py"
  python "$LOG_DIR/get-pip.py" -q
fi

if [[ $SKIP_INSTALL -eq 0 ]]; then
  info "Installing backend package (pip install -e .) — first run pulls unstructured/torch, can take several minutes…"
  pip install -q --upgrade pip
  pip install -q -e "$ROOT_DIR"
elif ! python -c "import app" >/dev/null 2>&1; then
  error "--skip-install was passed but the backend package isn't installed in .venv yet. Run once without --skip-install first."
  exit 1
fi

info "Running Alembic migrations (alembic upgrade head)…"
(cd "$ROOT_DIR" && alembic upgrade head)

# ---- 3. API -----------------------------------------------------------------

if port_in_use 8000; then
  warn "Port 8000 already in use — assuming the API is already running, not starting another copy."
else
  info "Starting API (uvicorn app.api:app) → logs/api.log"
  (cd "$ROOT_DIR" && uvicorn app.api:app --host 0.0.0.0 --port 8000 --reload) \
    > "$LOG_DIR/api.log" 2>&1 &
  PIDS+=($!)
fi

# ---- 4. Worker(s) -------------------------------------------------------------

info "Starting $WORKERS worker process(es) → logs/worker-*.log"
for i in $(seq 1 "$WORKERS"); do
  (cd "$ROOT_DIR" && python -m app.worker) > "$LOG_DIR/worker-$i.log" 2>&1 &
  PIDS+=($!)
done

# ---- 5. Frontend --------------------------------------------------------------

if [[ ! -f "$FRONTEND_DIR/.env" ]]; then
  cp "$FRONTEND_DIR/.env.example" "$FRONTEND_DIR/.env"
fi

if [[ $SKIP_INSTALL -eq 0 && ! -d "$FRONTEND_DIR/node_modules" ]]; then
  info "Installing frontend deps (npm install)…"
  (cd "$FRONTEND_DIR" && npm install --silent)
fi

if port_in_use 5173; then
  warn "Port 5173 already in use — assuming the frontend dev server is already running, not starting another copy."
else
  info "Starting frontend (npm run dev) → logs/frontend.log"
  (cd "$FRONTEND_DIR" && npm run dev -- --host 0.0.0.0 --port 5173) \
    > "$LOG_DIR/frontend.log" 2>&1 &
  PIDS+=($!)
fi

# ---- 6. Wait for the API to actually answer, then report ---------------------

info "Waiting for the API to respond on :8000/health…"
for _ in $(seq 1 60); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
  info "API is up."
else
  warn "API didn't answer /health yet — check logs/api.log."
fi

cat <<EOF

Everything is up:
  Frontend:      http://localhost:5173
  API:           http://localhost:8000
  API docs:      http://localhost:8000/docs
  Postgres:      localhost:5432 (raw2query/raw2query)

Logs: $LOG_DIR/{api,worker-N,frontend}.log
Press Ctrl+C to stop the API, workers, and frontend (db is stopped too unless --keep-db was passed).
EOF

wait

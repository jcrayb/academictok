#!/usr/bin/env bash
#
# Run ingestion on this (Ollama-enabled) machine, then ship just what changed
# to a remote production server's DB over SSH, and disconnect.
#
# Designed for: production server is too slow to run an LLM (LLM_ENABLED=false
# there, see config.py), so ingestion happens here on a desktop with Ollama,
# and the result is synced over afterwards rather than running the server
# against this machine's DB directly. Only a delta is transferred — see
# scripts/sync_export.py / scripts/sync_apply.py for how — not the whole file.
#
# One-time setup before the first real run:
#   1. Copy the production DB onto this machine once (e.g.
#      `scp user@homeserver:~/academictok/academictok.db .`) so this
#      machine's ingestion sees the same already-ingested papers.
#   2. Run this script with -b (bootstrap) to set the local watermark to now
#      without exporting anything, since that clone *is* the baseline.
#
# Usage:
#   scripts/ollama_sync_run.sh -r REMOTE [options] -- INGEST_COMMAND...
#
# Options:
#   -r REMOTE      SSH destination for the production server, e.g. user@host
#                  (required unless -b is also omitted... no: always required)
#   -d REMOTE_DIR  Remote academictok repo root (default: ~/academictok)
#   -p REMOTE_PY   Remote python interpreter (default: REMOTE_DIR/bin/python)
#   -s STATE_FILE  Local watermark file (default: .sync_state.json)
#   -n             Don't manage Ollama (assume it's already running)
#   -b             Bootstrap: skip ingest/Ollama/transfer, just set the local
#                  watermark to now (run once, right after cloning prod's DB)
#   -h             Show this help and exit
#
# INGEST_COMMAND is whatever you'd normally run locally, e.g.:
#   scripts/ollama_sync_run.sh -r jcrayb@homeserver -- \
#       scripts/bulk_ingest.sh -f -l 30 scripts/queries.example.txt
#   scripts/ollama_sync_run.sh -r jcrayb@homeserver -- \
#       ./bin/python scripts/run_ingest.py --fast --field queueing-theory
#
set -uo pipefail

usage() {
  sed -n '3,/^set /{/^set /d; s/^#//; s/^ //; p;}' "$0"
  exit "${1:-0}"
}

REMOTE=""
REMOTE_DIR="academictok"
REMOTE_PYTHON=""
STATE_FILE=".sync_state.json"
MANAGE_OLLAMA=1
BOOTSTRAP=0

while getopts ":r:d:p:s:nbh" opt; do
  case "$opt" in
    r) REMOTE="$OPTARG" ;;
    d) REMOTE_DIR="$OPTARG" ;;
    p) REMOTE_PYTHON="$OPTARG" ;;
    s) STATE_FILE="$OPTARG" ;;
    n) MANAGE_OLLAMA=0 ;;
    b) BOOTSTRAP=1 ;;
    h) usage 0 ;;
    :) echo "Error: -$OPTARG requires an argument." >&2; usage 1 ;;
    \?) echo "Error: unknown option -$OPTARG." >&2; usage 1 ;;
  esac
done
shift $((OPTIND - 1))

if [ -z "$REMOTE" ]; then
  echo "Error: -r REMOTE (SSH destination) is required." >&2
  usage 1
fi
[ -z "$REMOTE_PYTHON" ] && REMOTE_PYTHON="$REMOTE_DIR/bin/python"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$REPO_ROOT/bin/python" ]; then
    PYTHON="$REPO_ROOT/bin/python"
  else
    PYTHON="python3"
  fi
fi

read_watermark() {
  "$PYTHON" - "$STATE_FILE" <<'EOF'
import json, os, sys
path = sys.argv[1]
default = "1970-01-01T00:00:00+00:00"
if not os.path.exists(path):
    print(default)
else:
    print(json.load(open(path)).get("last_synced_at", default))
EOF
}

write_watermark() {
  "$PYTHON" - "$STATE_FILE" "$1" <<'EOF'
import json, sys
json.dump({"last_synced_at": sys.argv[2]}, open(sys.argv[1], "w"))
EOF
}

if [ "$BOOTSTRAP" -eq 1 ]; then
  now="$("$PYTHON" -c 'from db import now_iso; print(now_iso())')"
  write_watermark "$now"
  echo "Watermark set to $now. Nothing exported (this is the baseline)."
  exit 0
fi

if [ $# -eq 0 ]; then
  echo "Error: no ingest command given (pass it after --)." >&2
  usage 1
fi

OLLAMA_PID=""
cleanup() {
  if [ -n "$OLLAMA_PID" ]; then
    echo "Stopping Ollama (pid $OLLAMA_PID)…"
    kill "$OLLAMA_PID" 2>/dev/null
  fi
}
trap cleanup EXIT

OLLAMA_HOST_URL="$("$PYTHON" -c 'import config; print(config.OLLAMA_HOST)' 2>/dev/null || echo "http://localhost:11434")"

if [ "$MANAGE_OLLAMA" -eq 1 ]; then
  if curl -s -o /dev/null -m 2 "$OLLAMA_HOST_URL"; then
    echo "Ollama already running at $OLLAMA_HOST_URL."
  else
    echo "Starting Ollama…"
    ollama serve >/tmp/ollama_sync_run.log 2>&1 &
    OLLAMA_PID=$!
    for i in $(seq 1 30); do
      curl -s -o /dev/null -m 2 "$OLLAMA_HOST_URL" && break
      sleep 1
    done
    if ! curl -s -o /dev/null -m 2 "$OLLAMA_HOST_URL"; then
      echo "Error: Ollama didn't come up at $OLLAMA_HOST_URL (see /tmp/ollama_sync_run.log)." >&2
      exit 1
    fi
    echo "Ollama up (pid $OLLAMA_PID)."
  fi
fi

echo "════════════════════════════════════════════════════════════════"
echo "Running: $*"
echo "════════════════════════════════════════════════════════════════"
if ! "$@"; then
  echo "Error: ingest command failed; not syncing." >&2
  exit 1
fi

WATERMARK="$(read_watermark)"
DELTA="$(mktemp /tmp/academictok-delta-XXXXXX.db)"
rm -f "$DELTA"  # mktemp creates it; sync_export.py wants to create it fresh

echo "════════════════════════════════════════════════════════════════"
echo "Exporting changes since $WATERMARK…"
echo "════════════════════════════════════════════════════════════════"
NEW_WATERMARK="$("$PYTHON" scripts/sync_export.py --since "$WATERMARK" --out "$DELTA")"
if [ -z "$NEW_WATERMARK" ]; then
  echo "Error: export didn't report a new watermark." >&2
  exit 1
fi

if [ ! -s "$DELTA" ]; then
  echo "Nothing to sync."
  write_watermark "$NEW_WATERMARK"
  exit 0
fi

REMOTE_DELTA="/tmp/$(basename "$DELTA")"
echo "Shipping delta to $REMOTE:$REMOTE_DELTA…"
if ! scp -q "$DELTA" "$REMOTE:$REMOTE_DELTA"; then
  echo "Error: scp to $REMOTE failed; not advancing watermark. Delta kept at $DELTA." >&2
  exit 1
fi

echo "Applying on $REMOTE…"
if ssh "$REMOTE" "cd '$REMOTE_DIR' && '$REMOTE_PYTHON' scripts/sync_apply.py --delta '$REMOTE_DELTA'"; then
  ssh "$REMOTE" "rm -f '$REMOTE_DELTA'" 2>/dev/null
  rm -f "$DELTA"
  write_watermark "$NEW_WATERMARK"
  echo "Synced. Watermark advanced to $NEW_WATERMARK."
else
  echo "Error: remote apply failed; not advancing watermark. Delta kept at $DELTA (and remotely at $REMOTE_DELTA) for a retry:" >&2
  echo "  ssh $REMOTE \"cd '$REMOTE_DIR' && '$REMOTE_PYTHON' scripts/sync_apply.py --delta '$REMOTE_DELTA'\"" >&2
  exit 1
fi

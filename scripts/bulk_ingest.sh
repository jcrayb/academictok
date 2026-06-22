#!/usr/bin/env bash
#
# Bulk-populate AcademicTok from a file of search queries.
#
# Reads a text file with one query per line and ingests each query's papers in
# turn. Blank lines and lines beginning with '#' are ignored, so the file can be
# commented. A failure on one query is logged and the batch keeps going, so a
# single bad query or network blip won't abort the whole run.
#
# Each query is handed to scripts/run_ingest.py --query, which fetches the
# papers, summarizes/classifies them, and stores anything new (already-ingested
# papers are skipped, so re-running is safe and incremental).
#
# Usage:
#   scripts/bulk_ingest.sh [options] QUERIES_FILE
#
# Options:
#   -l LIMIT          Max papers per query           (default: 20)
#   -c MIN_CITATIONS  Drop papers below this count   (default: 0)
#   -f                Fast mode: walk the *entire* queries file fetching,
#                     summarizing, and classifying every paper first (no PDFs),
#                     then do one PDF/detailed-summary pass at the end across
#                     everything just ingested (see python scripts/run_ingest.py
#                     --fast --defer-enrich and scripts/upgrade_summaries.py)
#   -q                Quiet ingestion (warnings only, no per-paper log)
#   -h                Show this help and exit
#
# Environment:
#   PYTHON   Override the Python interpreter (defaults to the repo venv at
#            ./bin/python, falling back to python3).
#
# Example:
#   scripts/bulk_ingest.sh -l 30 -c 5 scripts/queries.example.txt
#   scripts/bulk_ingest.sh -f -l 30 scripts/queries.example.txt
#
set -uo pipefail

usage() {
  # Print the header comment block (everything between the shebang and `set`).
  sed -n '3,/^set /{/^set /d; s/^#//; s/^ //; p;}' "$0"
  exit "${1:-0}"
}

LIMIT=20
MIN_CITATIONS=0
QUIET=""
FAST=""
DEFER=""

while getopts ":l:c:fqh" opt; do
  case "$opt" in
    l) LIMIT="$OPTARG" ;;
    c) MIN_CITATIONS="$OPTARG" ;;
    f) FAST="--fast"; DEFER="--defer-enrich" ;;
    q) QUIET="--quiet" ;;
    h) usage 0 ;;
    :) echo "Error: -$OPTARG requires an argument." >&2; usage 1 ;;
    \?) echo "Error: unknown option -$OPTARG." >&2; usage 1 ;;
  esac
done
shift $((OPTIND - 1))

QUERIES_FILE="${1:-}"
if [ -z "$QUERIES_FILE" ]; then
  echo "Error: no queries file given." >&2
  usage 1
fi
if [ ! -f "$QUERIES_FILE" ]; then
  echo "Error: queries file not found: $QUERIES_FILE" >&2
  exit 1
fi

# Resolve the repo root (this script lives in <repo>/scripts) and run from there
# so .env and the relative DATABASE_PATH/MEDIA_DIR defaults resolve the same way
# they do for the documented `python scripts/run_ingest.py` invocation.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Prefer the project's virtualenv interpreter if present; PYTHON=... overrides.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$REPO_ROOT/bin/python" ]; then
    PYTHON="$REPO_ROOT/bin/python"
  else
    PYTHON="python3"
  fi
fi

# Make QUERIES_FILE absolute before we cd, so a relative path still resolves.
QUERIES_FILE="$(cd "$(dirname "$QUERIES_FILE")" && pwd)/$(basename "$QUERIES_FILE")"
cd "$REPO_ROOT"

# Count the real (non-blank, non-comment) queries up front for progress display.
total=$(grep -cvE '^[[:space:]]*($|#)' "$QUERIES_FILE")
if [ "$total" -eq 0 ]; then
  echo "No queries found in $QUERIES_FILE (all blank or comments)."
  exit 0
fi

echo "Bulk ingest: $total queries from '$QUERIES_FILE'"
echo "  limit=$LIMIT  min_citations=$MIN_CITATIONS  fast=${FAST:+yes (PDFs deferred to the end)}  python=$PYTHON"
echo

idx=0
ok=0
failed=0
FAILED_QUERIES=()

# `|| [ -n "$line" ]` so a final line without a trailing newline is still read.
while IFS= read -r line || [ -n "$line" ]; do
  # Trim leading/trailing whitespace.
  query="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -z "$query" ] && continue            # skip blank lines
  case "$query" in '#'*) continue ;; esac # skip comments

  idx=$((idx + 1))
  echo "════════════════════════════════════════════════════════════════"
  echo "[$idx/$total] Ingesting: $query"
  echo "════════════════════════════════════════════════════════════════"

  if "$PYTHON" scripts/run_ingest.py \
        --query "$query" --limit "$LIMIT" --min-citations "$MIN_CITATIONS" \
        $FAST $DEFER $QUIET; then
    ok=$((ok + 1))
  else
    status=$?
    failed=$((failed + 1))
    FAILED_QUERIES+=("$query")
    echo "WARNING: query failed (exit $status): $query" >&2
  fi
  echo
done < "$QUERIES_FILE"

if [ -n "$FAST" ]; then
  echo "════════════════════════════════════════════════════════════════"
  echo "All queries ingested. Running detailed-summary/PDF pass over everything…"
  echo "════════════════════════════════════════════════════════════════"
  "$PYTHON" scripts/upgrade_summaries.py --detailed-only --all $QUIET
  echo
fi

echo "════════════════════════════════════════════════════════════════"
echo "Bulk ingest complete: $ok ok, $failed failed (of $total)."
if [ "$failed" -gt 0 ]; then
  echo "Failed queries:"
  for q in "${FAILED_QUERIES[@]}"; do
    echo "  - $q"
  done
  exit 1
fi

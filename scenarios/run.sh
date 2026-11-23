#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
  greenfield|brownfield|ambiguous) scenario="$1" ;;
  *) printf 'Usage: scenarios/run.sh {greenfield|brownfield|ambiguous} [run-id]\n' >&2; exit 2 ;;
esac

run="${2:-$scenario}"
uv run python -m workflow start "$run" "$scenario"

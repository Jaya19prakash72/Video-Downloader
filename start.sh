#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.deno/bin:$PATH"
exec gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 0 --access-logfile - --error-logfile -

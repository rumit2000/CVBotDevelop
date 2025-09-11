#!/usr/bin/env bash
set -euo pipefail

export PYTHONUNBUFFERED=1
echo "[BOOT] Python:" $(python3 -V)
python3 - <<'PY'
import sys
print("[BOOT] Executable:", sys.executable)
import uvicorn
print("[BOOT] uvicorn:", uvicorn.__version__)
PY

if [ ! -f data/about_cache.txt ]; then
  echo "[BOOT] No cache detected. Running ingestion..."
  if ! python3 ingestion.py; then
    echo "[BOOT] ingestion failed (continue anyway)"
  fi
else
  echo "[BOOT] Cache detected. Skipping ingestion."
fi

PORT="${PORT:-10000}"
echo "[BOOT] Starting uvicorn on port $PORT ..."
exec python3 -m uvicorn webhook:app --host 0.0.0.0 --port "$PORT" --log-level info --access-log

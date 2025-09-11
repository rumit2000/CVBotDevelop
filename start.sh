#!/usr/bin/env bash
set -xeuo pipefail

export PYTHONUNBUFFERED=1

echo "[BOOT] Python:" $(python3 -V || true)
python3 -c "import sys; print('[BOOT] Executable:', sys.executable)" || true
python3 -c "import uvicorn; print('[BOOT] uvicorn:', uvicorn.__version__)" || echo "[BOOT] uvicorn import FAILED"

# Если кэша ещё нет — соберём (about/faq + векторный индекс)
if [ ! -f data/about_cache.txt ] || [ ! -f data/faq_cache.json ]; then
  echo "[BOOT] No cache detected. Running ingestion..."
  python3 ingestion.py || echo "[BOOT] ingestion failed (continue anyway)"
else
  echo "[BOOT] Cache found: data/about_cache.txt & data/faq_cache.json"
fi

echo "[BOOT] Starting uvicorn on port ${PORT:-8000} ..."
exec python3 -m uvicorn webhook:app --host 0.0.0.0 --port "${PORT:-8000}" --log-level info --access-log

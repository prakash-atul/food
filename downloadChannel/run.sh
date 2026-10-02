#!/usr/bin/env bash
# Convenience runner (bash). Reads ./list.txt, ./cookies.txt -> ./output/
# Override the endpoint with VLLM_BASE_URL (default 27B on :8005).
export VLLM_BASE_URL="${VLLM_BASE_URL:-http://localhost:8005/v1}"
export REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-120}"
export RETRIES="${RETRIES:-4}"
python app.py "$@"

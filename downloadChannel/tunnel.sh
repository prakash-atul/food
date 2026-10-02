#!/usr/bin/env bash
# Open (and auto-reconnect) the SSH tunnel to the HPC Qwen endpoint.
# Keep this running in its own terminal while app.py downloads.
#
#   bash tunnel.sh          # 27B on :8005 (default)
#   PORT=8010 bash tunnel.sh  # 4B on :8010
#
# The network sometimes drops; this loop re-establishes the tunnel within ~3s.

PORT="${PORT:-8005}"
HPC="${HPC:-atul_prakash@10.1.7.58}"

echo "Tunneling localhost:${PORT} -> ${HPC}:localhost:${PORT}  (Ctrl-C to stop)"
while true; do
  ssh -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
      -o ConnectTimeout=10 -N -L "${PORT}:localhost:${PORT}" "${HPC}"
  echo "[tunnel] dropped, reconnecting in 3s..."
  sleep 3
done

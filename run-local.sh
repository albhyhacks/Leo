#!/usr/bin/env bash
# Run Leo entirely on this Jetson: mic + speakers on the board, browser on the
# board, no network needed. Usage: ./run-local.sh
set -e
cd "$(dirname "$0")"
./start.sh
echo "Opening Leo at https://localhost:8080 (accept the certificate once)..."
nohup firefox --new-window https://localhost:8080 >/dev/null 2>&1 &

#!/usr/bin/env bash
# Generate a self-signed TLS certificate for the bridge. Browsers only allow the
# microphone on a secure origin; https://localhost is one. Add your LAN IP as the
# first argument to also reach Leo from another device:  ./scripts/gen-cert.sh 192.168.1.20
set -euo pipefail
cd "$(dirname "$0")/../certs"
SAN="DNS:localhost,DNS:leo.local,IP:127.0.0.1"
[ $# -ge 1 ] && SAN="$SAN,IP:$1"
openssl req -x509 -nodes -newkey rsa:2048 -days 365 -keyout tau.key -out tau.crt \
  -subj "/CN=leo.local" -addext "subjectAltName=$SAN"
echo "Wrote certs/tau.crt and certs/tau.key (SAN: $SAN)"

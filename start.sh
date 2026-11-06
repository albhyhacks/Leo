#!/usr/bin/env bash
# Leo — bring the whole stack up and print the URL to open in the browser.
#
# Safe to re-run: every step is idempotent. Works from a cold boot, and keeps
# the `sudo docker` fallback for sessions that still predate the docker group.
#
#   ./start.sh
#
# Optional: export SUDO_ASKPASS=/path/to/helper before running if `docker`
# itself needs root (a helper is any script that prints the password).
set -uo pipefail
cd "$(dirname "$0")"

# --- 1. find a usable docker command ---------------------------------------
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if sudo -n true 2>/dev/null; then
    DOCKER=(sudo docker)
  elif [ -n "${SUDO_ASKPASS:-}" ]; then
    DOCKER=(sudo -A docker)
  else
    cat <<'EOF'
Leo: cannot talk to the Docker daemon as this user.

Either log out and back in (the bridge runs in Docker and the docker group
only applies to new sessions), or point sudo at a password helper:

  export SUDO_ASKPASS=/path/to/askpass.sh   # script that prints the password
  ./start.sh
EOF
    exit 1
  fi
fi

# --- 2. start the four containers -----------------------------------------
"${DOCKER[@]}" compose up -d || exit 1

# First run builds the bridge image; that takes ~8 minutes on the Orin.
if ! "${DOCKER[@]}" compose images bridge >/dev/null 2>&1; then
  echo "Building the bridge image (first run only, ~8 min)…"
  "${DOCKER[@]}" compose build bridge || exit 1
fi

# --- 3. wait for the bridge to answer HTTPS health ------------------------
printf 'waiting for the bridge '
health=000
for _ in $(seq 1 60); do
  health=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 2 https://localhost:8080/health 2>/dev/null)
  [ "$health" = "200" ] && break
  printf '.'
  sleep 2
done
printf '\n'

if [ "$health" != "200" ]; then
  echo "Leo: the bridge did not become healthy (last HTTP $health)."
  echo "Recent bridge logs:"
  "${DOCKER[@]}" logs --tail 30 tau-bridge-1 2>&1 | tail -30
  exit 1
fi

# --- 4. print the URL, warning if the certificate does not cover this IP --
ip=$(hostname -I | awk '{print $1}')
echo
echo "Leo is up."
echo "  Voice: https://${ip}:8080             (talk to Leo)"
echo "  Dash:  https://${ip}:8080/dashboard   (status, memory, latency)"
echo "         (self-signed certificate — accept the warning; the browser"
echo "          needs HTTPS before it will allow the microphone)"
echo "  Logs:  docker logs -f tau-bridge-1"
echo "  Stats: ${PWD}/logs/latency.csv  (one row per spoken turn)"

if [ -f certs/tau.crt ] && command -v openssl >/dev/null 2>&1; then
  if openssl x509 -in certs/tau.crt -noout -text 2>/dev/null | grep -q "$ip"; then
    echo "  Cert:  covers ${ip} ✔"
  else
    echo "  Cert:  does NOT list ${ip} — the mic will still work after"
    echo "         accepting the warning, but regenerate certs/tau.crt with"
    echo "         this IP in the SAN if the browser refuses getUserMedia."
  fi
fi

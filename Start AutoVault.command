#!/bin/bash
set -u

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1
PORT="${AUTOVAULT_PORT:-8766}"
URL="http://127.0.0.1:${PORT}"

pause_on_error() {
  printf '\nAutoVault could not start. See the message above.\n'
  read -r -p 'Press Return to close this window... '
  exit 1
}

if ! command -v docker >/dev/null 2>&1; then
  echo 'Docker was not found. Install Docker Desktop, then run this launcher again.'
  open 'https://www.docker.com/products/docker-desktop/' >/dev/null 2>&1 || true
  pause_on_error
fi

if ! docker info >/dev/null 2>&1; then
  echo 'Starting Docker Desktop…'
  open -a Docker >/dev/null 2>&1 || true
  ready=0
  for _ in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
  done
  if [ "$ready" -ne 1 ]; then
    echo 'Docker Desktop did not become ready. Open Docker Desktop and try again.'
    pause_on_error
  fi
fi

echo 'Building and starting AutoVault…'
if ! docker compose up --build -d; then
  pause_on_error
fi

echo 'Waiting for AutoVault to be ready…'
ready=0
for _ in $(seq 1 60); do
  if curl --fail --silent "$URL/api/health" >/dev/null; then ready=1; break; fi
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo 'AutoVault did not become ready. Recent container logs:'
  docker compose logs --tail=80 autovault
  pause_on_error
fi

echo "AutoVault is ready at $URL"
open "$URL"
echo 'Your workflows and snapshots remain saved in Docker’s persistent autovault_data volume.'
read -r -p 'Press Return to close this window (AutoVault will keep running)... '

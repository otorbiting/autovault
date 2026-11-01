#!/bin/bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${AUTOVAULT_PORT:-8766}"
URL="http://127.0.0.1:${PORT}"
BACKUP="$APP_DIR/portable-data/autovault-data-backup.tar.gz"
OUTPUT="$APP_DIR/outputs/autovault-project-with-data.zip"
cd "$APP_DIR"

if ! curl --fail --silent "$URL/api/health" >/dev/null; then
  echo "AutoVault is not running at $URL. Double-click Start AutoVault.command first."
  read -r -p 'Press Return to close this window... '
  exit 1
fi

mkdir -p "$APP_DIR/portable-data" "$APP_DIR/outputs"
echo 'Creating a fresh snapshot of the current workflows, notes, run history, and versions…'
curl --fail --silent --show-error -X POST "$URL/api/backup" >/dev/null
curl --fail --silent --show-error "$URL/api/backup/download" -o "$BACKUP"
python3 -c 'from pathlib import Path; Path(__import__("sys").argv[1]).unlink(missing_ok=True)' "$OUTPUT"
zip -qr "$OUTPUT" . -x './outputs/*' './.git/*' './work/*' './__pycache__/*'
echo "Portable project created: $OUTPUT"
open -R "$OUTPUT"
echo 'On the other device, unzip the project, start AutoVault, then choose RESTORE and select portable-data/autovault-data-backup.tar.gz.'
read -r -p 'Press Return to close this window... '

#!/bin/bash
# Apply config/boot/config.txt from this repo to /boot/firmware/config.txt.
# Use when boot firmware drifted (e.g. stale HiFiBerry overlay) and capture devices
# never appear in arecord -l / PortAudio.
#
# Requires reboot after running.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/config/boot/config.txt"
DST="/boot/firmware/config.txt"
if [[ ! -f "$SRC" ]]; then
  echo "Missing $SRC" >&2
  exit 1
fi
if [[ ! -w "/boot/firmware" ]] && ! sudo -n true 2>/dev/null; then
  echo "Run with sudo or as a user allowed to write $DST" >&2
  exit 1
fi
TS="$(date +%Y%m%d%H%M%S)"
sudo cp -a "$DST" "${DST}.bak.before-apply-${TS}"
sudo cp -a "$SRC" "$DST"
echo "Updated $DST (backup: ${DST}.bak.before-apply-${TS})"
echo "Reboot required: sudo reboot"

#!/bin/bash
# Build oled_initramfs static binary for early boot display
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUT="$PROJECT_DIR/utils/oled_early"
SRC="$PROJECT_DIR/utils/oled_initramfs.c"

echo "Building oled_early (static)..."
gcc -static -O2 -o "$OUT" "$SRC" -Wall

if [ -f "$OUT" ]; then
    echo "Built: $OUT"
    ls -la "$OUT"
else
    echo "Build failed"
    exit 1
fi

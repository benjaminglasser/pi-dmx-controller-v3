#!/bin/bash
# Install oled_splash.service (fixes systemd ordering cycle)
# Run with: sudo scripts/install_oled_splash.sh
# Or from project root: sudo scripts/install_oled_splash.sh
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ "$EUID" -ne 0 ]; then
    echo "Run with: sudo $0"
    exit 1
fi

echo "Installing OLED splash service (fixes ordering cycle)..."

# Remove oled_wake - it caused the cycle with basic.target; initramfs handles early display
if systemctl is-enabled oled_wake.service 2>/dev/null; then
    echo "  Disabling oled_wake.service (was causing ordering cycle)"
    systemctl disable oled_wake.service || true
fi
rm -f /etc/systemd/system/oled_wake.service

# Install oled_splash.service
cp "$PROJECT_DIR/systemd/oled_splash.service" /etc/systemd/system/oled_splash.service

# Ensure DMX service starts after splash (they share the SPI OLED)
for DMX_SVC in /etc/systemd/system/dmx_audio_react.service /etc/systemd/system/pi-dmx.service; do
    if [ -f "$DMX_SVC" ]; then
        if ! grep -q "oled_splash.service" "$DMX_SVC"; then
            echo "  Adding After=oled_splash.service to $(basename $DMX_SVC)"
            sed -i '/^After=/s/$/ oled_splash.service/' "$DMX_SVC" 2>/dev/null || \
            sed -i '/^\[Unit\]/a After=oled_splash.service' "$DMX_SVC"
        fi
        break
    fi
done

systemctl daemon-reload
systemctl enable oled_splash.service

echo "Done. Reboot to apply: sudo reboot"

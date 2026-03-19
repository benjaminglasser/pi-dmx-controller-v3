#!/bin/bash
# Install initramfs hooks for early OLED display
# Run with: sudo scripts/install_oled_initramfs.sh
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ "$EUID" -ne 0 ]; then
    echo "Run with: sudo $0"
    exit 1
fi

echo "Installing OLED early-boot initramfs hooks..."

# Build binary first
"$PROJECT_DIR/scripts/build_oled_initramfs.sh"

# Copy hook
cp "$PROJECT_DIR/config/initramfs/hook-oled-boot" /etc/initramfs-tools/hooks/oled-boot
chmod +x /etc/initramfs-tools/hooks/oled-boot

# Remove from init-top (runs too early, SPI may not exist)
rm -f /etc/initramfs-tools/scripts/init-top/oled-display
# Use init-premount (runs after udev, SPI devices more likely ready)
cp "$PROJECT_DIR/config/initramfs/script-init-top-oled-display" /etc/initramfs-tools/scripts/init-premount/oled-display
chmod +x /etc/initramfs-tools/scripts/init-premount/oled-display

# Add SPI modules if not present
for mod in spi_bcm2835 spidev; do
    if ! grep -q "^$mod" /etc/initramfs-tools/modules 2>/dev/null; then
        echo "$mod" >> /etc/initramfs-tools/modules
        echo "  Added $mod to initramfs modules"
    fi
done

# Rebuild initramfs
echo "Rebuilding initramfs..."
update-initramfs -u

echo "Done. Reboot to see early OLED display."

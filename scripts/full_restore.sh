#!/usr/bin/env bash
###############################################################################
# full_restore.sh - Complete DMX Controller System Restoration
#
# This script restores the DMX Audio-Reactive Light Controller from scratch
# after an SD card failure or fresh Raspberry Pi OS install.
#
# Usage:
#   sudo ./scripts/full_restore.sh
#
# Prerequisites:
#   - Fresh Raspberry Pi OS Bookworm (32-bit) installed
#   - SSH access configured
#   - Internet connection
#   - This repository cloned to ~/pi-dmx-controller-v2
###############################################################################

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  DMX Controller Full System Restore${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: This script must be run with sudo${NC}"
    echo "Usage: sudo $0"
    exit 1
fi

# Get the actual user (not root)
ACTUAL_USER="${SUDO_USER:-$(whoami)}"
ACTUAL_HOME=$(eval echo "~$ACTUAL_USER")

echo -e "${YELLOW}Running as: $ACTUAL_USER${NC}"
echo -e "${YELLOW}Project directory: $PROJECT_DIR${NC}"
echo ""

###############################################################################
# Step 1: System Update
###############################################################################
echo -e "${GREEN}[1/8] Updating system packages...${NC}"
apt update
apt -y full-upgrade

###############################################################################
# Step 2: Install Core Packages
###############################################################################
echo -e "${GREEN}[2/8] Installing core packages...${NC}"
apt install -y \
    git python3 python3-venv python3-pip \
    alsa-utils libportaudio2 portaudio19-dev libsndfile1 \
    python3-pil i2c-tools \
    libcairo2-dev libgirepository1.0-dev

###############################################################################
# Step 3: Enable SPI and I2C
###############################################################################
echo -e "${GREEN}[3/8] Enabling SPI and I2C interfaces...${NC}"
raspi-config nonint do_spi 0
raspi-config nonint do_i2c 0

###############################################################################
# Step 4: Install Boot Configuration
###############################################################################
echo -e "${GREEN}[4/8] Installing boot configuration...${NC}"
if [ -f "$PROJECT_DIR/config/boot/config.txt" ]; then
    cp "$PROJECT_DIR/config/boot/config.txt" /boot/firmware/config.txt
    echo "  - Installed /boot/firmware/config.txt"
else
    echo -e "${RED}  - Warning: config/boot/config.txt not found${NC}"
    echo "  - Applying minimal config manually..."
    
    CFG=/boot/firmware/config.txt
    
    # Disable onboard audio
    sed -i 's/^dtparam=audio=on/# dtparam=audio=on/' "$CFG" || true
    grep -q '^dtparam=audio=off' "$CFG" || echo 'dtparam=audio=off' >> "$CFG"
    
    # Enable HiFiBerry
    grep -q '^dtoverlay=hifiberry-dacplusadcpro' "$CFG" || \
        echo 'dtoverlay=hifiberry-dacplusadcpro' >> "$CFG"
    
    # Enable UART
    grep -q '^enable_uart=1' "$CFG" || echo 'enable_uart=1' >> "$CFG"
    
    # Disable Bluetooth
    grep -q '^dtoverlay=disable-bt' "$CFG" || echo 'dtoverlay=disable-bt' >> "$CFG"
    
    # SPI single CS
    grep -q '^dtoverlay=spi0-2cs' "$CFG" || echo 'dtoverlay=spi0-2cs,cs0_pin=0' >> "$CFG"
fi

###############################################################################
# Step 5: Install ALSA Configuration
###############################################################################
echo -e "${GREEN}[5/8] Installing ALSA configuration...${NC}"
if [ -f "$PROJECT_DIR/config/alsa/asound.conf" ]; then
    cp "$PROJECT_DIR/config/alsa/asound.conf" /etc/asound.conf
    echo "  - Installed /etc/asound.conf"
else
    echo -e "${RED}  - Warning: config/alsa/asound.conf not found${NC}"
    echo "  - Creating minimal ALSA config..."
    cat > /etc/asound.conf << 'EOF'
pcm.!default {
    type hw
    card sndrpihifiberry
}
ctl.!default {
    type hw
    card sndrpihifiberry
}
EOF
fi

###############################################################################
# Step 6: Create Python Virtual Environment
###############################################################################
echo -e "${GREEN}[6/8] Setting up Python virtual environment...${NC}"
cd "$PROJECT_DIR"

# Remove old venv if exists
rm -rf .venv

# Create new venv with system site packages (for any system-installed packages)
sudo -u "$ACTUAL_USER" python3 -m venv .venv --system-site-packages

# Install Python dependencies
sudo -u "$ACTUAL_USER" .venv/bin/pip install --upgrade pip
sudo -u "$ACTUAL_USER" .venv/bin/pip install -r requirements.txt

echo "  - Virtual environment created at $PROJECT_DIR/.venv"

###############################################################################
# Step 7: Install Systemd Services
###############################################################################
echo -e "${GREEN}[7/8] Installing systemd services...${NC}"

# Copy service files
cp "$PROJECT_DIR/systemd/oled_splash.service" /etc/systemd/system/
cp "$PROJECT_DIR/systemd/dmx_audio_react.service" /etc/systemd/system/
echo "  - Installed oled_splash.service"
echo "  - Installed dmx_audio_react.service"

# Install dmx-dev command
chmod +x "$PROJECT_DIR/scripts/dmx-dev"
ln -sf "$PROJECT_DIR/scripts/dmx-dev" /usr/local/bin/dmx-dev
echo "  - Installed dmx-dev command"

# Reload and enable services
systemctl daemon-reload
systemctl enable oled_splash.service
systemctl enable dmx_audio_react.service
echo "  - Services enabled for boot"

###############################################################################
# Step 8: Verification
###############################################################################
echo -e "${GREEN}[8/8] Verifying installation...${NC}"

echo ""
echo "Checking configuration:"

# Check SPI
if [ -e /dev/spidev0.0 ] || [ -e /dev/spidev0.1 ]; then
    echo -e "  ${GREEN}✓${NC} SPI enabled"
else
    echo -e "  ${YELLOW}!${NC} SPI not yet enabled (will be after reboot)"
fi

# Check I2C
if [ -e /dev/i2c-1 ]; then
    echo -e "  ${GREEN}✓${NC} I2C enabled"
else
    echo -e "  ${YELLOW}!${NC} I2C not yet enabled (will be after reboot)"
fi

# Check UART
if [ -e /dev/serial0 ]; then
    echo -e "  ${GREEN}✓${NC} UART available"
else
    echo -e "  ${YELLOW}!${NC} UART not yet available (will be after reboot)"
fi

# Check Python venv
if [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
    echo -e "  ${GREEN}✓${NC} Python virtual environment"
else
    echo -e "  ${RED}✗${NC} Python virtual environment missing"
fi

# Check services
if systemctl is-enabled oled_splash.service &>/dev/null; then
    echo -e "  ${GREEN}✓${NC} oled_splash.service enabled"
else
    echo -e "  ${RED}✗${NC} oled_splash.service not enabled"
fi

if systemctl is-enabled dmx_audio_react.service &>/dev/null; then
    echo -e "  ${GREEN}✓${NC} dmx_audio_react.service enabled"
else
    echo -e "  ${RED}✗${NC} dmx_audio_react.service not enabled"
fi

# Check dmx-dev
if command -v dmx-dev &>/dev/null; then
    echo -e "  ${GREEN}✓${NC} dmx-dev command available"
else
    echo -e "  ${RED}✗${NC} dmx-dev command not found"
fi

###############################################################################
# Complete
###############################################################################
echo ""
echo -e "${BLUE}============================================${NC}"
echo -e "${GREEN}  Restoration Complete!${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""
echo "Next steps:"
echo "  1. Reboot the system: sudo reboot"
echo "  2. After reboot, verify audio: arecord -l"
echo "  3. Check service status: sudo systemctl status dmx_audio_react.service"
echo "  4. View logs: sudo journalctl -u dmx_audio_react.service -f"
echo ""
echo "For development mode:"
echo "  dmx-dev disable   # Stop service and prevent autostart"
echo "  dmx-dev enable    # Re-enable autostart"
echo "  dmx-dev status    # Check current mode"
echo ""
echo -e "${YELLOW}>>> REBOOT REQUIRED: sudo reboot${NC}"

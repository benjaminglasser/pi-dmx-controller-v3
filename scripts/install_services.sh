#!/bin/bash
# install_services.sh: Install DMX controller systemd services and dev mode toggle
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Installing DMX Audio React services..."
echo "Project directory: $PROJECT_DIR"
echo ""

# Check if running as root or with sudo
if [ "$EUID" -ne 0 ]; then
    echo "This script requires sudo privileges."
    echo "Please run: sudo $0"
    exit 1
fi

# Copy systemd service files
echo "Copying systemd service files..."
cp "$PROJECT_DIR/systemd/oled_splash.service" /etc/systemd/system/
cp "$PROJECT_DIR/systemd/dmx_audio_react.service" /etc/systemd/system/
echo "  - oled_splash.service"
echo "  - dmx_audio_react.service"

# Make dmx-dev executable and create symlink
echo ""
echo "Installing dmx-dev command..."
chmod +x "$PROJECT_DIR/scripts/dmx-dev"
ln -sf "$PROJECT_DIR/scripts/dmx-dev" /usr/local/bin/dmx-dev
echo "  - dmx-dev available in PATH"

# Check for cairosvg in venv
echo ""
echo "Checking Python dependencies..."
VENV_PIP="$PROJECT_DIR/.venv/bin/pip"
if [ -x "$VENV_PIP" ]; then
    if ! "$PROJECT_DIR/.venv/bin/python" -c "import cairosvg" 2>/dev/null; then
        echo "  - Installing cairosvg for SVG support..."
        "$VENV_PIP" install cairosvg
    else
        echo "  - cairosvg already installed"
    fi
else
    echo "  - Warning: Virtual environment not found at $PROJECT_DIR/.venv"
    echo "    SVG support may not work. Run: pip install cairosvg"
fi

# Check for csw.svg
echo ""
if [ -f "$PROJECT_DIR/assets/csw.svg" ]; then
    echo "Found csw.svg in assets folder."
else
    echo "Note: csw.svg not found in $PROJECT_DIR/assets/"
    echo "      The splash screen will fall back to logo.jpg"
    echo "      Place csw.svg in the assets folder for custom splash."
fi

# Reload systemd and enable services
echo ""
echo "Enabling systemd services..."
systemctl daemon-reload
systemctl enable oled_splash.service
systemctl enable dmx_audio_react.service
echo "  - Services enabled for boot"

echo ""
echo "============================================"
echo "Installation complete!"
echo ""
echo "The DMX controller will now:"
echo "  1. Show splash screen on OLED for 4 seconds at boot"
echo "  2. Automatically start dmx_audio_react.py"
echo ""
echo "To enter development mode (disable autostart):"
echo "  dmx-dev disable"
echo ""
echo "To re-enable autostart:"
echo "  dmx-dev enable"
echo ""
echo "To check status:"
echo "  dmx-dev status"
echo ""
echo "To start the service manually now:"
echo "  sudo systemctl start dmx_audio_react.service"
echo "============================================"

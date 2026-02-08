#!/usr/bin/env bash
set -euo pipefail

echo "[1/7] Update system"
sudo apt update
sudo apt -y full-upgrade

echo "[2/7] Core packages"
sudo apt install -y \
  git python3 python3-venv python3-pip \
  alsa-utils libportaudio2 portaudio19-dev libsndfile1 \
  ola ola-python \
  python3-pil i2c-tools

echo "[3/7] Enable SPI/I2C (non-interactive)"
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0

echo "[4/7] HiFiBerry overlay (ALSA) in /boot/firmware/config.txt"
CFG=/boot/firmware/config.txt

# Disable onboard audio if enabled
sudo sed -i 's/^dtparam=audio=on/# dtparam=audio=on/' "$CFG" || true
grep -q '^dtparam=audio=off' "$CFG" || echo 'dtparam=audio=off' | sudo tee -a "$CFG"

# Ensure HiFiBerry DAC+ADC overlay is present
grep -q '^dtoverlay=hifiberry-dacplusadc' "$CFG" || \
  echo 'dtoverlay=hifiberry-dacplusadc' | sudo tee -a "$CFG"

# Ensure I2S is on (uncomment or append)
if grep -q '^#dtparam=i2s=on' "$CFG"; then
  sudo sed -i 's/^#dtparam=i2s=on/dtparam=i2s=on/' "$CFG"
elif ! grep -q '^dtparam=i2s=on' "$CFG"; then
  echo 'dtparam=i2s=on' | sudo tee -a "$CFG"
fi

echo "[5/7] Python venv (with system packages so OLA Python is visible)"
cd ~/pi-dmx-controller-v2
rm -rf .venv
python3 -m venv .venv --system-site-packages
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "[6/7] OLA daemon + patch Universe 0 to DMXKing port 0"
sudo systemctl enable olad
sudo systemctl restart olad
sleep 2

# Find DMXking device ID and patch universe 0
DEV_ID=$(ola_dev_info | awk '/DMXking/{print id}{id=$2}')
echo "Detected DMXking device id: ${DEV_ID:-<fallback 10>}"
sudo ola_patch -d "${DEV_ID:-10}" -p 0 -u 0 || true

echo "[7/7] (Optional) Install & enable systemd services if present"

if [ -f systemd/pi-dmx.service ]; then
  echo "  - Installing pi-dmx.service"
  sudo cp systemd/pi-dmx.service /etc/systemd/system/pi-dmx.service
  sudo systemctl enable pi-dmx.service || true
fi

if [ -f systemd/oled_boot.service ]; then
  echo "  - Installing oled_boot.service"
  sudo cp systemd/oled_boot.service /etc/systemd/system/oled_boot.service
  sudo systemctl enable oled_boot.service || true
fi

sudo systemctl daemon-reload

echo
echo "Bootstrap complete."
echo ">>> Reboot strongly recommended: sudo reboot"
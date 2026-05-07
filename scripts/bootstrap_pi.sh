#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[1/7] Update system"
sudo apt-get update
sudo apt-get -y -o Dpkg::Options::="--force-confold" full-upgrade

echo "[2/7] Core packages"
# build-essential is required by scripts/install_oled_initramfs.sh (gcc -static)
sudo apt-get install -y -o Dpkg::Options::="--force-confold" \
  git python3 python3-venv python3-pip \
  alsa-utils libportaudio2 portaudio19-dev libsndfile1 \
  ola ola-python \
  python3-pil i2c-tools \
  build-essential

echo "[3/7] Enable SPI/I2C (non-interactive)"
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0

echo "[4/7] ALSA placeholder capture (Pi USB-input-only installs — avoids PortAudio device count zero)"
sudo sh -c 'echo snd-dummy >/etc/modules-load.d/pi-dmx-alsa-placeholder.conf'
sudo modprobe snd-dummy 2>/dev/null || true

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

if [ -f systemd/oled_splash.service ]; then
  echo "  - Installing oled_splash.service"
  sudo cp systemd/oled_splash.service /etc/systemd/system/oled_splash.service
  sudo systemctl enable oled_splash.service || true
  # Remove oled_wake if present (caused ordering cycle)
  sudo systemctl disable oled_wake.service 2>/dev/null || true
  sudo rm -f /etc/systemd/system/oled_wake.service
fi

sudo systemctl daemon-reload

echo
echo "Bootstrap complete."
echo ">>> Reboot strongly recommended: sudo reboot"
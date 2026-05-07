# Configuration Files

This directory contains all system configuration files needed to restore the DMX controller after an SD card failure or fresh OS install.

## Directory Structure

```
config/
├── boot/
│   └── config.txt      # Raspberry Pi firmware config (/boot/firmware/config.txt)
├── alsa/
│   └── asound.conf     # ALSA audio config (/etc/asound.conf)
└── README-config.md    # This file
```

## File Descriptions

### boot/config.txt

The complete Raspberry Pi firmware configuration. Key settings:

| Setting | Purpose |
|---------|---------|
| `dtoverlay=hifiberry-dacplusadcpro` | Enable HiFiBerry DAC+ ADC Pro HAT (use `hifiberry-dacplusadc` for non-Pro) |
| `dtparam=audio=off` | Disable onboard audio (conflicts with HiFiBerry) |
| `dtoverlay=disable-bt` | Disable Bluetooth to free UART for DMX |
| `enable_uart=1` | Enable UART for RS485 DMX output |
| `dtparam=spi=on` | Enable SPI for OLED display |
| `dtparam=i2c_arm=on` | Enable I2C for HiFiBerry codec |
| `dtparam=spi=on` | Enable SPI for OLED (SSD1322 256×64) |
| `dtoverlay=spi0-2cs,cs0_pin=0` | Use single SPI CS to free GPIO8 for encoder 5 |

**Installation:**
```bash
sudo cp config/boot/config.txt /boot/firmware/config.txt
```

### alsa/asound.conf

ALSA configuration that sets the HiFiBerry as the default audio device. Without this file, the system won't capture audio from the HiFiBerry ADC.

**Installation:**
```bash
sudo cp config/alsa/asound.conf /etc/asound.conf
```

## Quick Restore

To restore all config files at once:

```bash
cd ~/pi-dmx-controller-v2
sudo cp config/boot/config.txt /boot/firmware/config.txt
sudo cp config/alsa/asound.conf /etc/asound.conf
sudo reboot
```

See [docs/QUICKSTART.md](../docs/QUICKSTART.md) — canonical **fresh SD card / full restore** guide (includes UART, udev, ALSA, bootstrap, and non-`pi` users).

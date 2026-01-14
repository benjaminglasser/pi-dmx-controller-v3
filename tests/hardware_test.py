#!/usr/bin/env python3
"""
Hardware Test Script
Tests all connected hardware:
  - MCP3008 ADC (3 pots + 1 slider on CH0-CH3)
  - SPI OLED display (SSD1309 on CE1)
  - 2x Rotary Encoders with push buttons
  - Reset button (GPIO 25)

Wiring Summary:
  MCP3008 (SPI0 CE0):
    - CH0: Pot 1 (Center Freq)
    - CH1: Pot 2 (Q)
    - CH2: Pot 3 (Threshold)
    - CH3: Slider (Brightness)

  SPI OLED - Waveshare 2.42" SSD1309 (SPI0 CE1):
    - VCC:  3.3V
    - GND:  GND
    - DIN:  MOSI (GPIO 10, Pin 19)
    - CLK:  SCLK (GPIO 11, Pin 23)
    - CS:   CE1 (GPIO 7, Pin 26)
    - DC:   GPIO 24 (Pin 18)
    - RST:  GPIO 12 (Pin 32)

  Rotary Encoder 1:
    - CLK: GPIO 5
    - DT:  GPIO 6
    - SW:  GPIO 13

  Rotary Encoder 2:
    - CLK: GPIO 17
    - DT:  GPIO 27
    - SW:  GPIO 22

  Reset Button:
    - GPIO 25 (Pin 22) → Button → GND

Run: python tests/hardware_test.py
Press Ctrl+C to exit.
"""

import time
import sys
import threading
import os

# ============ SPI / MCP3008 ============
try:
    import spidev
    SPI_AVAILABLE = True
except ImportError:
    print("WARNING: spidev not installed. Run: pip install spidev")
    SPI_AVAILABLE = False

# ============ GPIO ============
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    print("WARNING: RPi.GPIO not installed")
    GPIO_AVAILABLE = False

# ============ OLED ============
try:
    from PIL import Image, ImageDraw, ImageFont
    from luma.core.interface.serial import spi as luma_spi
    from luma.oled.device import ssd1309
    OLED_AVAILABLE = True
except ImportError:
    print("WARNING: OLED libs not installed. Run: pip install luma.oled pillow")
    OLED_AVAILABLE = False

# ============ Configuration ============

# MCP3008 on SPI0 CE0
MCP_SPI_BUS = 0
MCP_SPI_DEV = 0  # CE0

# OLED on SPI0 CE1 (Waveshare 2.42" SSD1309)
OLED_SPI_DEV = 1  # CE1
OLED_RST_PIN = 12
OLED_DC_PIN = 24

# Rotary Encoder 1
ENC1_CLK = 5
ENC1_DT = 6
ENC1_SW = 13

# Rotary Encoder 2
ENC2_CLK = 17
ENC2_DT = 27
ENC2_SW = 22

# Reset button (separate from encoders)
RESET_PIN = 25

# Channel labels for MCP3008
CHANNEL_LABELS = [
    "Pot 1 (Freq)",
    "Pot 2 (Q)   ",
    "Pot 3 (Thr) ",
    "Slider (Brt)",
]

# ============ Global State ============
encoder1_value = 0
encoder2_value = 0
encoder1_button = False
encoder2_button = False
reset_button = False
reset_count = 0
reset_triggered = False
running = True
pot_values = [0, 0, 0, 0]

# EMA smoothing for pots (reduces jitter)
pot_ema = [None, None, None, None]
EMA_ALPHA = 0.3  # Lower = smoother but slower response

# ============ MCP3008 Functions ============

mcp_spi = None
mcp_lock = threading.Lock()

def init_mcp3008():
    global mcp_spi
    if not SPI_AVAILABLE:
        return False
    try:
        mcp_spi = spidev.SpiDev()
        mcp_spi.open(MCP_SPI_BUS, MCP_SPI_DEV)
        mcp_spi.max_speed_hz = 1000000  # 1MHz for stability
        mcp_spi.mode = 0
        return True
    except Exception as e:
        print(f"MCP3008 init failed: {e}")
        return False

def read_mcp3008(channel):
    global mcp_spi
    if mcp_spi is None or channel < 0 or channel > 7:
        return 0
    cmd = [1, (8 + channel) << 4, 0]
    with mcp_lock:
        try:
            resp = mcp_spi.xfer2(cmd)
        except Exception:
            return 0
    return ((resp[1] & 3) << 8) | resp[2]

def read_pot_smoothed(channel):
    """Read pot with EMA smoothing to reduce jitter."""
    raw = read_mcp3008(channel)
    if pot_ema[channel] is None:
        pot_ema[channel] = raw
    else:
        pot_ema[channel] = EMA_ALPHA * raw + (1 - EMA_ALPHA) * pot_ema[channel]
    return int(pot_ema[channel])

def value_to_bar(value, width=20):
    filled = int((value / 1023) * width)
    return "█" * filled + "░" * (width - filled)

# ============ OLED Functions ============

oled_device = None

def init_oled():
    global oled_device
    if not OLED_AVAILABLE:
        return False
    try:
        serial = luma_spi(
            device=OLED_SPI_DEV,  # CE1
            port=0,
            bus_speed_hz=8000000,
            gpio_DC=OLED_DC_PIN,
            gpio_RST=OLED_RST_PIN,
        )
        oled_device = ssd1309(serial, width=128, height=64, rotate=0)
        return True
    except Exception as e:
        print(f"OLED init failed: {e}")
        print("Make sure OLED is wired correctly:")
        print("  CS → CE1 (Pin 26), DC → GPIO24, RST → GPIO12")
        return False

def draw_oled():
    global oled_device
    if oled_device is None:
        return
    try:
        image = Image.new("1", (128, 64))
        draw = ImageDraw.Draw(image)
        
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 9)
        except:
            font = ImageFont.load_default()
        
        # Title
        draw.text((0, 0), "Hardware Test", font=font, fill=1)
        
        # Pot values (2x2 grid)
        draw.text((0, 12), f"P1:{int((pot_values[0]/1023)*100):3d}%", font=font, fill=1)
        draw.text((64, 12), f"P2:{int((pot_values[1]/1023)*100):3d}%", font=font, fill=1)
        draw.text((0, 22), f"P3:{int((pot_values[2]/1023)*100):3d}%", font=font, fill=1)
        draw.text((64, 22), f"SL:{int((pot_values[3]/1023)*100):3d}%", font=font, fill=1)
        
        # Encoder values
        btn1 = "*" if encoder1_button else " "
        btn2 = "*" if encoder2_button else " "
        draw.text((0, 36), f"E1:{encoder1_value:4d}{btn1}", font=font, fill=1)
        draw.text((64, 36), f"E2:{encoder2_value:4d}{btn2}", font=font, fill=1)
        
        # Reset button
        rst = "RST!" if reset_button else f"Rst:{reset_count}"
        draw.text((0, 50), rst, font=font, fill=1)
        
        oled_device.display(image)
    except Exception as e:
        pass  # Silently ignore OLED errors during operation

# ============ GPIO / Encoder / Reset Functions ============

def setup_gpio():
    if not GPIO_AVAILABLE:
        return False
    try:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        
        # Encoder 1
        GPIO.setup(ENC1_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENC1_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENC1_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        
        # Encoder 2
        GPIO.setup(ENC2_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENC2_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENC2_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        
        # Reset button
        GPIO.setup(RESET_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        
        return True
    except Exception as e:
        print(f"GPIO setup failed: {e}")
        return False

def gpio_thread():
    global encoder1_value, encoder2_value, encoder1_button, encoder2_button
    global reset_button, reset_count, reset_triggered, running
    
    if not GPIO_AVAILABLE:
        return
    
    enc1_last_clk = GPIO.input(ENC1_CLK)
    enc2_last_clk = GPIO.input(ENC2_CLK)
    last_reset = 1
    
    while running:
        try:
            # Encoder 1
            enc1_clk = GPIO.input(ENC1_CLK)
            enc1_dt = GPIO.input(ENC1_DT)
            enc1_sw = GPIO.input(ENC1_SW)
            
            if enc1_clk != enc1_last_clk:
                if enc1_dt != enc1_clk:
                    encoder1_value += 1
                else:
                    encoder1_value -= 1
            enc1_last_clk = enc1_clk
            encoder1_button = (enc1_sw == 0)
            
            # Encoder 2
            enc2_clk = GPIO.input(ENC2_CLK)
            enc2_dt = GPIO.input(ENC2_DT)
            enc2_sw = GPIO.input(ENC2_SW)
            
            if enc2_clk != enc2_last_clk:
                if enc2_dt != enc2_clk:
                    encoder2_value += 1
                else:
                    encoder2_value -= 1
            enc2_last_clk = enc2_clk
            encoder2_button = (enc2_sw == 0)
            
            # Reset button (GPIO 25)
            reset_state = GPIO.input(RESET_PIN)
            reset_button = (reset_state == 0)
            
            # Detect reset button press (falling edge)
            if last_reset == 1 and reset_state == 0:
                reset_triggered = True
                reset_count += 1
            last_reset = reset_state
            
            time.sleep(0.001)  # 1ms polling
        except Exception:
            break

# ============ Main ============

def main():
    global running, pot_values, reset_triggered
    
    print("=" * 50)
    print("Hardware Test Script")
    print("=" * 50)
    print()
    
    # Initialize MCP3008
    if init_mcp3008():
        print("✓ MCP3008 initialized (SPI0 CE0)")
    else:
        print("✗ MCP3008 not available")
    
    # Initialize OLED
    if init_oled():
        print("✓ OLED initialized (SPI0 CE1, SSD1309)")
    else:
        print("✗ OLED not available")
    
    # Initialize GPIO
    if setup_gpio():
        print("✓ GPIO initialized (Encoders + Reset)")
        gpio_t = threading.Thread(target=gpio_thread, daemon=True)
        gpio_t.start()
    else:
        print("✗ GPIO not available")
    
    print()
    print("Press Ctrl+C to exit.")
    print()
    time.sleep(1)
    
    try:
        while True:
            # Read pots with smoothing
            for ch in range(4):
                pot_values[ch] = read_pot_smoothed(ch)
            
            # Clear screen
            os.system('clear' if os.name == 'posix' else 'cls')
            
            # Display header
            print("Hardware Test (Ctrl+C to exit)")
            print("-" * 50)
            print()
            
            # MCP3008 readings
            print("Pots/Slider:")
            for ch in range(4):
                value = pot_values[ch]
                percent = (value / 1023) * 100
                bar = value_to_bar(value, width=20)
                label = CHANNEL_LABELS[ch]
                print(f"  {label}  {value:4d}  ({percent:5.1f}%)  {bar}")
            print()
            
            # Encoder readings
            print("Rotary Encoders:")
            btn1 = "[PRESSED]" if encoder1_button else ""
            btn2 = "[PRESSED]" if encoder2_button else ""
            print(f"  Encoder 1: {encoder1_value:6d}  {btn1}")
            print(f"  Encoder 2: {encoder2_value:6d}  {btn2}")
            print()
            
            # Reset button (GPIO 25)
            print("Reset Button (GPIO 25):")
            reset_status = "[PRESSED]" if reset_button else ""
            print(f"  Press count: {reset_count}  {reset_status}")
            if reset_triggered:
                print("  *** RESET TRIGGERED! ***")
                reset_triggered = False
            print()
            
            # OLED status
            if oled_device:
                print("OLED: Displaying live values ✓")
                draw_oled()
            else:
                print("OLED: Not connected")
            
            print()
            print("-" * 50)
            
            time.sleep(0.1)  # 10 Hz update
            
    except KeyboardInterrupt:
        print("\n\nExiting...")
    finally:
        running = False
        time.sleep(0.1)
        
        # Turn off OLED
        if oled_device:
            try:
                oled_device.clear()
                oled_device.hide()
            except Exception:
                pass
            print("OLED turned off.")
        
        if mcp_spi:
            mcp_spi.close()
        if GPIO_AVAILABLE:
            GPIO.cleanup()
        print("Done.")

if __name__ == "__main__":
    main()

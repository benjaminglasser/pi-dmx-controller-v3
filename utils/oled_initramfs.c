/*
 * Minimal SSD1322 OLED init for early boot (initramfs).
 * Lights up the display so user sees the Pi is alive.
 * Hardware: CE1 (spidev0.1), RST=GPIO12, DC=GPIO24, 256x64
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <linux/spi/spidev.h>

#define RST_PIN 12
#define DC_PIN  24
#define WIDTH   256
#define HEIGHT  64
#define SPI_DEV "/dev/spidev0.1"

static int gpio_export(int pin) {
    char buf[64];
    int fd = open("/sys/class/gpio/export", O_WRONLY);
    if (fd < 0) return -1;
    snprintf(buf, sizeof(buf), "%d", pin);
    write(fd, buf, strlen(buf));
    close(fd);
    return 0;
}

static int gpio_direction(int pin, const char *dir) {
    char path[64];
    int fd;
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/direction", pin);
    for (int i = 0; i < 20; i++) {
        fd = open(path, O_WRONLY);
        if (fd >= 0) {
            write(fd, dir, strlen(dir));
            close(fd);
            return 0;
        }
        usleep(50000);
    }
    return -1;
}

static void gpio_set(int pin, int val) {
    char path[64], c = val ? '1' : '0';
    int fd;
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/value", pin);
    fd = open(path, O_WRONLY);
    if (fd >= 0) {
        write(fd, &c, 1);
        close(fd);
    }
}

static int spi_fd = -1;

static void spi_cmd(uint8_t cmd) {
    gpio_set(DC_PIN, 0);
    write(spi_fd, &cmd, 1);
}

static void spi_cmd2(uint8_t c1, uint8_t c2) {
    uint8_t buf[2] = { c1, c2 };
    gpio_set(DC_PIN, 0);
    write(spi_fd, buf, 2);
}

static void spi_cmd3(uint8_t c1, uint8_t c2, uint8_t c3) {
    uint8_t buf[3] = { c1, c2, c3 };
    gpio_set(DC_PIN, 0);
    write(spi_fd, buf, 3);
}

static void spi_data(const uint8_t *data, size_t len) {
    gpio_set(DC_PIN, 1);
    write(spi_fd, data, len);
}

static void ssd1322_init(void) {
    gpio_export(RST_PIN);
    gpio_export(DC_PIN);
    usleep(100000);
    gpio_direction(RST_PIN, "out");
    gpio_direction(DC_PIN, "out");

    gpio_set(RST_PIN, 0);
    usleep(10000);
    gpio_set(RST_PIN, 1);
    usleep(100000);

    /* Init sequence from luma ssd1322 (256x64) */
    spi_cmd2(0xFD, 0x12);
    spi_cmd(0xA4);
    spi_cmd2(0xB3, 0xF2);
    spi_cmd2(0xCA, 0x3F);
    spi_cmd2(0xA2, 0x00);
    spi_cmd2(0xA1, 0x00);
    spi_cmd3(0xA0, 0x14, 0x11);
    spi_cmd2(0xB5, 0x00);
    spi_cmd2(0xAB, 0x01);
    spi_cmd3(0xB4, 0xA0, 0xFD);
    spi_cmd2(0xC7, 0x0F);
    spi_cmd(0xB9);
    spi_cmd2(0xB1, 0xF0);
    spi_cmd3(0xD1, 0x82, 0x20);
    spi_cmd2(0xBB, 0x0D);
    spi_cmd2(0xB6, 0x08);
    spi_cmd2(0xBE, 0x00);
    spi_cmd(0xA6);
    spi_cmd(0xA9);

    /* Turn display on */
    spi_cmd(0xAF);
}

static void ssd1322_fill(uint8_t grey) {
    uint8_t col[3], row[3], cmd;
    const size_t pix = (size_t)WIDTH * HEIGHT / 2;
    uint8_t *buf = malloc(pix);
    if (!buf) return;

    memset(buf, grey, pix);

    /* Set column addr: 28-91 for 256px centered in 480 */
    col[0] = 0x15; col[1] = 28; col[2] = 91;
    gpio_set(DC_PIN, 0);
    write(spi_fd, col, 3);

    /* Set row addr: 0-63 */
    row[0] = 0x75; row[1] = 0; row[2] = 63;
    write(spi_fd, row, 3);

    cmd = 0x5C;
    write(spi_fd, &cmd, 1);

    gpio_set(DC_PIN, 1);
    write(spi_fd, buf, pix);
    free(buf);
}

int main(int argc, char **argv) {
    uint8_t mode = 0;
    int fd;

    for (int i = 0; i < 30; i++) {
        fd = open(SPI_DEV, O_RDWR);
        if (fd >= 0) break;
        usleep(200000);
    }
    if (fd < 0) return 1;

    spi_fd = fd;

    mode = 0;
    ioctl(fd, SPI_IOC_WR_MODE, &mode);
    ioctl(fd, SPI_IOC_RD_MODE, &mode);

    uint8_t bpw = 8;
    ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bpw);
    ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, (void *)2000000);

    ssd1322_init();
    /* Brighter gray = 0x88 (was 0x55), hold 2s for visibility (was 0.5s) */
    ssd1322_fill(0x88);
    usleep(2000000);
    close(fd);
    return 0;
}

/*
 * Pico-MMU RS485 Driver — Linux process MCU transport
 *
 * Replaces rs485.c when building Klipper for MACH=linux (integration tests).
 * Instead of a hardware UART + RS485 DE pin, a plain TCP socket is used to
 * talk to the slave_simulator.py server that runs the real slave state machine.
 *
 * Environment variables (with defaults):
 *   SLAVE_SIM_HOST  (default "127.0.0.1")
 *   SLAVE_SIM_PORT  (default "9000")
 *
 * The wire format is identical to the physical RS485 bus: raw PMU binary
 * frames.  slave_simulator.py routes them to the correct SlaveNode pipes.
 *
 * Note: gpio_out_setup / gpio_out_write are no-ops in Klipper's Linux MCU
 * board layer, so the de_gpio field is valid but silent — no changes needed
 * to the shared rs485.h header or command_bridge.c.
 */

#include "rs485.h"
#include "protocol.h"

#include "board/gpio.h"   /* gpio_out_setup — no-op on Linux */

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

/* File descriptor for the simulator TCP connection (-1 = not connected). */
static int sim_fd = -1;

/* -------------------------------------------------------------------------
 * Internal helpers
 * ---------------------------------------------------------------------- */

/* Open (or re-open) a non-blocking TCP connection to the slave simulator. */
static int
_sim_connect(void)
{
    const char *host = getenv("SLAVE_SIM_HOST");
    const char *port_str = getenv("SLAVE_SIM_PORT");
    if (!host)
        host = "127.0.0.1";
    int port = port_str ? atoi(port_str) : 9000;

    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        perror("rs485_linux: socket");
        return -1;
    }

    /* Disable Nagle — we send small binary frames and want low latency. */
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port   = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
        fprintf(stderr, "rs485_linux: invalid SLAVE_SIM_HOST '%s'\n", host);
        close(fd);
        return -1;
    }

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("rs485_linux: connect");
        close(fd);
        return -1;
    }

    /* Set non-blocking AFTER connect so connect() itself is blocking. */
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    return fd;
}

/* Pull bytes from the socket into rs485->rx_buf ring buffer. */
static void
_drain_socket(struct pmu_rs485 *rs485)
{
    if (sim_fd < 0)
        return;

    uint8_t tmp[256];
    ssize_t n;
    while ((n = recv(sim_fd, tmp, sizeof(tmp), 0)) > 0) {
        for (ssize_t i = 0; i < n; i++) {
            uint16_t next = (rs485->rx_head + 1) & 0xFF;
            if (next != rs485->rx_tail) {  /* not full */
                rs485->rx_buf[rs485->rx_head] = tmp[i];
                rs485->rx_head = next;
            }
        }
    }
    /* EAGAIN / EWOULDBLOCK means nothing available right now — that's fine. */
    if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
        perror("rs485_linux: recv");
        close(sim_fd);
        sim_fd = -1;
    }
}

/* -------------------------------------------------------------------------
 * Public API (declared in rs485.h)
 * ---------------------------------------------------------------------- */

void
pmu_rs485_init(struct pmu_rs485 *rs485, const struct pmu_rs485_config *config)
{
    memcpy(&rs485->config, config, sizeof(*config));
    rs485->rx_head   = 0;
    rs485->rx_tail   = 0;
    rs485->seq_counter = 0;

    /* DE pin: no-op on Linux (gpio_out_setup returns a zeroed struct). */
    rs485->de_gpio = gpio_out_setup(config->de_pin, 0);

    if (sim_fd >= 0) {
        close(sim_fd);
        sim_fd = -1;
    }

    sim_fd = _sim_connect();
    if (sim_fd < 0)
        fprintf(stderr, "rs485_linux: WARNING — could not connect to slave simulator\n");
}

uint8_t
pmu_rs485_send(struct pmu_rs485 *rs485, uint8_t addr, uint8_t cmd,
               const uint8_t *data, uint8_t data_len)
{
    uint8_t seq = rs485->seq_counter++;
    uint8_t frame_buf[PMU_MAX_FRAME_SIZE];

    size_t frame_len = pmu_build_frame(frame_buf, addr, cmd, seq, data, data_len);
    if (frame_len == 0)
        return seq;

    pmu_rs485_send_raw(rs485, frame_buf, frame_len);
    return seq;
}

void
pmu_rs485_send_raw(struct pmu_rs485 *rs485, const uint8_t *frame, size_t len)
{
    if (sim_fd < 0)
        return;

    ssize_t sent = 0;
    while ((size_t)sent < len) {
        ssize_t n = write(sim_fd, frame + sent, len - (size_t)sent);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            perror("rs485_linux: write");
            close(sim_fd);
            sim_fd = -1;
            return;
        }
        sent += n;
    }
}

int
pmu_rs485_recv(struct pmu_rs485 *rs485, struct pmu_frame *frame)
{
    _drain_socket(rs485);

    /* Build a contiguous snapshot from the ring buffer for pmu_parse_frame. */
    uint16_t avail = (rs485->rx_head - rs485->rx_tail) & 0xFF;
    if (avail < PMU_MIN_FRAME_SIZE)
        return 0;

    /* Copy ring-buffer bytes into a linear staging buffer. */
    uint8_t staging[PMU_MAX_FRAME_SIZE + 1];
    uint16_t copy_len = avail < sizeof(staging) ? avail : (uint16_t)sizeof(staging);
    for (uint16_t i = 0; i < copy_len; i++)
        staging[i] = rs485->rx_buf[(rs485->rx_tail + i) & 0xFF];

    int result = pmu_parse_frame(staging, copy_len, frame);

    if (result > 0) {
        /* Advance ring tail by the number of consumed bytes. */
        uint8_t consumed = (uint8_t)(PMU_HEADER_SIZE + frame->data_len + PMU_CRC_SIZE);
        rs485->rx_tail = (rs485->rx_tail + consumed) & 0xFF;
        return 1;
    }
    if (result < 0) {
        /* Framing error — discard one byte and try again next call. */
        rs485->rx_tail = (rs485->rx_tail + 1) & 0xFF;
    }
    return 0;
}

void
pmu_rs485_flush_rx(struct pmu_rs485 *rs485)
{
    rs485->rx_head = 0;
    rs485->rx_tail = 0;

    /* Also drain the socket itself so stale bytes don't arrive later. */
    if (sim_fd >= 0) {
        uint8_t discard[256];
        while (recv(sim_fd, discard, sizeof(discard), 0) > 0)
            ;
    }
}

void
pmu_rs485_rx_isr(struct pmu_rs485 *rs485, uint8_t byte)
{
    /*
     * No hardware interrupt on Linux — bytes arrive via _drain_socket()
     * called from pmu_rs485_recv().  This stub is required to satisfy the
     * linker when command_bridge.c is compiled.
     */
    (void)rs485;
    (void)byte;
}

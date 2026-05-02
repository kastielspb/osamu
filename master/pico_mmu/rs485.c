/*
 * Pico-MMU RS485 UART Driver - Implementation
 * For Klipper MCU firmware on RP2350.
 *
 * Note: This implementation uses Klipper's internal APIs for GPIO, UART, and
 * timing. The actual integration requires linking against klipper/src objects.
 * Placeholders marked with KLIPPER_API indicate where Klipper functions are used.
 */

#include "rs485.h"
#include "protocol.h"
#include <string.h>

// --- Platform abstraction (Klipper MCU internals) ---
// These would be replaced by actual Klipper API calls in the final build.

// KLIPPER_API: GPIO operations
static inline void gpio_out_setup(uint8_t pin, uint8_t val) {
    // gpio_out_setup(pin, val) — Klipper internal
    (void)pin; (void)val;
}

static inline void gpio_out_write(uint8_t pin, uint8_t val) {
    // gpio_out_write(pin, val) — Klipper internal
    (void)pin; (void)val;
}

// KLIPPER_API: UART operations
static inline void uart_init(uint8_t bus, uint32_t baud, uint8_t tx, uint8_t rx) {
    (void)bus; (void)baud; (void)tx; (void)rx;
}

static inline void uart_write(uint8_t bus, const uint8_t *data, size_t len) {
    (void)bus; (void)data; (void)len;
}

static inline int uart_tx_complete(uint8_t bus) {
    (void)bus;
    return 1;
}

// KLIPPER_API: Timing
static inline void udelay(uint32_t us) {
    (void)us;
}

// --- Ring buffer helpers ---

static inline uint16_t ring_next(uint16_t idx) {
    return (idx + 1) & 0xFF;  // 256-byte ring
}

static inline uint16_t ring_available(const struct pmu_rs485 *rs485) {
    return (rs485->rx_head - rs485->rx_tail) & 0xFF;
}

static inline uint8_t ring_peek(const struct pmu_rs485 *rs485, uint16_t offset) {
    return rs485->rx_buf[(rs485->rx_tail + offset) & 0xFF];
}

static inline void ring_consume(struct pmu_rs485 *rs485, uint16_t count) {
    rs485->rx_tail = (rs485->rx_tail + count) & 0xFF;
}

// --- Implementation ---

void
pmu_rs485_init(struct pmu_rs485 *rs485, const struct pmu_rs485_config *config)
{
    memcpy(&rs485->config, config, sizeof(*config));
    rs485->rx_head = 0;
    rs485->rx_tail = 0;
    rs485->seq_counter = 0;

    // Configure DE pin as output, start in RX mode (LOW)
    gpio_out_setup(config->de_pin, 0);

    // Configure UART
    uart_init(config->uart_bus, config->baud, config->tx_pin, config->rx_pin);
}

void
pmu_rs485_rx_isr(struct pmu_rs485 *rs485, uint8_t byte)
{
    uint16_t next = ring_next(rs485->rx_head);
    if (next != rs485->rx_tail) {  // Not full
        rs485->rx_buf[rs485->rx_head] = byte;
        rs485->rx_head = next;
    }
    // If full, byte is dropped (overflow)
}

uint8_t
pmu_rs485_send(struct pmu_rs485 *rs485, uint8_t addr, uint8_t cmd,
               const uint8_t *data, uint8_t data_len)
{
    uint8_t seq = rs485->seq_counter++;

    size_t frame_len = pmu_build_frame(rs485->tx_buf, addr, cmd, seq, data, data_len);
    if (frame_len == 0)
        return seq;

    pmu_rs485_send_raw(rs485, rs485->tx_buf, frame_len);
    return seq;
}

void
pmu_rs485_send_raw(struct pmu_rs485 *rs485, const uint8_t *frame, size_t len)
{
    // Switch to TX mode
    gpio_out_write(rs485->config.de_pin, 1);
    udelay(5);  // Transceiver switching time

    // Send data
    uart_write(rs485->config.uart_bus, frame, len);

    // Wait for TX complete
    while (!uart_tx_complete(rs485->config.uart_bus))
        ;

    // Small guard time then switch back to RX
    udelay(50);
    gpio_out_write(rs485->config.de_pin, 0);
}

int
pmu_rs485_recv(struct pmu_rs485 *rs485, struct pmu_frame *frame)
{
    // Skip bytes until we find a preamble
    while (ring_available(rs485) > 0 && ring_peek(rs485, 0) != PMU_PREAMBLE) {
        ring_consume(rs485, 1);
    }

    uint16_t avail = ring_available(rs485);
    if (avail < PMU_MIN_FRAME_SIZE)
        return 0;

    // Copy available data to linear buffer for parsing
    uint8_t linear[PMU_MAX_FRAME_SIZE];
    uint16_t copy_len = avail < PMU_MAX_FRAME_SIZE ? avail : PMU_MAX_FRAME_SIZE;
    for (uint16_t i = 0; i < copy_len; i++) {
        linear[i] = ring_peek(rs485, i);
    }

    int result = pmu_parse_frame(linear, copy_len, frame);
    if (result > 0) {
        ring_consume(rs485, (uint16_t)result);
        return 1;
    } else if (result < 0) {
        // Invalid frame, discard preamble byte
        ring_consume(rs485, 1);
        return 0;
    }

    // result == 0: incomplete, need more data
    return 0;
}

void
pmu_rs485_flush_rx(struct pmu_rs485 *rs485)
{
    rs485->rx_tail = rs485->rx_head;
}

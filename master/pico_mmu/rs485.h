/*
 * Pico-MMU RS485 UART Driver - Header
 * Half-duplex RS485 with Direction Enable (DE) pin management.
 */

#ifndef PICO_MMU_RS485_H
#define PICO_MMU_RS485_H

#include <stdint.h>
#include <stddef.h>
#include "board/gpio.h"
#include "protocol.h"

// RS485 configuration
struct pmu_rs485_config {
    uint8_t uart_bus;       // UART peripheral index (0 or 1)
    uint8_t tx_pin;
    uint8_t rx_pin;
    uint8_t de_pin;         // Direction Enable pin
    uint32_t baud;
};

// RS485 instance state
struct pmu_rs485 {
    struct pmu_rs485_config config;
    struct gpio_out de_gpio;    // Direction Enable GPIO handle (Klipper board/gpio.h)
    // RX ring buffer
    uint8_t rx_buf[256];
    volatile uint16_t rx_head;
    volatile uint16_t rx_tail;
    // TX buffer
    uint8_t tx_buf[PMU_MAX_FRAME_SIZE];
    // Sequence counter
    uint8_t seq_counter;
};

/**
 * Initialize RS485 interface.
 * Configures UART, DE pin, and enables RX interrupt.
 */
void pmu_rs485_init(struct pmu_rs485 *rs485, const struct pmu_rs485_config *config);

/**
 * Send a frame over RS485.
 * Handles DE pin switching: TX mode → send → wait → RX mode.
 * Blocking call (waits for TX complete).
 *
 * @param rs485     RS485 instance
 * @param addr      Target address
 * @param cmd       Command byte
 * @param data      Payload (can be NULL)
 * @param data_len  Payload length
 * @return          Sequence number used
 */
uint8_t pmu_rs485_send(struct pmu_rs485 *rs485, uint8_t addr, uint8_t cmd,
                       const uint8_t *data, uint8_t data_len);

/**
 * Send a raw pre-built frame over RS485.
 */
void pmu_rs485_send_raw(struct pmu_rs485 *rs485, const uint8_t *frame, size_t len);

/**
 * Try to receive a frame (non-blocking).
 * Reads from RX ring buffer, attempts to parse a complete frame.
 *
 * @param rs485     RS485 instance
 * @param frame     Output frame (filled on success)
 * @return          1 if frame received, 0 if no complete frame available
 */
int pmu_rs485_recv(struct pmu_rs485 *rs485, struct pmu_frame *frame);

/**
 * Flush RX buffer (discard all pending data).
 */
void pmu_rs485_flush_rx(struct pmu_rs485 *rs485);

/**
 * UART RX interrupt handler.
 * Call this from the UART IRQ to feed bytes into the ring buffer.
 */
void pmu_rs485_rx_isr(struct pmu_rs485 *rs485, uint8_t byte);

#endif // PICO_MMU_RS485_H

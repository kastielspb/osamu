/*
 * Pico-MMU Command Bridge - Klipper MCU command registration
 *
 * Registers custom MCU commands that allow the Klipper host (extras module)
 * to send/receive RS485 frames through the Master MCU.
 *
 * Klipper MCU commands registered:
 *   config_pmu_rs485 — configure RS485 interface
 *   pmu_rs485_send   — send a frame to a slave
 *   pmu_rs485_query  — send and wait for response (with timeout)
 *
 * Klipper MCU responses:
 *   pmu_rs485_rx     — received frame from slave (async callback to host)
 */

#include <string.h>
#include "rs485.h"
#include "protocol.h"

#include "board/gpio.h"
#include "board/misc.h"
#include "command.h"
#include "sched.h"
#include "basecmd.h"

// --- Module state ---
static struct pmu_rs485 rs485_bus;
static uint8_t bus_configured = 0;

// Pending query state
static struct {
    uint8_t active;
    uint8_t seq;
    uint8_t addr;
    uint32_t deadline;  // Timer ticks
} pending_query;

// --- Klipper command handlers ---

/*
 * config_pmu_rs485 uart_bus=%c tx_line=%u rx_line=%u de_line=%u baud=%u
 *
 * Configure the RS485 bus interface.
 */
void
command_config_pmu_rs485(uint32_t *args)
{
    struct pmu_rs485_config config;
    config.uart_bus = args[0];
    config.tx_pin = args[1];
    config.rx_pin = args[2];
    config.de_pin = args[3];
    config.baud = args[4];

    pmu_rs485_init(&rs485_bus, &config);
    bus_configured = 1;

    // Reset pending query
    pending_query.active = 0;
}
DECL_COMMAND(command_config_pmu_rs485,
             "config_pmu_rs485 uart_bus=%c tx_line=%u rx_line=%u de_line=%u baud=%u");

/*
 * pmu_rs485_send addr=%c cmd=%c data=%*s
 *
 * Send a frame to a slave. Fire-and-forget (no response expected from host perspective).
 * Response from slave will be delivered via pmu_rs485_rx callback.
 */
void
command_pmu_rs485_send(uint32_t *args)
{
    if (!bus_configured)
        return;

    uint8_t addr = args[0];
    uint8_t cmd = args[1];
    uint8_t data_len = args[2];
    uint8_t *data = command_decode_ptr(args[3]);

    pmu_rs485_send(&rs485_bus, addr, cmd, data, data_len);
}
DECL_COMMAND(command_pmu_rs485_send, "pmu_rs485_send addr=%c cmd=%c data=%*s");

/*
 * pmu_rs485_query addr=%c cmd=%c data=%*s timeout=%u
 *
 * Send a frame and wait for response within timeout (in timer ticks).
 * Response will be delivered via pmu_rs485_rx callback to host.
 */
void
command_pmu_rs485_query(uint32_t *args)
{
    if (!bus_configured)
        return;

    uint8_t addr = args[0];
    uint8_t cmd = args[1];
    uint8_t data_len = args[2];
    uint8_t *data = command_decode_ptr(args[3]);
    uint32_t timeout = args[4];

    // Flush any stale RX data
    pmu_rs485_flush_rx(&rs485_bus);

    uint8_t seq = pmu_rs485_send(&rs485_bus, addr, cmd, data, data_len);

    // Set up pending query state for the task loop
    pending_query.active = 1;
    pending_query.seq = seq;
    pending_query.addr = addr;
    pending_query.deadline = timer_read_time() + timeout;
}
DECL_COMMAND(command_pmu_rs485_query,
             "pmu_rs485_query addr=%c cmd=%c data=%*s timeout=%u");

/*
 * Response message sent to host when a frame is received from a slave.
 * Format: pmu_rs485_rx addr=%c cmd=%c seq=%c data=%*s
 */
static void
send_rx_to_host(const struct pmu_frame *frame)
{
    sendf("pmu_rs485_rx addr=%c cmd=%c seq=%c data=%*s",
          frame->addr, frame->cmd, frame->seq,
          frame->data_len, frame->data);
}

// --- Task: periodic RS485 RX check ---

/*
 * Called from Klipper scheduler task loop.
 * Checks for received frames and dispatches to host.
 */
void
pmu_rs485_task(void)
{
    if (!bus_configured)
        return;

    struct pmu_frame frame;

    // Try to receive frames
    while (pmu_rs485_recv(&rs485_bus, &frame)) {
        // Only forward responses (bit7 set) to host
        if (frame.cmd & PMU_CMD_DIR_RESPONSE) {
            send_rx_to_host(&frame);

            // Check if this completes a pending query
            if (pending_query.active &&
                frame.addr == pending_query.addr &&
                frame.seq == pending_query.seq) {
                pending_query.active = 0;
            }
        }
    }

    // Check query timeout
    if (pending_query.active) {
        if (timer_is_before(pending_query.deadline, timer_read_time())) {
            // Deadline passed with no matching response — send empty reply to
            // unblock the host-side lookup_query_command future.
            struct pmu_frame timeout_frame;
            timeout_frame.addr = pending_query.addr;
            timeout_frame.cmd = PMU_CMD_DIR_RESPONSE;
            timeout_frame.seq = pending_query.seq;
            timeout_frame.data_len = 0;
            send_rx_to_host(&timeout_frame);
            pending_query.active = 0;
        } else {
            // Keep the task loop awake so we can poll the TCP socket
            // continuously while waiting for the slave's response.
            sched_wake_tasks();
        }
    }
}
DECL_TASK(pmu_rs485_task);

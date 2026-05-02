/*
 * Pico-MMU RS485 Protocol - Frame encoding/decoding and CRC-16/MODBUS
 * Part of Klipper MCU firmware for RP2350 Master node.
 */

#ifndef PICO_MMU_PROTOCOL_H
#define PICO_MMU_PROTOCOL_H

#include <stdint.h>
#include <stddef.h>

// Frame constants
#define PMU_PREAMBLE        0xAA
#define PMU_MAX_DATA_LEN    64
#define PMU_HEADER_SIZE     5   // PREAMBLE + ADDR + CMD + SEQ + LEN
#define PMU_CRC_SIZE        2
#define PMU_MIN_FRAME_SIZE  (PMU_HEADER_SIZE + PMU_CRC_SIZE)
#define PMU_MAX_FRAME_SIZE  (PMU_HEADER_SIZE + PMU_MAX_DATA_LEN + PMU_CRC_SIZE)

// Addresses
#define PMU_ADDR_BROADCAST   0x00
#define PMU_ADDR_UNASSIGNED  0xFF

// CMD direction bit
#define PMU_CMD_DIR_RESPONSE 0x80

// Command codes
#define PMU_CMD_PING         0x01
#define PMU_CMD_DISCOVER     0x02
#define PMU_CMD_ASSIGN_ADDR  0x03
#define PMU_CMD_GET_STATUS   0x04
#define PMU_CMD_FEED         0x05
#define PMU_CMD_RETRACT      0x06
#define PMU_CMD_SET_ASSIST   0x07
#define PMU_CMD_STOP         0x08
#define PMU_CMD_STOP_ALL     0x09
#define PMU_CMD_SET_LED              0x0A
#define PMU_CMD_GET_CONFIG           0x0B
#define PMU_CMD_SET_CURRENT          0x0C
#define PMU_CMD_HOME_SLOT            0x0D
#define PMU_CMD_SET_FILAMENT_COLOR   0x0E

// Status codes
#define PMU_STATUS_OK              0x00
#define PMU_STATUS_BUSY            0x01
#define PMU_STATUS_ERR_SLOT_EMPTY  0x02
#define PMU_STATUS_ERR_JAM         0x03
#define PMU_STATUS_ERR_TIMEOUT     0x04
#define PMU_STATUS_ERR_INVALID     0x05
#define PMU_STATUS_UNKNOWN_CMD     0xFF

// Parsed frame structure
struct pmu_frame {
    uint8_t addr;
    uint8_t cmd;        // Raw cmd byte (includes direction bit)
    uint8_t seq;
    uint8_t data_len;
    uint8_t data[PMU_MAX_DATA_LEN];
};

/**
 * Compute CRC-16/MODBUS.
 * @param data  Pointer to data buffer
 * @param len   Number of bytes
 * @return      16-bit CRC value
 */
uint16_t pmu_crc16(const uint8_t *data, size_t len);

/**
 * Build a complete RS485 frame into the output buffer.
 * @param buf       Output buffer (must be at least PMU_MAX_FRAME_SIZE)
 * @param addr      Device address
 * @param cmd       Command byte (caller sets direction bit)
 * @param seq       Sequence number
 * @param data      Payload data (can be NULL if data_len == 0)
 * @param data_len  Payload length (0-64)
 * @return          Total frame length in bytes, or 0 on error
 */
size_t pmu_build_frame(uint8_t *buf, uint8_t addr, uint8_t cmd,
                       uint8_t seq, const uint8_t *data, uint8_t data_len);

/**
 * Parse a frame from a buffer.
 * @param buf       Input buffer
 * @param buf_len   Available bytes in buffer
 * @param frame     Output parsed frame structure
 * @return          Number of bytes consumed, or 0 if incomplete/invalid
 *                  Negative value indicates error (discard first byte and retry)
 */
int pmu_parse_frame(const uint8_t *buf, size_t buf_len, struct pmu_frame *frame);

#endif // PICO_MMU_PROTOCOL_H

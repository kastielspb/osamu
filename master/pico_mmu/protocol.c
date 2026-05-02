/*
 * Pico-MMU RS485 Protocol - Implementation
 */

#include "protocol.h"
#include <string.h>

uint16_t
pmu_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int j = 0; j < 8; j++) {
            if (crc & 0x0001)
                crc = (crc >> 1) ^ 0xA001;
            else
                crc >>= 1;
        }
    }
    return crc;
}

size_t
pmu_build_frame(uint8_t *buf, uint8_t addr, uint8_t cmd,
                uint8_t seq, const uint8_t *data, uint8_t data_len)
{
    if (data_len > PMU_MAX_DATA_LEN)
        return 0;

    // Header
    buf[0] = PMU_PREAMBLE;
    buf[1] = addr;
    buf[2] = cmd;
    buf[3] = seq;
    buf[4] = data_len;

    // Data
    if (data_len > 0 && data != NULL)
        memcpy(&buf[PMU_HEADER_SIZE], data, data_len);

    // CRC-16 over header + data
    size_t payload_len = PMU_HEADER_SIZE + data_len;
    uint16_t crc = pmu_crc16(buf, payload_len);
    buf[payload_len] = (uint8_t)(crc & 0xFF);       // Low byte
    buf[payload_len + 1] = (uint8_t)(crc >> 8);     // High byte

    return payload_len + PMU_CRC_SIZE;
}

int
pmu_parse_frame(const uint8_t *buf, size_t buf_len, struct pmu_frame *frame)
{
    if (buf_len < PMU_MIN_FRAME_SIZE)
        return 0;  // Need more data

    // Check preamble
    if (buf[0] != PMU_PREAMBLE)
        return -1;  // Invalid, discard byte

    uint8_t data_len = buf[4];
    if (data_len > PMU_MAX_DATA_LEN)
        return -1;  // Invalid length

    size_t expected_size = PMU_HEADER_SIZE + data_len + PMU_CRC_SIZE;
    if (buf_len < expected_size)
        return 0;  // Incomplete, need more data

    // Verify CRC
    size_t payload_len = PMU_HEADER_SIZE + data_len;
    uint16_t crc_received = (uint16_t)buf[payload_len] |
                            ((uint16_t)buf[payload_len + 1] << 8);
    uint16_t crc_computed = pmu_crc16(buf, payload_len);

    if (crc_received != crc_computed)
        return -1;  // CRC mismatch

    // Fill frame structure
    frame->addr = buf[1];
    frame->cmd = buf[2];
    frame->seq = buf[3];
    frame->data_len = data_len;
    if (data_len > 0)
        memcpy(frame->data, &buf[PMU_HEADER_SIZE], data_len);

    return (int)expected_size;
}

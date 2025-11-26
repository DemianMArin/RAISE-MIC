# Complete Data Flow Explanation

## 1. **ADC Data Acquisition**
- 12-bit ADC samples at 16 kHz (every 62.5 μs)
- Stored as `int16_t` in `saadc_sample_buffer[2][2000]` (double-buffering)
- Two buffers allow one to fill while the other is being processed

```c
// Line 32-34
#define SAADC_SAMPLE_INTERVAL_US 62.5 // 16k Hz sample rate
#define SAADC_BUFFER_SIZE 2000// 2000 samples = 250ms of audio at 16kHz, uses 8KB RAM total
static int16_t saadc_sample_buffer[2][SAADC_BUFFER_SIZE];
```

## 2. **Buffer Fill Event**
- When a buffer fills with 2000 samples, `NRFX_SAADC_EVT_DONE` fires
- The event handler prepares the next buffer for sampling (alternates between buffer 0 and 1)

```c
// Line 118-120
case NRFX_SAADC_EVT_BUF_REQ:
    // Set up the next available buffer. Alternate between buffer 0 and 1
    err = nrfx_saadc_buffer_set(saadc_sample_buffer[(saadc_current_buffer++) % 2], SAADC_BUFFER_SIZE);
```

## 3. **Buffer Queuing**
- When buffer is filled, increment sequence number and queue the buffer
- Buffer pointer and size are put into `buffer_msgq` (max 8 messages)
- If queue is full, the **incoming buffer is dropped** (newest data lost)

```c
// Line 127-128
case NRFX_SAADC_EVT_DONE:
    ble_tx_state.seq_num++; // New sequence of buffer size SAADC_BUFFER_SIZE full =)
```

```c
// Line 159-167
struct buffer_msg msg;
msg.buffer = (int16_t *)p_event->data.done.p_buffer;
msg.size = p_event->data.done.size;

// Try to queue the buffer (non-blocking - we're in Interrupt Service Routine (ISR)!)
if (k_msgq_put(&buffer_msgq, &msg, K_NO_WAIT) != 0) {
    // Queue full! Audio data will be dropped
    LOG_WRN("Buffer queue full! Dropping %d samples", msg.size);
}
```

## 4. **Work Handler Scheduling**
- After queuing (or dropping), schedule `ble_send_work_handler` to process the queue

```c
// Line 169-170
// ALWAYS trigger work handler to drain queue (even if this buffer was dropped)
k_work_submit(&ble_send_work);
```

## 5. **Start Sending a Buffer**
- Check if currently sending a buffer using `ble_tx_state.is_sending`
- If not, get next buffer from queue and initialize transmission state

```c
// Line 386-397
// Check if we need to get a new buffer from the queue
if (!ble_tx_state.is_sending) {
    struct buffer_msg msg;

    // Try to get a buffer from the message queue
    if (k_msgq_get(&buffer_msgq, &msg, K_NO_WAIT) == 0) {
        // Got a buffer! Start sending it
        ble_tx_state.current_buffer = msg.buffer;
        ble_tx_state.total_samples = msg.size;
        ble_tx_state.packet_index = 0;
        ble_tx_state.samples_sent = 0;
        ble_tx_state.is_sending = true;
```

## 6. **Pre-transmission Checks**
- Verify streaming is enabled (default: true)
- Verify GATT notifications are enabled
- Verify device is connected

```c
// Line 406-412
// Check if we can send (connected and notifications enabled)
if (!streaming_enabled || !notify_enabled || !current_conn) {
    LOG_WRN("Cannot send: not ready (streaming=%d, notify=%d, conn=%p)",
            streaming_enabled, notify_enabled, current_conn);
    ble_tx_state.is_sending = false;
    return;
}
```

## 7. **Packet Creation**
- Split buffer (2000 samples) into chunks of 118 samples
- Total packets per buffer: ⌈2000/118⌉ = **17 packets**
- Packet size: 8 bytes header + (118 × 2) bytes data = **244 bytes**

**Packet Structure:**
```
[4 bytes] seq_num      - Buffer sequence number (which 2000-sample buffer)
[2 bytes] sample_count - Samples in THIS packet (usually 118, last packet ~56)
[2 bytes] packet_index - Packet number within current buffer (0-16)
[236 bytes] samples    - Audio data (118 int16_t samples in little-endian)
```

```c
// Line 414-447
// Calculate how many samples to send in this chunk
size_t remaining = ble_tx_state.total_samples - ble_tx_state.samples_sent;
size_t chunk_size = MIN(SAMPLES_PER_PACKET, remaining);

...

uint8_t packet[PACKET_HEADER_SIZE + SAMPLES_PER_PACKET * 2];
size_t len = sizeof(packet);

// Header: sequence number (4 bytes, little-endian)
sys_put_le32(ble_tx_state.seq_num, &packet[0]);

// Header: sample count (2 bytes, little-endian)
sys_put_le16(chunk_size, &packet[4]);

// Header: packet index (2 bytes, little-endian)
sys_put_le16(ble_tx_state.packet_index, &packet[6]);
```

## 8. **BLE Transmission**
- Send packet via BLE notification
- Register callback `notify_cb` to trigger next chunk after successful send

```c
// Line 454-463
// Send via BLE
struct bt_gatt_notify_params params = {
    .attr = &stream_svc.attrs[3],
    .data = packet,
    .len = PACKET_HEADER_SIZE + chunk_size * 2,
    .func = notify_cb,
    .user_data = NULL,
};

err = bt_gatt_notify_cb(current_conn, &params);
```

## 9. **Error Handling**
- If error is `-ENOMEM` (error code -12): BLE TX buffers are full
- **Entire current buffer (all 2000 samples) is abandoned**
- Set `is_sending = false` to allow processing next buffer from queue
- No retry for abandoned buffer - it's lost

```c
// Line 464-473
if (err != 0) {
    if (err == -12) {  // ENOMEM - BLE TX buffers full
        LOG_WRN("BLE TX buffers full, backing off");
        // Don't retry immediately - will be triggered by next notify_cb or buffer
        ble_tx_state.is_sending = false;  // Release current buffer
    } else {
        LOG_ERR("bt_gatt_notify_cb failed: %d", err);
    }
    return;
}
```

## 10. **Progress Tracking**
- Update samples sent counter and packet index
- Check if all samples in buffer have been sent

```c
// Line 475-478
// Update progress
ble_tx_state.samples_sent += chunk_size;
ble_tx_state.packet_index++;
// ble_tx_state.seq_num++;
```

## 11. **Buffer Completion**
- When all samples sent, mark buffer as complete
- Submit work to check if another buffer is waiting in queue
- If more buffers exist, start sending immediately

```c
// Line 480-489
// Check if buffer complete
if (ble_tx_state.samples_sent >= ble_tx_state.total_samples) {
    LOG_INF("Buffer sent completely (%d samples in %d packets)",
            ble_tx_state.total_samples,
            (ble_tx_state.total_samples + SAMPLES_PER_PACKET - 1) / SAMPLES_PER_PACKET);
    ble_tx_state.is_sending = false;

    // Check if there's another buffer waiting in queue
    k_work_submit(&ble_send_work);
}
```

## 12. **Next Chunk Trigger**
- When BLE notification completes, `notify_cb` is called
- Callback triggers work handler to send next chunk of current buffer

```c
// Line 301-311
static void notify_cb(struct bt_conn *conn, void *user_data)
{
    ARG_UNUSED(conn);
    ARG_UNUSED(user_data);

    // Previous packet sent successfully, trigger work handler to send next chunk
    if (ble_tx_state.is_sending) {
        k_work_submit(&ble_send_work);
    }
}
```

---

## Data Loss Points:
1. **Queue overflow**: When `buffer_msgq` is full (8 buffers backlog)
2. **BLE TX full**: When BLE transmission can't keep up with ADC sampling

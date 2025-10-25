/*
 * BLE ADC Streaming with Timer-Triggered Continuous Sampling (nRF54L15)
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
LOG_MODULE_REGISTER(RAISE_mic, LOG_LEVEL_DBG);


#include <zephyr/device.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/settings/settings.h>
#include <nrfx_saadc.h>
#include <nrfx_timer.h>
#include <helpers/nrfx_gppi.h>
#if defined(DPPI_PRESENT)
#include <nrfx_dppi.h>
#else
#include <nrfx_ppi.h>
#endif

// Audio sampling Defines
// ADC
#define NRF_SAADC_INPUT_AIN4 NRF_PIN_PORT_TO_PIN_NUMBER(11U, 1)
#define SAADC_INPUT_PIN NRF_SAADC_INPUT_AIN4
static nrfx_saadc_channel_t channel = NRFX_SAADC_DEFAULT_CHANNEL_SE(SAADC_INPUT_PIN, 0);
#define SAADC_SAMPLE_INTERVAL_US 125  // 1,000,000 / 125 = 8,000 Hz (8 kHz sample rate)
#define SAADC_BUFFER_SIZE 2000  // 2000 samples = 250ms of audio at 8kHz, uses 8KB RAM total
static int16_t saadc_sample_buffer[2][SAADC_BUFFER_SIZE];
static uint32_t saadc_current_buffer = 0;
//Timer
#define TIMER_INSTANCE_NUMBER 22
const nrfx_timer_t timer_instance = NRFX_TIMER_INSTANCE(TIMER_INSTANCE_NUMBER);


#define DEVICE_NAME CONFIG_BT_DEVICE_NAME
#define DEVICE_NAME_LEN (sizeof(CONFIG_BT_DEVICE_NAME) - 1)

static bool streaming_enabled = true;
static int16_t sample;
static uint8_t seq_num = 0;

/* UUIDs */
#define BT_UUID_STREAM_SERVICE_VAL BT_UUID_128_ENCODE(0x12345678,0x1234,0x5678,0x1234,0x56789abcdef0)
#define BT_UUID_CTRL_CHAR_VAL      BT_UUID_128_ENCODE(0x12345678,0x1234,0x5678,0x1234,0x56789abcdef1)
#define BT_UUID_DATA_CHAR_VAL      BT_UUID_128_ENCODE(0x12345678,0x1234,0x5678,0x1234,0x56789abcdef2)

static struct bt_uuid_128 stream_service_uuid = BT_UUID_INIT_128(BT_UUID_STREAM_SERVICE_VAL);
static struct bt_uuid_128 ctrl_char_uuid = BT_UUID_INIT_128(BT_UUID_CTRL_CHAR_VAL);
static struct bt_uuid_128 data_char_uuid = BT_UUID_INIT_128(BT_UUID_DATA_CHAR_VAL);

static struct bt_conn *current_conn;
static uint8_t ctrl_value = 0;
static uint8_t notify_enabled;

static struct k_work_delayable spam_work;

static void spam_notify(struct k_work *work);

// ============================================================================
// Message Queue and Work Thread for Audio Buffer Transmission
// ============================================================================

// Message structure for passing buffer info from SAADC ISR to work thread
struct buffer_msg {
    int16_t *buffer;    // Pointer to the filled buffer (4 bytes)
    size_t size;        // Number of samples in buffer (4 bytes)
};

// Message queue - holds up to 4 buffer messages (queue full = dropping audio data)
#define BUFFER_QUEUE_SIZE 4
K_MSGQ_DEFINE(buffer_msgq, sizeof(struct buffer_msg), BUFFER_QUEUE_SIZE, 4);

// Work item for BLE transmission (runs in thread context, not ISR)
static struct k_work ble_send_work;

// BLE transmission state - tracks chunked sending progress
static struct {
    int16_t *current_buffer;    // Buffer currently being sent
    size_t total_samples;       // Total samples in current buffer
    size_t samples_sent;        // How many samples already sent
    bool is_sending;            // True if actively sending a buffer
    uint32_t seq_num;           // Packet sequence number
} ble_tx_state = {
    .current_buffer = NULL,
    .total_samples = 0,
    .samples_sent = 0,
    .is_sending = false,
    .seq_num = 0
};

// BLE packet configuration
#define SAMPLES_PER_PACKET 50   // Send 50 samples per BLE packet (100 bytes + 6 byte header)
#define PACKET_HEADER_SIZE 6    // 4 bytes seq_num + 2 bytes sample_count

// Forward declaration
static void ble_send_work_handler(struct k_work *work);

/* SAADC Event Handler */
static void saadc_event_handler(nrfx_saadc_evt_t const *p_event)
{
    nrfx_err_t err;
    switch (p_event->type)
    {
        case NRFX_SAADC_EVT_READY:
            // Buffer is ready, timer (and sampling) can be started
            nrfx_timer_enable(&timer_instance);
            LOG_INF("SAADC ready, timer started");
            break;

        case NRFX_SAADC_EVT_BUF_REQ:
            // Set up the next available buffer. Alternate between buffer 0 and 1
            err = nrfx_saadc_buffer_set(saadc_sample_buffer[(saadc_current_buffer++) % 2], SAADC_BUFFER_SIZE);
            if (err != NRFX_SUCCESS) {
                LOG_ERR("nrfx_saadc_buffer_set error: %08x", err);
                return;
            }
            break;

        case NRFX_SAADC_EVT_DONE:
            // Buffer has been filled with samples - calculate stats for debugging
            int64_t average = 0;
            int16_t max = INT16_MIN;
            int16_t min = INT16_MAX;
            int16_t current_value;

            for (int i = 0; i < p_event->data.done.size; i++) {
                current_value = ((int16_t *)(p_event->data.done.p_buffer))[i];
                average += current_value;
                if (current_value > max) {
                    max = current_value;
                }
                if (current_value < min) {
                    min = current_value;
                }
            }
            average = average / p_event->data.done.size;

            LOG_INF("SAADC buffer filled: %d samples, AVG=%d, MIN=%d, MAX=%d",
                    p_event->data.done.size, (int16_t)average, min, max);

            // Queue buffer for BLE transmission
            struct buffer_msg msg;
            msg.buffer = (int16_t *)p_event->data.done.p_buffer;
            msg.size = p_event->data.done.size;

            // Try to queue the buffer (non-blocking - we're in ISR!)
            if (k_msgq_put(&buffer_msgq, &msg, K_NO_WAIT) != 0) {
                // Queue full! Audio data will be dropped
                LOG_WRN("Buffer queue full! Dropping %d samples", msg.size);
            }

            // ALWAYS trigger work handler to drain queue (even if this buffer was dropped)
            k_work_submit(&ble_send_work);
            break;

        default:
            LOG_INF("Unhandled SAADC evt %d", p_event->type);
            break;
    }
}

/* Timer Configuration */
static void configure_timer(void)
{
    nrfx_err_t err;

    // Configure timer to 1MHz frequency
    nrfx_timer_config_t timer_config = NRFX_TIMER_DEFAULT_CONFIG(1000000);
    err = nrfx_timer_init(&timer_instance, &timer_config, NULL);
    if (err != NRFX_SUCCESS) {
        LOG_ERR("nrfx_timer_init error: %08x", err);
        return;
    }

    // Set compare channel 0 to generate event every SAADC_SAMPLE_INTERVAL_US
    uint32_t timer_ticks = nrfx_timer_us_to_ticks(&timer_instance, SAADC_SAMPLE_INTERVAL_US);
    nrfx_timer_extended_compare(&timer_instance, NRF_TIMER_CC_CHANNEL0, timer_ticks,
                                 NRF_TIMER_SHORT_COMPARE0_CLEAR_MASK, false);

    LOG_INF("Timer configured: %d us interval (%d Hz)", SAADC_SAMPLE_INTERVAL_US,
            1000000 / SAADC_SAMPLE_INTERVAL_US);
}

/* SAADC Configuration */
static void configure_saadc(void)
{
    nrfx_err_t err;

    // Connect ADC interrupt to nrfx interrupt handler
    IRQ_CONNECT(DT_IRQN(DT_NODELABEL(adc)),
                DT_IRQ(DT_NODELABEL(adc), priority),
                nrfx_isr, nrfx_saadc_irq_handler, 0);

    // Initialize the nrfx_SAADC driver
    err = nrfx_saadc_init(DT_IRQ(DT_NODELABEL(adc), priority));
    if (err != NRFX_SUCCESS) {
        LOG_ERR("nrfx_saadc_init error: %08x", err);
        return;
    }
    LOG_INF("ADC initiated");

    // Change gain config and apply channel configuration
    channel.channel_config.gain = NRF_SAADC_GAIN1_4;
    err = nrfx_saadc_channels_config(&channel, 1);
    if (err != NRFX_SUCCESS) {
        LOG_ERR("nrfx_saadc_channels_config error: %08x", err);
        return;
    }
    LOG_INF("ADC channel config successful");

    // Configure channel 0 in advanced mode with event handler (non-blocking mode)
    nrfx_saadc_adv_config_t saadc_adv_config = NRFX_SAADC_DEFAULT_ADV_CONFIG;
    err = nrfx_saadc_advanced_mode_set(BIT(0),
                                        NRF_SAADC_RESOLUTION_12BIT,
                                        &saadc_adv_config,
                                        saadc_event_handler);
    if (err != NRFX_SUCCESS) {
        LOG_ERR("nrfx_saadc_advanced_mode_set error: %08x", err);
        return;
    }
    LOG_INF("ADC advanced mode set");

    // Configure two buffers to make use of double-buffering feature of SAADC
    err = nrfx_saadc_buffer_set(saadc_sample_buffer[0], SAADC_BUFFER_SIZE);
    if (err != NRFX_SUCCESS) {
        LOG_ERR("nrfx_saadc_buffer_set error: %08x", err);
        return;
    }
    err = nrfx_saadc_buffer_set(saadc_sample_buffer[1], SAADC_BUFFER_SIZE);
    if (err != NRFX_SUCCESS) {
        LOG_ERR("nrfx_saadc_buffer_set error: %08x", err);
        return;
    }
    LOG_INF("ADC buffers set (double-buffering enabled)");

    // Trigger the SAADC. This will not start sampling, but will prepare buffer for sampling triggered through DPPI
    err = nrfx_saadc_mode_trigger();
    if (err != NRFX_SUCCESS) {
        LOG_ERR("nrfx_saadc_mode_trigger error: %08x", err);
        return;
    }
    LOG_INF("SAADC mode triggered - ready for DPPI sampling");
}

/* DPPI Configuration */
static void configure_ppi(void)
{
    nrfx_err_t err;

    // Declare variables used to hold the (D)PPI channel number
    uint8_t m_saadc_sample_ppi_channel;
    uint8_t m_saadc_start_ppi_channel;

    // Allocate two DPPI channels
    err = nrfx_gppi_channel_alloc(&m_saadc_sample_ppi_channel);
    if (err != NRFX_SUCCESS) {
        LOG_ERR("nrfx_gppi_channel_alloc error: %08x", err);
        return;
    }

    err = nrfx_gppi_channel_alloc(&m_saadc_start_ppi_channel);
    if (err != NRFX_SUCCESS) {
        LOG_ERR("nrfx_gppi_channel_alloc error: %08x", err);
        return;
    }

    // Trigger task sample from timer compare event
    nrfx_gppi_channel_endpoints_setup(m_saadc_sample_ppi_channel,
                                      nrfx_timer_compare_event_address_get(&timer_instance, NRF_TIMER_CC_CHANNEL0),
                                      nrf_saadc_task_address_get(NRF_SAADC, NRF_SAADC_TASK_SAMPLE));

    // Trigger task start from SAADC end event (for continuous sampling)
    nrfx_gppi_channel_endpoints_setup(m_saadc_start_ppi_channel,
                                      nrf_saadc_event_address_get(NRF_SAADC, NRF_SAADC_EVENT_END),
                                      nrf_saadc_task_address_get(NRF_SAADC, NRF_SAADC_TASK_START));

    // Enable both (D)PPI channels
    nrfx_gppi_channels_enable(BIT(m_saadc_sample_ppi_channel));
    nrfx_gppi_channels_enable(BIT(m_saadc_start_ppi_channel));

    LOG_INF("DPPI configured - Timer -> SAADC connection established");
}

/* Callback when BLE notification is sent - triggers sending next chunk */
static void notify_cb(struct bt_conn *conn, void *user_data)
{
    ARG_UNUSED(conn);
    ARG_UNUSED(user_data);

    // Previous packet sent successfully, trigger work handler to send next chunk
    if (ble_tx_state.is_sending) {
        k_work_submit(&ble_send_work);
    }
}

static ssize_t write_ctrl(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                          const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
    if (len > 0) {
        ctrl_value = ((uint8_t *)buf)[0];
        streaming_enabled = (ctrl_value != 0);

        if (streaming_enabled) {
            k_work_schedule(&spam_work, K_NO_WAIT);
        } else {
            k_work_cancel_delayable(&spam_work);
        }
    }
    return len;
}

static void data_ccc_cfg_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
    notify_enabled = (value == BT_GATT_CCC_NOTIFY);
}

BT_GATT_SERVICE_DEFINE(stream_svc,
    BT_GATT_PRIMARY_SERVICE(&stream_service_uuid),
    BT_GATT_CHARACTERISTIC(&ctrl_char_uuid.uuid, BT_GATT_CHRC_WRITE,
                           BT_GATT_PERM_WRITE, NULL, write_ctrl, &ctrl_value),
    BT_GATT_CHARACTERISTIC(&data_char_uuid.uuid, BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_NONE, NULL, NULL, NULL),
    BT_GATT_CCC(data_ccc_cfg_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE)
);

static void spam_notify(struct k_work *work)
{
    if (!streaming_enabled || !notify_enabled || !current_conn) {
      printk("Conditions not met\n\r");
      return;
    }

    //nrfx_err_t err = nrfx_saadc_mode_trigger();
    //if (err != NRFX_SUCCESS) {
    //    k_work_schedule(&spam_work, K_MSEC(1));
    //    printk("ADC not working");
    //    return;
    //}

    /* int battery_voltage_mv = ((900 * 4) * sample) / (1 << 12);  */
    int battery_voltage_mv = 4096;

    uint8_t payload[5];
    payload[0] = seq_num++;
    sys_put_le32(battery_voltage_mv, &payload[1]);

    struct bt_gatt_notify_params params = {
        .attr = &stream_svc.attrs[3],
        .data = payload,
        .len = sizeof(payload),
        .func = notify_cb,
        .user_data = NULL,
    };

    uint8_t err = bt_gatt_notify_cb(current_conn, &params);
    if (err != 0) {
        // back off if failed
        k_work_schedule(&spam_work, K_MSEC(1));
    }
}

/* BLE Send Work Handler - sends audio buffer in chunks */
static void ble_send_work_handler(struct k_work *work)
{
    int err;

    // Check if we need to get a new buffer from the queue
    if (!ble_tx_state.is_sending) {
        struct buffer_msg msg;

        // Try to get a buffer from the message queue
        if (k_msgq_get(&buffer_msgq, &msg, K_NO_WAIT) == 0) {
            // Got a buffer! Start sending it
            ble_tx_state.current_buffer = msg.buffer;
            ble_tx_state.total_samples = msg.size;
            ble_tx_state.samples_sent = 0;
            ble_tx_state.is_sending = true;

            LOG_INF("Starting to send buffer: %d samples", msg.size);
        } else {
            // Queue empty, nothing to send
            return;
        }
    }

    // Check if we can send (connected and notifications enabled)
    if (!streaming_enabled || !notify_enabled || !current_conn) {
        LOG_WRN("Cannot send: not ready (streaming=%d, notify=%d, conn=%p)",
                streaming_enabled, notify_enabled, current_conn);
        ble_tx_state.is_sending = false;
        return;
    }

    // Calculate how many samples to send in this chunk
    size_t remaining = ble_tx_state.total_samples - ble_tx_state.samples_sent;
    size_t chunk_size = MIN(SAMPLES_PER_PACKET, remaining);

    /*
     * Build BLE Packet for Audio Transmission
     * =========================================
     * Packet Format (sent over BLE to receiver):
     *   Byte 0-3:  seq_num       (uint32_t, little-endian) - Packet sequence number
     *   Byte 4-5:  sample_count  (uint16_t, little-endian) - Number of samples in this packet
     *   Byte 6+:   samples       (int16_t array, little-endian) - Audio sample data
     *
     * Example with 50 samples:
     *   packet[0..3]   = 0x2A000000  (seq_num = 42)
     *   packet[4..5]   = 0x3200      (sample_count = 50)
     *   packet[6..7]   = 0xD5FF      (sample[0] = -43 as int16_le)
     *   packet[8..9]   = 0xE2FF      (sample[1] = -30 as int16_le)
     *   ...
     *   packet[104..105] = sample[49]
     *   Total size: 6 + (50 * 2) = 106 bytes
     */
    uint8_t packet[PACKET_HEADER_SIZE + SAMPLES_PER_PACKET * 2];

    // Header: sequence number (4 bytes, little-endian)
    sys_put_le32(ble_tx_state.seq_num, &packet[0]);

    // Header: sample count (2 bytes, little-endian)
    sys_put_le16(chunk_size, &packet[4]);

    // Payload: audio samples (chunk_size * 2 bytes, int16 little-endian)
    memcpy(&packet[6],
           &ble_tx_state.current_buffer[ble_tx_state.samples_sent],
           chunk_size * sizeof(int16_t));

    // Send via BLE
    struct bt_gatt_notify_params params = {
        .attr = &stream_svc.attrs[3],
        .data = packet,
        .len = PACKET_HEADER_SIZE + chunk_size * 2,
        .func = notify_cb,
        .user_data = NULL,
    };

    err = bt_gatt_notify_cb(current_conn, &params);
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

    // Update progress
    ble_tx_state.samples_sent += chunk_size;
    ble_tx_state.seq_num++;

    // Check if buffer complete
    if (ble_tx_state.samples_sent >= ble_tx_state.total_samples) {
        LOG_INF("Buffer sent completely (%d samples in %d packets)",
                ble_tx_state.total_samples,
                (ble_tx_state.total_samples + SAMPLES_PER_PACKET - 1) / SAMPLES_PER_PACKET);
        ble_tx_state.is_sending = false;

        // Check if there's another buffer waiting in queue
        k_work_submit(&ble_send_work);
    }

    // Otherwise, notify_cb will trigger next chunk when current packet is sent
}

static void connected(struct bt_conn *conn, uint8_t err)
{
    if (!err) {
        printk("Device connected =)\n\r");
        current_conn = bt_conn_ref(conn);
    }
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
    if (current_conn) {
        bt_conn_unref(current_conn);
        current_conn = NULL;
    }
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
    .connected = connected,
    .disconnected = disconnected,
};

/* Bluetooth Configuration */
static void configure_bluetooth(void)
{
    int err;

    // Enable Bluetooth
    err = bt_enable(NULL);
    if (err != 0) {
        LOG_ERR("Bluetooth init failed (err %d)", err);
        return;
    }
    LOG_INF("Bluetooth enabled");

    // Load settings if enabled
    if (IS_ENABLED(CONFIG_SETTINGS)) {
        settings_load();
    }

    // Set up advertising data
    const struct bt_data ad[] = {
        BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
        BT_DATA(BT_DATA_NAME_COMPLETE, DEVICE_NAME, DEVICE_NAME_LEN),
    };

    const struct bt_data sd[] = {
        BT_DATA_BYTES(BT_DATA_UUID128_ALL, BT_UUID_STREAM_SERVICE_VAL),
    };

    // Start advertising
    err = bt_le_adv_start(BT_LE_ADV_CONN, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));
    if (err != 0) {
        LOG_ERR("Advertising failed to start (err %d)", err);
        return;
    }
    LOG_INF("Bluetooth advertising started");
}

void main(void)
{
    LOG_INF("Starting RAISE mic application");
    LOG_INF("Sample rate: %d Hz, Buffer size: %d samples",
            1000000 / SAADC_SAMPLE_INTERVAL_US, SAADC_BUFFER_SIZE);
    LOG_INF("Data rate: ~%d KB/s",
            (1000000 / SAADC_SAMPLE_INTERVAL_US) * 2 / 1000);

    // Initialize work items
    // k_work_init_delayable(&spam_work, spam_notify);  // Legacy (for testing)
    k_work_init(&ble_send_work, ble_send_work_handler);  // Audio buffer transmission

    // Configure all peripherals
    configure_timer();
    configure_saadc();
    configure_ppi();
    configure_bluetooth();

    LOG_INF("All systems initialized - continuous sampling ready");
    LOG_INF("Waiting for connection and stream enable...");

    // Sleep forever - everything runs via interrupts and event handlers
    k_sleep(K_FOREVER);
}

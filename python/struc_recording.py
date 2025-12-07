"""
RAISE Microphone Recording Application - Updated for new packet format

Packet format from nRF54L15:
  [seq_num (4 bytes, uint32_le)][sample_count (2 bytes, uint16_le)][samples (count * 2 bytes, int16_le)]

TODO:
  - Bitrate calculation needs update (currently counts bytes, should count samples or be recalculated)
  - Packet loss calculation needs fix for 32-bit sequence numbers (currently assumes 8-bit wrapping)
"""

import sys
import asyncio
import time
import numpy as np
import sounddevice as sd
from bleak import BleakScanner, BleakClient
from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QLabel, QListWidget, QHBoxLayout
)
from PyQt5.QtCore import QTimer
import pyqtgraph as pg
from datetime import datetime
import csv
from qasync import QEventLoop
import wave
import os
from typing import List, Tuple, Optional
import lc3


# Optional MP3 support
try:
    from pydub import AudioSegment
    _PydubAvailable = True
except Exception:
    _PydubAvailable = False

DEVICE_NAME = "NEW_NAME"
CTRL_UUID = "12345678-1234-5678-1234-56789abcdef1"
DATA_UUID = "12345678-1234-5678-1234-56789abcdef2"

AUDIO_FS = 48000 # 48kHz (matches nRF54L15 configuration)
PACKET_HEADER_SIZE = 8
SAMPLES_PER_PACKET = 118
SAADC_BUFFER_SIZE = 2000

# Voltage plot configuration
VOLTAGE_PLOT_WINDOW = 1000  # Number of samples to display on x-axis
VOLTAGE_PLOT_Y_MIN = -400# Minimum y-axis value (ADC range: -2048 to 2047)
VOLTAGE_PLOT_Y_MAX = 400# Maximum y-axis value

#LC3 codec configuration
# 10,000 us / (1/48,000) =  480 samples
# 480 * 2 bytes (16 bit depth) = 960 bytes
ENC_BUF_SIZE = 120
DEC_BUF_SIZE = 960
PCM_SAMPLE_RATE = 48000
PCM_BIT_DEPTH = 16
LC3_BITRATE = 96000
LC3_FRAME_SIZE_US = 10000
LC3_NUM_CHANNELS = 1
AUDIO_CH_MONO = 0

class RAISEApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RAISE Microphone Testbench")
        self.resize(3000, 2000)
        self.client = None
        self.device = None
        self.connected = False
        self.recording = False
        self.streaming = False

        # Data stores
        self.analog_data: List[Optional[int]] = []       # mV integers (all-time), None for dropped packets
        self.audio_buffer: List[int] = []      # chunk buffer for playback
        self.packet_metadata: List[Tuple[int, int, int]] = []   # List of (seq_num, sample_count, packet_index) tuples for each packet

        # Metrics
        self.bitrate_values = []
        self.packet_loss_values = []
        self.snr_values = []
        self.last_seq = None
        self.last_time = time.time()

        # Recording segment tracking
        self.record_start_sample_idx: int = 0  # sample index in analog_data where recording began

        # Initialize LC3 decoder
        self.lc3_decoder = lc3.Decoder(
            frame_duration_us=LC3_FRAME_SIZE_US,  # 10000 us
            sample_rate_hz=PCM_SAMPLE_RATE         # 48000 Hz
        )
        print(f"LC3 Decoder initialized: {LC3_FRAME_SIZE_US}us @ {PCM_SAMPLE_RATE}Hz")

        self.setup_ui()
        self.scan_devices()

    def setup_ui(self):
        layout = QVBoxLayout()

        self.device_list = QListWidget()
        layout.addWidget(self.device_list)

        self.connect_btn = QPushButton("Connect to Selected Device")
        self.connect_btn.clicked.connect(self.connect_to_selected)
        layout.addWidget(self.connect_btn)

        self.toggle_btn = QPushButton("Start Listening")
        self.toggle_btn.clicked.connect(self.toggle_stream)
        layout.addWidget(self.toggle_btn)

        self.record_btn = QPushButton("Start Recording")
        self.record_btn.clicked.connect(self.toggle_recording_lc3codec)
        layout.addWidget(self.record_btn)

        self.labels = {
            "bitrate": QLabel("Bitrate: N/A"),
            "snr": QLabel("Normalized SNR: N/A"),
            "loss": QLabel("Packet Loss: N/A"),
            "voltage": QLabel("Voltage: N/A")
        }
        for label in self.labels.values():
            layout.addWidget(label)

        self.graphs = {
            "voltage": pg.PlotWidget(title="Analog Voltage (mV)"),
            "bitrate": pg.PlotWidget(title="Bitrate (bps)"),
            "snr": pg.PlotWidget(title="Normalized SNR (dB)"),
            "loss": pg.PlotWidget(title="Packet Loss (%)")
        }

        # Configure voltage plot with fixed y-axis range
        self.graphs["voltage"].setYRange(VOLTAGE_PLOT_Y_MIN, VOLTAGE_PLOT_Y_MAX, padding=0)

        self.graph_lines = {}
        for key, graph in self.graphs.items():
            self.graph_lines[key] = graph.plot(pen='g')
            layout.addWidget(graph)

        self.setLayout(layout)

        self.plot_timer = QTimer()
        self.plot_timer.timeout.connect(self.update_plots)
        self.plot_timer.start(200)

    def scan_devices(self):
        async def run():
            self.devices = await BleakScanner.discover()
            print("Devices found:", [(dev.name, dev.address) for dev in self.devices])
            self.device_list.clear()
            for dev in self.devices:
                self.device_list.addItem(f"{dev.name or 'Unknown'} - {dev.address}")

        asyncio.ensure_future(run())

    def connect_to_selected(self):
        index = self.device_list.currentRow
        index = self.device_list.currentRow()
        if index == -1:
            return
        self.device = self.devices[index]

        async def run():
            self.client = BleakClient(self.device)
            await self.client.connect()
            # await self.client.start_notify(DATA_UUID, self.handle_notify)
            await self.client.start_notify(DATA_UUID, self.handle_notify_lc3codec)

            self.connected = True
            print("Connected to", self.device.name)

        asyncio.ensure_future(run())

    def toggle_stream(self):
        if not self.client or not self.connected:
            return

        self.streaming = not self.streaming
        new_state = 0x01 if self.streaming else 0x00
        self.toggle_btn.setText("Stop Listening" if self.streaming else "Start Listening")

        async def run():
            await self.client.write_gatt_char(CTRL_UUID, bytearray([new_state]))
            print("Listening mode", "ON" if new_state else "OFF")
        asyncio.ensure_future(run())

    def toggle_recording(self):
        self.recording = not self.recording
        if self.recording:
            # Calculate the current sample index by summing all samples from previous packets
            sample_idx: int = sum(sample_count for seq_num, sample_count, packet_index in self.packet_metadata)
            self.record_start_sample_idx = sample_idx
            self.record_btn.setText("Stop Recording")
            print(f"Recording: START at sample index {sample_idx}")
        else:
            self.record_btn.setText("Start Recording")
            print("Recording: STOP")
            self.save_recording()

    def toggle_recording_lc3codec(self):
        """LC3 Codec version - no packet metadata tracking"""
        self.recording = not self.recording
        if self.recording:
            # Mark current position as recording start
            self.record_start_sample_idx = len(self.analog_data)
            self.record_btn.setText("Stop Recording")
            print(f"LC3 Recording: START at sample index {self.record_start_sample_idx}")
        else:
            # Mark current position as recording end and save
            self.record_btn.setText("Start Recording")
            print("LC3 Recording: STOP")
            self.save_recording_lc3codec()

    def update_plots(self):
        if self.analog_data:
            # Old code (fixed 100 samples):
            #self.graph_lines["voltage"].setData(self.analog_data[-100:])
            #self.labels["voltage"].setText(f"Voltage: {self.analog_data[-1]} mV")

            # New code (configurable window, handle None values):
            voltage_display = [v if v is not None else 0 for v in self.analog_data[-VOLTAGE_PLOT_WINDOW:]]
            self.graph_lines["voltage"].setData(voltage_display)

            # Show last non-None value in label
            last_value = self.analog_data[-1] if self.analog_data[-1] is not None else 0
            self.labels["voltage"].setText(f"Voltage: {last_value} mV")

        if self.bitrate_values:
            self.graph_lines["bitrate"].setData(self.bitrate_values[-100:])
            self.labels["bitrate"].setText(f"Bitrate: {self.bitrate_values[-1]:.1f} bps")

        if self.snr_values:
            self.graph_lines["snr"].setData(self.snr_values[-100:])
            self.labels["snr"].setText(f"Normalized SNR: {self.snr_values[-1]:.2f} dB")

        if self.packet_loss_values:
            self.graph_lines["loss"].setData(self.packet_loss_values[-100:])
            self.labels["loss"].setText(f"Packet Loss: {self.packet_loss_values[-1]:.1f}%")

    def handle_notify_lc3codec(self, handle, data):
        """
        LC3 Encoded Packet Format from nRF54L15
        ==========================================
        Packet layout (244 bytes total):
          [0:2]                     = encoded_bytes_written   (uint16_t)
          [2:2+ENC_BUF_SIZE]        = audio_encoded           (ENC_BUF_SIZE bytes)
          [2+ENC_BUF_SIZE:4+ENC_BUF_SIZE] = encoded_bytes_written_2 (uint16_t)
          [4+ENC_BUF_SIZE:244]      = audio_encoded_2         (ENC_BUF_SIZE bytes)

        Each LC3 frame decodes to 480 samples (10ms @ 48kHz)
        Total decoded output: 960 samples per packet
        """
        EXPECTED_PACKET_SIZE = (2 + ENC_BUF_SIZE) * 2

        if len(data) != EXPECTED_PACKET_SIZE:
            print(f"LC3 packet size error: {len(data)} bytes (expected {EXPECTED_PACKET_SIZE})")
            return

        # Extract first LC3 frame
        encoded_bytes_1 = int.from_bytes(data[0:2], byteorder='little', signed=False)
        audio_encoded_1 = data[2:2+ENC_BUF_SIZE]

        # Extract second LC3 frame
        frame2_offset = 2 + ENC_BUF_SIZE
        encoded_bytes_2 = int.from_bytes(data[frame2_offset:frame2_offset+2], byteorder='little', signed=False)
        audio_encoded_2 = data[frame2_offset+2:frame2_offset+2+ENC_BUF_SIZE]

        # Validate encoded sizes match expected
        if encoded_bytes_1 != ENC_BUF_SIZE:
            print(f"Frame 1 size mismatch: {encoded_bytes_1} bytes (expected {ENC_BUF_SIZE})")
            return
        if encoded_bytes_2 != ENC_BUF_SIZE:
            print(f"Frame 2 size mismatch: {encoded_bytes_2} bytes (expected {ENC_BUF_SIZE})")
            return

        # Decode both LC3 frames (returns raw bytes)
        try:
            decoded_bytes_1 = self.lc3_decoder.decode(audio_encoded_1, bit_depth=PCM_BIT_DEPTH)
            decoded_bytes_2 = self.lc3_decoder.decode(audio_encoded_2, bit_depth=PCM_BIT_DEPTH)
        except Exception as e:
            print(f"LC3 decode failed: {e}")
            return

        if len(decoded_bytes_1) != DEC_BUF_SIZE or len(decoded_bytes_2) != DEC_BUF_SIZE:
            print(f"Unexpected decoded size: frame1={len(decoded_bytes_1)}, frame2={len(decoded_bytes_2)}")
            return

        # Convert raw bytes to int16 PCM samples
        decoded_frame_1 = np.frombuffer(decoded_bytes_1, dtype=np.int16)
        decoded_frame_2 = np.frombuffer(decoded_bytes_2, dtype=np.int16)

        # Combine both frames and add to buffers
        all_samples = list(decoded_frame_1) + list(decoded_frame_2)
        self.analog_data.extend(all_samples)
        self.audio_buffer.extend(all_samples)

    def handle_notify(self, handle, data): 
        """
        Build BLE Packet for Audio Transmission
        =========================================
        Packet Format (sent over BLE to receiver):
          Byte 0-3:  seq_num       (uint32_t, little-endian) - Buffer sequence number
          Byte 4-5:  sample_count  (uint16_t, little-endian) - Number of samples in this packet
          Byte 6-7:  packet_index  (uint16_t, little-endian) - Index of packet within SAADC_BUFFER_SIZE buffer
          Byte 8+:   samples       (int16_t array, little-endian) - Audio sample data
        
        Example with 50 samples:
          packet[0..3]   = 0x2A000000  (seq_num = 42, meaning 42nd buffer)
          packet[4..5]   = 0x3200      (sample_count = 50)
          packet[6..7]   = 0x0500      (packet_index = 5, meaning 5th packet in this buffer)
          packet[8..9]   = 0xD5FF      (sample[0] = -43 as int16_le)
          packet[10..11] = 0xE2FF      (sample[1] = -30 as int16_le)
          ...
          packet[106..107] = sample[49]
          Total size: 8 + (50 * 2) = 108 bytes
        """ 
        # Validate minimum packet size
        if len(data) < PACKET_HEADER_SIZE:
            print(f"Packet too small: {len(data)} bytes (expected >= {PACKET_HEADER_SIZE})")
            return

        # Parse packet header
        seq_num = int.from_bytes(data[0:4], byteorder='little', signed=False)  # uint32
        sample_count = int.from_bytes(data[4:6], byteorder='little', signed=False)  # uint16
        packet_index = int.from_bytes(data[6:8], byteorder='little', signed=False)  # uint16

        # Validate packet size matches declared sample count
        expected_size = PACKET_HEADER_SIZE + sample_count * 2
        if len(data) != expected_size:
            print(f"Packet size mismatch: expected {expected_size}, got {len(data)}")
            return

        # Extract audio samples (int16 array, little-endian, signed)
        samples = []
        for i in range(sample_count):
            offset = PACKET_HEADER_SIZE + i * 2
            sample = int.from_bytes(data[offset:offset+2], byteorder='little', signed=True)
            samples.append(sample)

        # ========================================================================
        # Gap Filling Logic - Handle Missing Buffers and Packets
        # ========================================================================
        # Two levels of packet loss detection:
        # 1. Buffer-level: Missing seq_num (entire SAADC buffers dropped)
        # 2. Packet-level: Missing packet_index within a seq_num (individual BLE packets dropped)

        packets_per_buffer = SAADC_BUFFER_SIZE // SAMPLES_PER_PACKET  # 60 packets per buffer

        if len(self.packet_metadata) > 0:
            last_seq, last_sample_count, last_packet_index = self.packet_metadata[-1]

            # Case 1: Same buffer (seq_num unchanged) - check for packet_index gaps
            if seq_num == last_seq:
                # Fill missing packet_index within this buffer
                for missing_idx in range(last_packet_index + 1, packet_index):
                    self.packet_metadata.append((seq_num, SAMPLES_PER_PACKET, missing_idx))
                    self.analog_data.extend([None] * SAMPLES_PER_PACKET)
                    print(f"Gap filled: seq={seq_num}, packet_index={missing_idx} (missing packet within buffer)")

            # Case 2: New buffer (seq_num jumped) - fill missing buffers and packets
            elif seq_num > last_seq:
                # Step 2a: Fill remaining packets in the previous buffer (if incomplete)
                for missing_idx in range(last_packet_index + 1, packets_per_buffer):
                    self.packet_metadata.append((last_seq, SAMPLES_PER_PACKET, missing_idx))
                    self.analog_data.extend([None] * SAMPLES_PER_PACKET)
                    print(f"Gap filled: seq={last_seq}, packet_index={missing_idx} (completing previous buffer)")

                # Step 2b: Fill completely missing buffers (seq_num gaps)
                for missing_seq in range(last_seq + 1, seq_num):
                    for missing_idx in range(packets_per_buffer):
                        self.packet_metadata.append((missing_seq, SAMPLES_PER_PACKET, missing_idx))
                        self.analog_data.extend([None] * SAMPLES_PER_PACKET)
                    print(f"Gap filled: seq={missing_seq} (entire buffer missing, {packets_per_buffer} packets)")

                # Step 2c: Fill missing packets at start of current buffer
                for missing_idx in range(0, packet_index):
                    self.packet_metadata.append((seq_num, SAMPLES_PER_PACKET, missing_idx))
                    self.analog_data.extend([None] * SAMPLES_PER_PACKET)
                    print(f"Gap filled: seq={seq_num}, packet_index={missing_idx} (missing packet at buffer start)")

            # Case 3: seq_num went backwards (shouldn't happen, but handle it)
            else:
                print(f"WARNING: seq_num went backwards! last={last_seq}, current={seq_num}")

        # Store current packet metadata
        self.packet_metadata.append((seq_num, sample_count, packet_index))

        # Add samples to data buffers
        if sample_count > 0:
            self.analog_data.extend(samples)
            self.audio_buffer.extend(samples)
        else:
            # Packet arrived but empty - fill with None
            self.analog_data.extend([None] * sample_count)

        # ========================================================================
        # METRICS DISABLED - TODO: Update for new packet format
        # ========================================================================
        # The old metrics code below is designed for single-sample packets.
        # With the new multi-sample format, these calculations are incorrect.
        # Uncomment and fix after testing basic audio playback works.
        # ========================================================================

        # now = time.time()
        # dt = now - self.last_time
        # if dt >= 1.0:
        #     byte_count = len(self.sequence_numbers) * 5  # OLD: 1 byte seq + 4 byte voltage
        #     bitrate = byte_count * 8 / dt
        #     self.bitrate_values.append(bitrate)
        #
        #     if self.last_seq is not None:
        #         expected = (self.last_seq + len(self.sequence_numbers)) % 256  # OLD: 8-bit seq
        #         received = self.sequence_numbers[-1]
        #         missing = (expected - received) % 256
        #         loss = 100.0 * missing / (missing + len(self.sequence_numbers)) if (missing + len(self.sequence_numbers)) else 0
        #     else:
        #         loss = 0.0
        #
        #     self.packet_loss_values.append(loss)
        #
        #     # "Normalized SNR" proxy (your original)
        #     normalized_snr = 10 * np.log10(2 ** (bitrate / 10000)) if bitrate > 0 else 0
        #     self.snr_values.append(normalized_snr)
        #
        #     self.last_time = now
        #     self.last_seq = self.sequence_numbers[-1]
        #     self.sequence_numbers.clear()

        # Playback every ~100 samples
        # if len(self.audio_buffer) >= 100:
        #     audio_np = np.array(self.audio_buffer, dtype=np.float32)
        #     # normalize to -1..1 for playback
        #     denom = np.max(np.abs(audio_np - np.mean(audio_np))) or 1.0
        #     audio_np = (audio_np - np.mean(audio_np)) / denom
        #     sd.play(audio_np, samplerate=AUDIO_FS, blocking=False)
        #     self.audio_buffer = []

    def _write_wav_int16(self, samples_int16, fs, wav_path):
        """Write PCM16 WAV without extra dependencies."""
        with wave.open(wav_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(fs)
            wf.writeframes(samples_int16.tobytes())

    def _maybe_write_mp3(self, wav_path, mp3_path):
        """Convert WAV->MP3 if pydub is available and encoder is present."""
        if not _PydubAvailable:
            return False
        try:
            aud = AudioSegment.from_wav(wav_path)
            aud.export(mp3_path, format="mp3", bitrate="192k")
            return True
        except Exception as e:
            print("MP3 export skipped:", e)
            return False

    def save_recording_lc3codec(self):
        """Save LC3-decoded audio recording"""
        start_sample = self.record_start_sample_idx
        end_sample = len(self.analog_data)

        if end_sample <= start_sample:
            print("Nothing to save.")
            return

        # Extract recorded segment
        recorded_samples = self.analog_data[start_sample:end_sample]
        num_samples = len(recorded_samples)

        print(f"Saving {num_samples} samples ({num_samples/PCM_SAMPLE_RATE:.2f} seconds @ {PCM_SAMPLE_RATE}Hz)...")

        base = "recording_lc3"

        # 1) Save CSV with simple index
        csv_name = f"{base}.csv"
        with open(csv_name, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["sample_index", "sample"])
            for i, sample in enumerate(recorded_samples):
                writer.writerow([i, sample])
        print(f"Saved: {csv_name} ({num_samples} samples)")


    def save_recording(self):
        # Determine sample range to save (only the current recording window)
        start_sample: int = self.record_start_sample_idx
        end_sample: int = len(self.analog_data)

        if end_sample <= start_sample:
            print("Nothing to save.")
            return

        # Build CSV data with seq_num, packet_index, num_samples, timestep, sample
        csv_rows: List[Tuple[int, int, int, int, Optional[int]]] = []

        # Track cumulative timestep
        timestep: int = 0

        for seq_num, sample_count, packet_index in self.packet_metadata:
            # For each sample in this packet
            for i in range(sample_count):
                current_timestep: int = timestep + i

                # Only include samples within the recording window
                if start_sample <= current_timestep < end_sample:
                    sample_value: Optional[int] = self.analog_data[current_timestep]
                    csv_rows.append((seq_num, packet_index, sample_count, current_timestep, sample_value))

            # Update timestep for next packet
            timestep += sample_count

        # File base name
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        # base = f"recording_{now}"
        base = f"recording"

        # 1) Save CSV with new format
        csv_name = f"{base}.csv"
        with open(csv_name, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["seq_num", "packet_index", "num_samples", "timestep", "sample"])
            for row in csv_rows:
                writer.writerow(row)
        print(f"Saved: {csv_name} ({len(csv_rows)} samples)")

        # 2) Save WAV (PCM16) @ 16 kHz
        # Convert mV series into audio-ish int16:
        #   center, then scale to 90% of full scale to avoid clipping

        # centered = seg_mv - np.mean(seg_mv)
        # peak = np.max(np.abs(centered)) or 1.0
        # norm_float = 0.9 * centered / peak
        # pcm16 = np.clip((norm_float * 32767.0), -32768, 32767).astype(np.int16)
        #
        # wav_name = f"{base}.wav"
        # self._write_wav_int16(pcm16, AUDIO_FS, wav_name)
        # print(f"Saved: {wav_name}")

        # 3) Optional MP3

        # mp3_name = f"{base}.mp3"
        # did_mp3 = self._maybe_write_mp3(wav_name, mp3_name)
        # if did_mp3:
        #     print(f"Saved: {mp3_name}")
        # else:
        #     print("MP3 not saved (pydub/encoder not available).")

        # Reset start index for next session

app = QApplication(sys.argv)
loop = QEventLoop(app)
asyncio.set_event_loop(loop)

window = RAISEApp()
window.show()

with loop:
    loop.run_forever()

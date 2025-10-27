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

# Optional MP3 support
try:
    from pydub import AudioSegment
    _PydubAvailable = True
except Exception:
    _PydubAvailable = False

DEVICE_NAME = "RAISE_mic"
CTRL_UUID = "12345678-1234-5678-1234-56789abcdef1"
DATA_UUID = "12345678-1234-5678-1234-56789abcdef2"

AUDIO_FS = 16000 # Sample rate: 8 kHz (matches nRF54L15 configuration)

class RAISEApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RAISE Microphone Testbench")
        self.resize(1000, 800)
        self.client = None
        self.device = None
        self.connected = False
        self.recording = False
        self.streaming = False

        # Data stores
        self.analog_data: List[Optional[int]] = []       # mV integers (all-time), None for dropped packets
        self.audio_buffer: List[int] = []      # chunk buffer for playback
        self.packet_metadata: List[Tuple[int, int]] = []   # List of (seq_num, sample_count) tuples for each packet

        # Metrics
        self.bitrate_values = []
        self.packet_loss_values = []
        self.snr_values = []
        self.last_seq = None
        self.last_time = time.time()

        # Recording segment tracking
        self.record_start_sample_idx: int = 0  # sample index in analog_data where recording began

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
        self.record_btn.clicked.connect(self.toggle_recording)
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
            await self.client.start_notify(DATA_UUID, self.handle_notify)
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
            sample_idx: int = sum(sample_count for seq_num, sample_count in self.packet_metadata)
            self.record_start_sample_idx = sample_idx
            self.record_btn.setText("Stop Recording")
            print(f"Recording: START at sample index {sample_idx}")
        else:
            self.record_btn.setText("Start Recording")
            print("Recording: STOP")
            self.save_recording()

    def update_plots(self):
        if self.analog_data:
            self.graph_lines["voltage"].setData(self.analog_data[-100:])
            self.labels["voltage"].setText(f"Voltage: {self.analog_data[-1]} mV")

        if self.bitrate_values:
            self.graph_lines["bitrate"].setData(self.bitrate_values[-100:])
            self.labels["bitrate"].setText(f"Bitrate: {self.bitrate_values[-1]:.1f} bps")

        if self.snr_values:
            self.graph_lines["snr"].setData(self.snr_values[-100:])
            self.labels["snr"].setText(f"Normalized SNR: {self.snr_values[-1]:.2f} dB")

        if self.packet_loss_values:
            self.graph_lines["loss"].setData(self.packet_loss_values[-100:])
            self.labels["loss"].setText(f"Packet Loss: {self.packet_loss_values[-1]:.1f}%")

    def handle_notify(self, handle, data):
        """
        Parse BLE notification packets from nRF54L15

        Packet Format (from nRF54L15 C code):
        =====================================
        Byte 0-3:  seq_num       (uint32_t, little-endian) - Packet sequence number
        Byte 4-5:  sample_count  (uint16_t, little-endian) - Number of samples in packet
        Byte 6+:   samples       (int16_t array, little-endian) - Audio sample data

        Example packet with 50 samples (106 bytes total):
            data[0:4]   = b'\x2A\x00\x00\x00'  -> seq_num = 42
            data[4:6]   = b'\x32\x00'          -> sample_count = 50
            data[6:8]   = b'\xD5\xFF'          -> samples[0] = -43 (int16)
            data[8:10]  = b'\xE2\xFF'          -> samples[1] = -30 (int16)
            ...
            data[104:106] = samples[49]

        Typical packet: 6 bytes header + 50 samples * 2 bytes = 106 bytes
        Sample rate: 8000 Hz
        Buffer size: 2000 samples per buffer (sent as 40 packets)
        """
        # Validate minimum packet size
        if len(data) < 6:
            print("Packet too small:", len(data), "bytes (expected >= 6)")
            return

        # Parse packet header
        seq_num = int.from_bytes(data[0:4], byteorder='little', signed=False)  # uint32
        sample_count = int.from_bytes(data[4:6], byteorder='little', signed=False)  # uint16

        # Validate packet size matches declared sample count
        expected_size = 6 + sample_count * 2
        if len(data) != expected_size:
            print(f"Packet size mismatch: expected {expected_size}, got {len(data)}")
            return

        # Extract audio samples (int16 array, little-endian, signed)
        samples = []
        for i in range(sample_count):
            offset = 6 + i * 2
            sample = int.from_bytes(data[offset:offset+2], byteorder='little', signed=True)
            samples.append(sample)

        # Handle missing packets by filling gaps with placeholder metadata and None samples
        if len(self.packet_metadata) > 0:
            last_seq: int = self.packet_metadata[-1][0]
            expected_seq: int = last_seq + 1

            # Fill gaps for missing packets (assume 50 samples each)
            while expected_seq < seq_num:
                self.packet_metadata.append((expected_seq, 50))
                # Fill analog_data with None values for missing samples
                self.analog_data.extend([None] * 50)
                expected_seq += 1

        # Store current packet metadata (even if sample_count is 0)
        self.packet_metadata.append((seq_num, sample_count))

        # Add samples to data buffers (only if we actually received samples)
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

    def save_recording(self):
        # Determine sample range to save (only the current recording window)
        start_sample: int = self.record_start_sample_idx
        end_sample: int = len(self.analog_data)

        if end_sample <= start_sample:
            print("Nothing to save.")
            return

        # Build CSV data with seq_num, num_samples, timestep, sample
        csv_rows: List[Tuple[int, int, int, Optional[int]]] = []

        # Track cumulative timestep
        timestep: int = 0

        for seq_num, sample_count in self.packet_metadata:
            # For each sample in this packet
            for i in range(sample_count):
                current_timestep: int = timestep + i

                # Only include samples within the recording window
                if start_sample <= current_timestep < end_sample:
                    sample_value: Optional[int] = self.analog_data[current_timestep]
                    csv_rows.append((seq_num, sample_count, current_timestep, sample_value))

            # Update timestep for next packet
            timestep += sample_count

        # File base name
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"recording_{now}"

        # 1) Save CSV with new format
        csv_name = f"{base}.csv"
        with open(csv_name, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["seq_num", "num_samples", "timestep", "sample"])
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

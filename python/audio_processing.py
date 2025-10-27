import csv
import numpy as np
import wave
import matplotlib.pyplot as plt
from typing import List, Tuple
import sys

# SAADC sampling rate from firmware (src/main.c:32)
# SAADC_SAMPLE_INTERVAL_US = 62.5 us = 16 kHz

SAADC_SAMPLE_RATE: int = 16000  # Hz


def analyze_csv(csv_path: str) -> Tuple[np.ndarray, int]:
    samples: List[int] = []
    none_count: int = 0

    print(f"Reading CSV: {csv_path}")
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sample_str = row['sample']

            # Handle None values (dropped packets) - treat as 0
            if sample_str and sample_str != 'None': # Check if "None" or empty
                samples.append(int(sample_str))
            else:
                samples.append(0)
                none_count += 1

    # Convert to numpy array for analysis
    samples_array = np.array(samples, dtype=np.float32)

    # Calculate statistics
    total_samples = len(samples)
    min_value = np.min(samples_array)
    max_value = np.max(samples_array)

    # Vrms = sqrt(mean(voltage^2))
    vrms = np.sqrt(np.mean(samples_array ** 2))

    # Print analysis
    print(f"\n=== CSV Analysis ===")
    print(f"Total samples: {total_samples}")
    print(f"None samples: {none_count} ({100.0 * none_count / total_samples:.2f}%)")
    print(f"Min value: {min_value:.0f}")
    print(f"Max value: {max_value:.0f}")
    print(f"Vrms: {vrms:.2f}")
    print(f"Duration: {total_samples / SAADC_SAMPLE_RATE:.2f} seconds")

    return samples_array, none_count


def create_audio(samples: np.ndarray, output_wav_path: str, sample_rate: int = SAADC_SAMPLE_RATE):
    print(f"\n=== Creating Audio ===")

    # Center the audio (remove DC offset)
    centered = samples - np.mean(samples)

    # Normalize to -0.9 to +0.9 range (90% of full scale to avoid clipping)
    peak = np.max(np.abs(centered))
    if peak > 0:
        normalized = 0.9 * centered / peak
    else:
        normalized = centered

    # Convert to 16-bit PCM
    pcm16 = np.clip(normalized * 32767.0, -32768, 32767).astype(np.int16)

    # Write WAV file
    with wave.open(output_wav_path, 'wb') as wf:
        wf.setnchannels(1)  # Mono
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())

    print(f"Saved: {output_wav_path}")
    print(f"Audio file: {len(samples) / sample_rate:.2f} seconds at {sample_rate} Hz")


def wav_to_csv(wav_path: str, output_csv_path: str) -> np.ndarray:
    print(f"\nReading WAV: {wav_path}")

    with wave.open(wav_path, 'rb') as wf:
        sample_rate = wf.getframerate()
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        n_frames = wf.getnframes()

        audio_data = wf.readframes(n_frames)

    # Convert bytes to numpy array (assuming 16-bit PCM)
    if sample_width == 2:
        samples = np.frombuffer(audio_data, dtype=np.int16)
    else:
        raise ValueError(f"Unsupported sample width: {sample_width}")

    # Handle stereo by taking only first channel
    if n_channels == 2:
        samples = samples[::2]

    # Calculate statistics
    min_value = np.min(samples)
    max_value = np.max(samples)
    vrms = np.sqrt(np.mean(samples.astype(np.float32) ** 2))

    print(f"Sample rate: {sample_rate} Hz")
    print(f"Total samples: {len(samples)}")
    print(f"Min value: {min_value}")
    print(f"Max value: {max_value}")
    print(f"Vrms: {vrms:.2f}")
    print(f"Duration: {len(samples) / sample_rate:.2f} seconds")

    # Write to CSV
    with open(output_csv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["timestep", "sample"])
        for i, sample in enumerate(samples):
            writer.writerow([i, int(sample)])

    print(f"Saved CSV: {output_csv_path}")

    return samples


def analyze_fft(csv_path: str, sample_rate: int = SAADC_SAMPLE_RATE):
    print(f"\n=== FFT Analysis: {csv_path} ===")

    samples: List[int] = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sample_str = row['sample']
            if sample_str and sample_str != 'None':
                samples.append(int(sample_str))
            else:
                samples.append(0)

    samples_array = np.array(samples, dtype=np.float32)

    # Compute FFT
    fft_result = np.fft.rfft(samples_array)
    fft_magnitude = np.abs(fft_result)
    fft_freq = np.fft.rfftfreq(len(samples_array), 1.0 / sample_rate)

    # Convert to dB
    fft_magnitude_db = 20 * np.log10(fft_magnitude + 1e-10)

    return fft_freq, fft_magnitude_db


def process_csv_to_wav():
    csv_file = "recording_20251026_200845.csv"
    output_file = "recording_20251026_200845_generated.wav"

    samples, none_count = analyze_csv(csv_file)
    create_audio(samples, output_file, sample_rate=SAADC_SAMPLE_RATE)


def process_wav_to_csv():
    wav_to_csv("hola_base.wav", "hola_base.csv")
    wav_to_csv("recording_20251026_200845_generated.wav", "recording_20251026_200845_generated.csv")

    # Read raw data
    samples1: List[int] = []
    samples2: List[int] = []

    with open("hola_base.csv", 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            samples1.append(int(row['sample']))

    with open("recording_20251026_200845_generated.csv", 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            samples2.append(int(row['sample']))

    samples1_array = np.array(samples1, dtype=np.float32)
    samples2_array = np.array(samples2, dtype=np.float32)

    # Compute FFT
    freq1, mag1 = analyze_fft("hola_base.csv", sample_rate=SAADC_SAMPLE_RATE)
    freq2, mag2 = analyze_fft("recording_20251026_200845_generated.csv", sample_rate=SAADC_SAMPLE_RATE)

    # Create time axis (in seconds)
    time1 = np.arange(len(samples1_array)) / SAADC_SAMPLE_RATE* 1.0
    time2 = np.arange(len(samples2_array)) / SAADC_SAMPLE_RATE* 1.0

    # Plot
    fig = plt.figure(figsize=(8, 6))

    # Raw data plots
    plt.subplot(3, 2, 1)
    plt.plot(time1, samples1_array)
    plt.title("Raw Data: hola_base.wav")
    plt.xlabel("Time (s)")
    plt.ylabel("Amplitude")
    plt.grid(True)

    plt.subplot(3, 2, 2)
    plt.plot(time2, samples2_array)
    plt.title("Raw Data: recording_20251026_200845_generated.wav")
    plt.xlabel("Time (s)")
    plt.ylabel("Amplitude")
    plt.grid(True)

    # FFT plots
    plt.subplot(3, 2, 3)
    plt.plot(freq1, mag1)
    plt.title("FFT: hola_base.wav")
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Magnitude (dB)")
    plt.grid(True)
    plt.xlim(0, 8000)

    plt.subplot(3, 2, 4)
    plt.plot(freq2, mag2)
    plt.title("FFT: recording_20251026_200845_generated.wav")
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Magnitude (dB)")
    plt.grid(True)
    plt.xlim(0, 8000)

    # Spectrogram plots
    plt.subplot(3, 2, 5)
    plt.specgram(samples1_array, Fs=SAADC_SAMPLE_RATE, NFFT=1024, noverlap=512, cmap='viridis')
    plt.title("Spectrogram: hola_base.wav")
    plt.xlabel("Time (s)")
    plt.ylabel("Frequency (Hz)")
    plt.ylim(0, 8000)
    plt.colorbar(label='Intensity (dB)')

    plt.subplot(3, 2, 6)
    plt.specgram(samples2_array, Fs=SAADC_SAMPLE_RATE, NFFT=1024, noverlap=512, cmap='viridis')
    plt.title("Spectrogram: recording_20251026_200845_generated.wav")
    plt.xlabel("Time (s)")
    plt.ylabel("Frequency (Hz)")
    plt.ylim(0, 8000)
    plt.colorbar(label='Intensity (dB)')

    plt.tight_layout()
    plt.savefig("fft_comparison.png", dpi=150)
    print("\nSaved FFT plot: fft_comparison.png")
    plt.show()


if __name__ == "__main__":

    if len(sys.argv) < 2:
        print("Usage: python audio_processing.py [all|2csv|2wav]")
        print("  all  - Execute all operations in order")
        print("  2csv - WAV to CSV conversion and FFT analysis")
        print("  2wav - CSV to WAV conversion and analysis")
        sys.exit(1)

    mode = sys.argv[1].lower()

    if mode == "all":
        print("=== Running All Operations ===\n")
        process_csv_to_wav()
        process_wav_to_csv()
    elif mode == "2csv":
        print("=== WAV to CSV + FFT ===\n")
        process_wav_to_csv()
    elif mode == "2wav":
        print("=== CSV to WAV ===\n")
        process_csv_to_wav()
    else:
        print(f"Unknown mode: {mode}")
        print("Valid modes: all, 2csv, 2wav")
        sys.exit(1)

# Audio Streaming Debugging Checklist

## Current Status
- ✅ BLE packet transmission working
- ✅ Python receiving packets correctly
- ✅ Data parsing successful
- ❌ Audio playback causes crashes
- ❌ Recorded audio is just noise

---

## TODO List

### 1. Check Proper Microphone Input
- [ ] Verify microphone physical connection to pin P11.1 (AIN4)
- [ ] Check ADC gain setting (currently 0.25x - try 1x gain)
- [ ] Verify signal levels in serial output (currently very small: -76 to -28)
- [ ] Test ADC with known signal (GND, VDD, or function generator)

### 2. Check Proper Reconstruction of Audio Based on Raw Data from ADC
- [ ] Verify DC bias removal in Python
- [ ] Fix audio scaling/normalization
- [ ] Confirm sample rate matches (8 kHz both sides)
- [ ] Check data type conversions (int16 → float32)

### 3. Fix Data Buffering Issues
- [ ] Increase buffer size before playback (try 400 samples instead of 100)
- [ ] Handle concurrent playback issues
- [ ] Add error handling for audio playback
- [ ] Test audio device configuration

### 4. Signal Quality Verification
- [ ] Record without playback to isolate issues
- [ ] Print sample statistics for debugging
- [ ] Check for missing/corrupted packets
- [ ] Analyze recorded WAV in external tool (Audacity)

### 5. Test with Sine Wave Generation
- [ ] Generate test tone on nRF54L15 (bypass microphone)
- [ ] Verify BLE transmission with known signal
- [ ] Confirm Python playback works with clean signal

---

## Additional Suggestions

### Hardware
- Use oscilloscope to verify microphone signal
- Test microphone separately (connect to PC)
- Check for electrical noise/interference

### Software
- Add high-pass filter for DC removal
- Implement AGC (Automatic Gain Control)
- Export raw ADC values to CSV for analysis

### Key Insight
**Current ADC range is only ~1% of full scale!**
- Expected: -2048 to +2047 (12-bit)
- Actual: -76 to -28
- **Action**: Increase gain or check microphone connection

---

## Priority Order
1. Increase ADC gain (src/main.c line 209)
2. Verify microphone hardware
3. Test with sine wave
4. Fix Python audio conversion
5. Re-enable metrics

**Last Updated**: 2025-10-24

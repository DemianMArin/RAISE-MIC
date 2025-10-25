# Audio Streaming Debugging Checklist

## Current Status
- ✅ BLE packet transmission working
- ✅ Python receiving packets correctly
- ✅ Data parsing successful
- ❌ Audio playback causes crashes
- ❌ Recorded audio is just noise

---

## TODO List
- [ ] Check ADC gain setting (currently 0.25x - try 1x gain)
- [ ] Verify signal levels in serial output (currently very small: -76 to -28)
- [ ] Verify DC bias removal in Python
- [ ] Fix audio scaling/normalization
- [ ] Check data type conversions (int16 → float32)
- [ ] Increase buffer size before playback (try 400 samples instead of 100)
- [ ] Add error handling for audio playback
- [ ] Record without playback to isolate issues
- [ ] Verify BLE transmission with known signal

---


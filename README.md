# RAISE MIC

Sending audio with microphone from nrf54l15-dk and custom board to computer via bluetooth. Goal is to measure noise reduction.

## Initialize submodules
Initialize the submodules to get liblc3 library dependency for python. This is not required for the implementation of lc3 codec in the nrf54l15.
```
git submodule init
git submodule update

or

git clone --recurse-submodules <repository_url>
```


## Build Configuration

<p align="center">
  <img src="Img/build_config.png" width="400">
</p>

- .overlay is to make sure the advanced configurations of the saadc are enabled in custom boards (check saadc advanced config tutorial).

## Current results

<p align="center">
  <img src="Img/fft_comparison.png" width="600">
</p>

## Input and output bytes lc3 codec calculation

Decoded output
```
Frame size window: 10,000 us
Sample rate: 48,0000
Bit depth: 16

[frame_size/(1/sample_rate)] * (bit_depth/8)

10,000 us / (1/48,000) =  480 samples
480 * 2 bytes (int16_t) = 960 bytes

[10,000 us / (1/48,000)] * (16/8) = 960 bytes
```

Encoded input
```
Bitrate: 96000 bps
Frame size window: 10,000 us

(bitrate*frame_size)/8 = bytes

(96000*0.01) = 960 bits
960/8 = 120 bytes

(96000*0.01)/8 = 120 bytes
```

## Liblc3 for python
To obtain the decode and encode functionality of liblc3 a fork of zephyrproject-rtos was done (https://github.com/zephyrproject-rtos/liblc3/tree/main). Which itself is a fork of google's liblc3. The decision to fork the zephyr version is based on using the same code used 
to create the pre-compiled version of the liblc3 for the cortex-m33 (used in nrf54l15 and nrf5430).

This was only tested with conda environments. Follow the `README.md` in `python/` for setting liblc3 in conda environment.



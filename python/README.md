# Install liblc3

If installing in Mac/Linus comment the following lines in `deps/liblc3/pyproject.toml`
```
[tool.meson-python]
allow-windows-internal-shared-libs = true
```

To install python wrapper
```
cd deps/liblc3/
pip install .
```

After installing `requirements.txt` (check next section). Make sure the test comes out with no errors.
```
cd deps/liblc3/python/tests
pytest basic_test.py
```

# How to use
```
# to install requirements (we recommend using Conda)
python -r requirements.txt 

# GUI to connect to nrf54l15 and visualize data
# This app allows to record audio for post-processing
python struc_recording.py 

# Post-processing of audio 
python audio_processing.py all
```


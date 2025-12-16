import sounddevice as sd

print("Listing Audio Devices:")
try:
    devices = sd.query_devices()
    for idx, dev in enumerate(devices):
        if dev['max_input_channels'] > 0:
            print(f"Index {idx}: {dev['name']}")
            print(f"  Max Input Channels: {dev['max_input_channels']}")
            print(f"  Default Sample Rate: {dev['default_samplerate']}")
            print("-" * 30)
except Exception as e:
    print(f"Error querying devices: {e}")

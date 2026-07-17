import pyaudio
p = pyaudio.PyAudio()
print("Default input device:", p.get_default_input_device_info()['name'])
for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    if info['maxInputChannels'] > 0:
        print(f"Input Device {i}: {info['name']}")
p.terminate()

# assets/

- `fillers/tier1/`, `fillers/tier2/` — generated `.wav` filler clips, produced by
  `python -m pipeline.generate_fillers --voice <path-to-voice.onnx>` (see the root
  README). Empty until you run that script; the app runs fine without them, but
  delayed responses will go silent instead of playing a filler until they exist.
- Piper voice model files (e.g. `en_US-lessac-medium.onnx` + its `.json` config)
  should also be placed somewhere under this folder (e.g. `assets/voices/`) so
  packaged builds can locate them via Electron's `extraResources`.

# Voice Assistant — Offline Real-Time Voice Assistant

A fully offline, audio-in / audio-out conversational assistant with an Electron
desktop interface. Full design rationale, component-by-component reasoning, and
the acceptance criteria this build targets live in [`implementation.md`](implementation.md) —
this README is just the "how do I run it" quick start.

## What's in this repo

```
xibotix/
├── application/          # Electron desktop app (main process + UI)
│   ├── main.js            # spawns/supervises the Python backend, owns the WebSocket client
│   ├── preload.js          # context-isolated bridge exposed to the renderer
│   ├── index.html          # UI shell (status ring, transcript, controls)
│   ├── app.js              # renderer logic — pure UI, no Node/Electron access
│   ├── styles.css          # dark theme (palette below)
│   └── package.json        # electron / electron-builder config
├── pipeline/              # Python backend — the Pipecat pipeline host
│   ├── run_pipeline.py     # entry point: builds and runs the full VAD→Turn→STT→LLM→TTS pipeline
│   ├── fallback_processor.py  # filler/fallback FrameProcessor (Section 7)
│   ├── ws_bridge.py        # local WebSocket control/status server (Section 8.5)
│   ├── health_check.py     # startup checks: Ollama reachable, audio devices, filler assets
│   ├── generate_fillers.py # one-time script to pre-synthesize filler .wav clips
│   ├── config.py           # single source of truth for every tunable parameter (Section 11)
│   ├── Modelfile            # custom Ollama model definition (Section 4.4)
│   └── requirements.txt
├── assets/
│   └── fillers/tier1/, tier2/   # generated filler .wav clips land here (empty until generated)
└── implementation.md       # full design document (architecture, rationale, testing plan, risks)
```

## Theme

The UI uses a fixed, low-saturation dark palette — no neon accents, one single
accent color used sparingly for the active/primary state:

| Color | Hex | Role |
|---|---|---|
| Graphite | `#363636` | panels / surfaces |
| Jet Black | `#242F40` | app background |
| Golden Bronze | `#CCA43B` | the one accent — active state, primary action |
| Alabaster Grey | `#E5E5E5` | secondary text |
| White | `#FFFFFF` | primary text |

Defined as CSS variables at the top of `application/styles.css` — change the
five values there to re-theme the whole app.

## Prerequisites (target hardware assumptions from implementation.md Section 3)

- Python 3.10+ and Node.js 18+
- [Ollama](https://ollama.com) installed and running
- A Piper voice model (`.onnx` + `.json`), e.g. `en_US-lessac-medium`
- A working microphone and speaker
- GPU is only used by the LLM stage (8GB VRAM target); everything else (VAD,
  turn detection, ASR, TTS) runs on CPU by design — see Section 3 for why.

## Setup

**1. Python backend**

```bash
cd xibotix
python -m venv .venv
source .venv/bin/activate        # or .venv\Scripts\activate on Windows
pip install -r pipeline/requirements.txt
```

Note: `pipeline/requirements.txt` installs `pipecat-ai` with the `local`,
`whisper`, `piper`, and `local-smart-turn` extras, which is what actually
pulls in `pyaudio` (local mic/speaker I/O), `faster-whisper`, `piper-tts`, and
Smart Turn's `torch`/`torchaudio`/`transformers` — plain `pip install
pipecat-ai` on its own won't include any of these. On macOS you need
`brew install portaudio` before this install (pyaudio compiles against it);
on Linux, `apt install portaudio19-dev` (or your distro's equivalent) first.
Windows installs a prebuilt pyaudio wheel, so no extra step is needed there.
The `local-smart-turn` extra is a heavy install (expect several hundred MB
for torch) — that's expected, not a sign something went wrong.

**2. Ollama model**

```bash
ollama pull llama3.2:3b
ollama create voice-assistant -f ./pipeline/Modelfile
```

**3. Piper voice + filler clips**

`PiperTTSService` loads the voice model in-process and will auto-download it
into `assets/voices/` the first time it runs if it's not already there
(matching `pipeline.config.TTSConfig.download_dir`, default
`./assets/voices`). You can also pre-place a voice manually — download a
Piper voice model (e.g. `en_US-lessac-medium.onnx` + its `.json` config) into
`assets/voices/` yourself if you'd rather not rely on the auto-download at
first run.

Either way, pre-generate the fallback filler clips (the short "let me think
about that" phrases played if the LLM is slow to respond — see
implementation.md Section 7) using the same voice model:

```bash
python -m pipeline.generate_fillers --voice ./assets/voices/en_US-lessac-medium.onnx
```

**4. Electron app**

```bash
cd application
npm install
```

## Running

From the project root, with the Python venv activated so `python` resolves to
it (or set `VA_PYTHON_BIN` to an absolute interpreter path):

```bash
cd application
npm start
```

This launches the Electron window, which in turn spawns the Python backend
(`python -m pipeline.run_pipeline`) and connects to it over a local WebSocket
on port 8765. Raw audio never crosses that socket — only transcript text and
pipeline state (`listening` / `thinking` / `speaking` / `interrupted`); see
implementation.md Section 8.1 for why that split exists.

You can also run the backend headlessly (no Electron), which is the
recommended first step per Section 14's build order:

```bash
python -m pipeline.run_pipeline
```

Or just run the startup health checks without starting the pipeline:

```bash
python -m pipeline.run_pipeline --check
```

## Configuration

Every tunable in `pipeline/config.py` can be overridden with an environment
variable without touching code — see the table in implementation.md Section 11
for the full list (VAD thresholds, STT model size, LLM model/temperature, TTS
voice, fallback timeouts, bridge port). For example, to try the larger
Whisper model:

```bash
VA_STT_MODEL=medium.en python -m pipeline.run_pipeline
```

## Packaging

```bash
cd application
npm run dist
```

This uses `electron-builder` (config already in `application/package.json`)
and bundles `../assets` as an extra resource. Note the caveat in
implementation.md Section 8.7: for real distribution beyond a dev machine, the
Python backend should be frozen into a standalone executable (e.g. via
PyInstaller) so end users don't need a Python environment set up — that step
isn't done here since it's a packaging/build-pipeline concern separate from
the application code itself.

## Known gaps / where to look next

This is a structurally complete reference implementation of every component in implementation.md. The codebase is now fully compatible with pipecat-ai 1.5.0, utilizing the updated LLMAssistantAggregator and VADProcessor patterns.

See implementation.md Sections 13–15 for the full testing plan and known
risks/mitigations (Smart Turn misjudgment, VRAM budget, CPU contention,
Ollama crashes, etc.).

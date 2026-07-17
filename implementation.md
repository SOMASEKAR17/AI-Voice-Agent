# Implementation Plan — Offline Real-Time Voice Assistant

## Table of contents

1. Objective and acceptance criteria
2. System architecture overview
3. Target hardware and environment
4. Component deep dive
   4.1 Voice Activity Detection (Silero VAD)
   4.2 Turn detection (Smart Turn)
   4.3 Speech-to-text (faster-whisper)
   4.4 Language model (Ollama)
   4.5 Text-to-speech (Piper)
5. Orchestration layer: Pipecat pipeline assembly
6. Interruption / barge-in handling
7. Fallback and filler conversation flow
8. Electron application
9. Project folder structure
10. Dependencies
11. Configuration reference
12. Latency budget and benchmarking methodology
13. Testing plan
14. Build and rollout order
15. Known risks and mitigations
16. Future extensions

---

## 1. Objective and acceptance criteria

Build a real-time, fully offline, audio-in / audio-out conversational assistant with an Electron desktop interface.

Acceptance criteria:

- The system accepts continuous microphone input and does not require push-to-talk.
- The system responds with synthesized voice, not text-only output.
- End-to-end latency (from the user finishing a turn to the first audible syllable of the response) is under 2 seconds under normal conditions on the target hardware.
- If the primary response path is delayed beyond a defined threshold, the system plays a natural filler utterance rather than going silent or showing a generic error.
- The user can interrupt the assistant mid-response (barge-in) and have the assistant stop speaking and address the new input, without requiring the assistant to finish its current sentence first.
- The entire pipeline runs without an internet connection. No stage depends on a cloud API in the default configuration.
- The interface is an Electron desktop application.

Non-goals for this phase (see Section 16 for how these might be added later): multi-user support, wake-word activation, persistent long-term memory across sessions, tool/function calling, telephony integration.

---

## 2. System architecture overview

The system is a streaming cascaded pipeline — not a monolithic speech-to-speech model, and not a naive record-then-process-then-play pipeline. Every stage streams its output to the next stage incrementally, so stages overlap in time rather than running strictly one after another.

```
                    ┌───────────────────────────────────────────────────────┐
                    │                     Electron app                      │
                    │  (renderer: transcript / status UI, control buttons)  │
                    └───────────────────────┬───────────────────────────────┘
                                            │ local WebSocket (control + text only)
                                            │
                    ┌───────────────────────▼───────────────────────────────┐
                    │                Python backend process                 │
                    │                 (Pipecat pipeline host)               │
                    │                                                       │
  Mic ──────────▶  │  Silero VAD ─▶ Smart Turn ─▶ faster-whisper (stream)   │
                    │                                    │                   │
                    │                                    ▼                   │
  Speaker ◀──────  │  Piper TTS ◀── Sentence queue ◀── Ollama (token stream)│
                    │       ▲                                                │
                    │       └── Fallback / filler processor (custom)         │
                    └───────────────────────────────────────────────────────┘
```

Design principle: audio I/O and the latency-critical loop live entirely inside the Python process. Electron never touches raw audio frames; it only receives text/state updates and sends control commands (start, stop, mute, restart).

---

## 3. Target hardware and environment

- GPU: NVIDIA RTX 4060, 8GB VRAM
- CPU: AMD Ryzen 7000 series
- RAM: 24GB
- OS: cross-platform target via Electron, primary development/testing assumed on the machine described above
- LLM runtime: Ollama (already installed)

Constraint that drives every downstream decision: 8GB of VRAM is the one scarce resource in this system. RAM (24GB) and CPU throughput (Ryzen 7000) are comfortable. Therefore:

- The LLM is the only stage that uses the GPU.
- VAD, turn detection, ASR, and TTS all run on CPU.
- Model size selection for the LLM is bounded by what fits in 8GB with room for context and KV cache, not by what would be ideal in the abstract.

---

## 4. Component deep dive

### 4.1 Voice Activity Detection — Silero VAD

Purpose: cheap, continuous, low-latency detection of "is there speech in this audio frame right now." This is a raw signal, not a decision about turn-taking — that's a separate layer (4.2).

Why Silero over WebRTC VAD: Silero is a small neural VAD model that's meaningfully more accurate at distinguishing speech from background noise and non-speech sounds (breathing, keyboard clicks, room noise) than WebRTC's energy-based VAD, for a CPU cost that's still negligible relative to the other stages.

Key parameters to tune:

- `sample_rate`: 16000 Hz (matches Whisper's expected input, avoids a resampling step)
- `frame_duration_ms`: typically 30ms frames for the rolling window
- `speech_threshold`: probability threshold above which a frame is classified as speech (start conservative, e.g. 0.5, and raise if false triggers from background noise are observed)
- `min_speech_duration_ms`: minimum duration before a speech segment is considered real, to avoid triggering on transient sounds

Reference configuration (Pipecat's VAD analyzer wraps Silero directly — check the installed `pipecat-ai` version's `pipecat.audio.vad` module for the exact current parameter names, since these have shifted across releases):

```python
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

vad_analyzer = SileroVADAnalyzer(
    params=VADParams(
        confidence=0.6,
        start_secs=0.2,
        stop_secs=0.8,
        min_volume=0.6,
    )
)
```

`start_secs` / `stop_secs` here are conservative defaults meant to avoid the exact failure mode this project started from — the assistant reacting to a brief pause as if the user were finished. These are tightened further by the turn-detection layer below, not replaced by it.

### 4.2 Turn detection — Smart Turn

Purpose: decide when the user has actually finished their thought, as opposed to pausing mid-sentence. This is the direct fix for the "user pauses, LLM starts generating, user resumes talking" scenario that motivated re-examining the naive fixed-timeout approach.

Two stop strategies were considered:

- **Fixed speech-timeout**: wait for N milliseconds of silence after the last detected speech, then declare the turn over. Simple, but this is exactly the mechanism that causes false turn-ends on normal conversational pauses (thinking, breathing, "um").
- **Smart Turn model**: a small model that listens to the trailing audio and judges whether the utterance sounds semantically/prosodically complete (falling intonation, completed clause structure) rather than purely measuring silence duration. This is Pipecat's default stop strategy and is the one used here.

```python
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy

turn_stop_strategy = TurnAnalyzerUserTurnStopStrategy(
    turn_analyzer=LocalSmartTurnAnalyzerV3()
)
```

Fallback consideration: if Smart Turn's compute cost or accuracy proves unsuitable in practice, a secondary fixed-timeout strategy with a longer, more conservative window (e.g. 800ms–1.2s of silence, plus a minimum-word-count gate so single-word fragments don't trigger a turn end) is the documented fallback — worse false-positive rate, but simpler and cheaper.

### 4.3 Speech-to-text — faster-whisper

Purpose: convert the user's speech into text incrementally, so that by the time the turn-detection layer declares the turn over, transcription is already nearly complete rather than starting from a blank slate.

Model size decision:

| Model | Relative accuracy | Relative CPU cost | Recommended use |
|---|---|---|---|
| `tiny.en` | Lowest | Lowest | Not recommended — too many transcription errors for a general assistant |
| `base.en` | Low-moderate | Low | Viable if CPU headroom is tight |
| `small.en` | Moderate-good | Moderate | **Starting point** — good balance for the Ryzen 7000 CPU |
| `medium.en` | Good | Higher | **Upgrade path** — worth trying given 24GB RAM headroom, if `small.en` latency is comfortably under budget |
| `large-v3` | Best | Highest | Not recommended for this latency target on CPU |

Quantization: int8 quantization via `faster-whisper`'s CTranslate2 backend, which meaningfully reduces CPU inference time with a small, usually acceptable accuracy cost.

Alternatives considered and rejected as primary (kept as documented fallbacks):

- `whisper.cpp`: comparable performance, C++-native. Rejected as primary because `faster-whisper`'s Python interface integrates more directly with Pipecat's async pipeline without a subprocess/FFI boundary. Worth revisiting if CPU transcription speed becomes the bottleneck stage.
- `distil-whisper`: a distilled, faster variant of Whisper. Worth trying if `small.en`/`medium.en` prove too slow on the target CPU — noted as a fallback, not adopted by default since it wasn't necessary in initial estimates.
- Cloud ASR (Deepgram, AssemblyAI): rejected outright for the default configuration since it breaks the offline requirement. Documented as an online fallback path only (Section 15).

Streaming integration sketch:

```python
from pipecat.services.whisper.stt import WhisperSTTService, Model

stt = WhisperSTTService(
    model=Model.SMALL_EN,   # or Model.MEDIUM_EN
    device="cpu",
    compute_type="int8",
)
```

(Class and enum names should be checked against the currently installed `pipecat-ai` version — STT service integrations have been renamed/reorganized across releases.)

### 4.4 Language model — Ollama

Model selection:

| Model | Approx. quantized size | First-token latency | Reasoning quality | Recommended role |
|---|---|---|---|---|
| `llama3.2:3b` | ~2GB | Lowest | Good for straightforward conversation | **Primary / default** — start here |
| `qwen2.5:3b-instruct` | ~2GB | Lowest | Comparable to llama3.2:3b, worth A/B testing | Alternative primary |
| `llama3.1:8b-instruct-q4_K_M` | ~4.7GB | ~150–300ms slower than 3B | Noticeably better reasoning/instruction-following | **Quality upgrade path** |
| Anything larger (13B+) | 8GB+ | Too slow / doesn't fit comfortably | N/A | Rejected for this VRAM budget |

Setup:

```bash
ollama pull llama3.2:3b
ollama pull llama3.1:8b-instruct-q4_K_M
```

A custom Modelfile is recommended to fix the assistant's system prompt, response style (concise, conversational, avoids markdown/lists in spoken responses since this is a voice interface), and to cap `num_predict` at a sane length so the model doesn't ramble in a way that's awkward to listen to:

```
FROM llama3.2:3b

SYSTEM """
You are a voice assistant. Respond in short, natural, conversational sentences.
Do not use markdown, bullet points, or numbered lists — this response will be spoken aloud.
Keep responses concise unless the user asks for detail.
"""

PARAMETER num_predict 200
PARAMETER temperature 0.7
```

```bash
ollama create voice-assistant -f ./Modelfile
```

Pipecat integration:

```python
from pipecat.services.ollama.llm import OllamaLLMService

llm = OllamaLLMService(
    model="voice-assistant",
    base_url="http://localhost:11434/v1",
)
```

Streaming: Ollama's `/api/chat` (or OpenAI-compatible `/v1/chat/completions`) endpoint streams tokens; Pipecat's `OllamaLLMService` consumes this stream and emits text frames incrementally, which is what allows the sentence-boundary buffering in Section 5 to start TTS before generation finishes.

Rejected alternative: raw `llama.cpp` server. Ollama already wraps `llama.cpp`-based inference with simpler model management and a stable streaming API; there was no latency or capability advantage to bypassing it, only added setup complexity.

### 4.5 Text-to-speech — Piper

Purpose: convert the LLM's streamed text, chunked at sentence boundaries, into audio with minimal first-chunk latency.

Why Piper: CPU-only, very fast real-time factor (it synthesizes audio faster than it will be played back, by a wide margin, even on CPU), and its voice quality is good enough for a natural-sounding assistant without needing GPU resources that are reserved for the LLM.

Voice selection: Piper ships multiple English voices at different quality/speed tiers (e.g. `en_US-lessac-medium`, `en_US-amy-medium`). Start with a `medium` quality tier voice — `high` quality tiers cost more CPU for a marginal naturalness gain that isn't necessary at this stage.

```python
from pipecat.services.piper.tts import PiperTTSService

tts = PiperTTSService(
    voice_id="en_US-lessac-medium",
    base_url="http://localhost:5000",  # if running Piper's HTTP server
)
```

Alternatives considered and rejected as primary:

- **Coqui TTS**: more expressive, higher-quality voices possible, but meaningfully heavier compute cost for a gain that isn't necessary given the latency priority. Worth revisiting if voice naturalness becomes a real usability complaint after the system is working.
- **ElevenLabs / OpenAI TTS (cloud)**: rejected for the default configuration since they require internet, violating the offline-first requirement. Documented as an online fallback only (Section 15).
- **Bark**: capable of more expressive/emotive speech, but slower and less predictable in latency — not suited to a real-time constraint.

Sentence-boundary chunking: the orchestrator (Section 5) slices the LLM's token stream at `.`, `?`, `!` boundaries and feeds each slice to Piper as soon as it's available, so the assistant starts speaking sentence 1 while sentence 2 is still being generated.

---

## 5. Orchestration layer: Pipecat pipeline assembly

The pipeline requires specific ordering to satisfy Pipecat 1.5.0 mechanics (specifically around LLMAssistantAggregator which acts as a text sink). The correct ordering is:

1. **	ransport.input()**: Mic audio.
2. **mute_processor**: Drops audio frames if the user mutes via the UI.
3. **udio_meter**: Analyzes volume for the UI ring.
4. **ad**: VADProcessor handling Silero VAD (emits UserStartedSpeaking).
5. **stt**: Whisper STT.
6. **context_aggregator.user()**: Formats user text for the LLM.
7. **allback**: Injects filler audio if LLM takes too long.
8. **llm**: Ollama LLM (streams TextFrames).
9. **ui_pre_tts**: Emits text to the UI WebSocket.
10. **	ts**: Piper TTS (consumes TextFrames, emits AudioRawFrames).
11. **context_aggregator.assistant()**: MUST be placed after TTS in 1.5.0; it aggregates the TextFrames for the conversation history and drops them.
12. **ui_post_tts**: Broadcasts speaking state changes to the UI.
13. **	ransport.output()**: Plays the audio via WASAPI/Realtek at 48000Hz (Pipecat auto-resamples).


## 6. Interruption / barge-in handling

Mechanism (relying on Pipecat's built-in system rather than custom code):

1. Silero VAD continuously emits raw start/stop speech signals, including while the assistant is speaking — VAD is never turned off during playback.
2. If speech is detected during assistant playback, the turn-start strategy can be configured to trigger an interruption immediately, without waiting for Smart Turn's completion judgment (completion judgment matters for deciding when to *start* generating a response to a fresh utterance, not for deciding whether an interruption is happening).
3. Pipecat pushes a high-priority `InterruptionFrame` through the pipeline. System frames bypass the normal per-processor queue and are handled immediately.
4. In-flight `DataFrame`/`ControlFrame` content is cancelled: queued TTS audio is flushed (silence resumes within tens of milliseconds), and the in-progress Ollama generation task is cancelled so the GPU stops producing tokens for a response that's now obsolete.
5. Frames marked `UninterruptibleFrame` are preserved rather than dropped — relevant later if function/tool calls are added (Section 16), so an in-flight tool call isn't silently corrupted by an interruption.
6. The context aggregator retains the partial user transcript spoken before the interruption and appends the new speech once the (new) turn actually ends, producing one consolidated user message for the LLM rather than two disjointed ones.

Explicit configuration decision: the turn-*start* strategy (governing when an interruption fires) is intentionally more eager/sensitive than the turn-*stop* strategy (governing when a fresh utterance is considered complete). This asymmetry is deliberate — false interruptions (stopping the assistant briefly when the user didn't really mean to) are far less annoying than false turn-ends (the assistant barging in on the user's unfinished thought).

Testing note: interruption during an in-flight tool/function call (not used in the current scope, but relevant if Section 16's extensions are added) is a known trickier edge case across voice frameworks generally and should be explicitly tested once tool use is introduced, rather than assumed to work by default.

---

## 7. Fallback and filler conversation flow

This layer is not provided by Pipecat out of the box — it is a custom `FrameProcessor` sitting between the LLM and TTS stages.

Behavior:

1. **Pre-cached filler audio**, generated once at application startup (or ahead of time and bundled as an asset), covering a handful of natural, short phrases: "let me think about that for a second," "good question, one moment," "mm, let's see." These are pre-synthesized with Piper and saved as `.wav` files, so playing one is just a file read plus audio write — no TTS inference in the critical path.
2. **First-token timeout**: if Ollama hasn't produced its first token within `first_token_timeout_s` (default 0.5s) of the user's turn ending, play a randomly-selected cached filler clip immediately.
3. **Long-delay timeout**: if the delay continues past `long_delay_timeout_s` (default 2.5s), play a second-tier filler clip with different phrasing than the first (avoids the assistant sounding like it's stuck in a loop saying the same thing).
4. **Graceful degradation on outright failure**: if Ollama errors or times out completely (e.g. model crashed, out of memory), fall back to a smaller/faster local model for a "good enough" response, or — if even that fails — ask a natural clarifying question that keeps the conversation moving rather than surfacing an error message.
5. **Crossfade on real response arrival**: if a filler clip is still playing when the real response's first sentence becomes available, crossfade rather than hard-cutting, so the transition doesn't sound jarring.

Reference implementation sketch:

```python
import asyncio
import random
from pathlib import Path

from pipecat.processors.frame_processor import FrameProcessor
from pipecat.frames.frames import TextFrame, TTSSpeakFrame, LLMFullResponseStartFrame


class FallbackFillerProcessor(FrameProcessor):
    def __init__(self, first_token_timeout_s: float, long_delay_timeout_s: float, filler_dir: str):
        super().__init__()
        self.first_token_timeout_s = first_token_timeout_s
        self.long_delay_timeout_s = long_delay_timeout_s
        self.filler_dir = Path(filler_dir)
        self.tier1_fillers = list((self.filler_dir / "tier1").glob("*.wav"))
        self.tier2_fillers = list((self.filler_dir / "tier2").glob("*.wav"))
        self._watch_task: asyncio.Task | None = None
        self._first_token_received = asyncio.Event()

    async def process_frame(self, frame, direction):
        if isinstance(frame, LLMFullResponseStartFrame):
            # A new user turn is being responded to — start the delay watchdog.
            self._first_token_received.clear()
            self._watch_task = asyncio.create_task(self._watch_for_delay())

        if isinstance(frame, TextFrame):
            # First real token arrived — cancel the watchdog, no filler needed (or crossfade if one is playing).
            self._first_token_received.set()

        await self.push_frame(frame, direction)

    async def _watch_for_delay(self):
        try:
            await asyncio.wait_for(self._first_token_received.wait(), timeout=self.first_token_timeout_s)
            return  # LLM responded in time, no filler needed
        except asyncio.TimeoutError:
            filler = random.choice(self.tier1_fillers)
            await self.push_frame(TTSSpeakFrame(audio_path=str(filler)))

        try:
            await asyncio.wait_for(self._first_token_received.wait(), timeout=self.long_delay_timeout_s - self.first_token_timeout_s)
        except asyncio.TimeoutError:
            filler = random.choice(self.tier2_fillers)
            await self.push_frame(TTSSpeakFrame(audio_path=str(filler)))
```

This is illustrative pseudocode showing the intended control flow (watchdog timers racing against the LLM's first token, tiered filler escalation). The exact frame types available (`LLMFullResponseStartFrame`, a hypothetical audio-path variant of `TTSSpeakFrame`) should be checked against the installed Pipecat version and adjusted — some frames may need to be constructed differently (e.g. reading raw PCM and wrapping in an `AudioRawFrame` rather than passing a file path directly).

Filler generation script (run once, offline, ahead of time):

```python
from piper import PiperVoice

voice = PiperVoice.load("en_US-lessac-medium.onnx")

tier1_phrases = [
    "Let me think about that for a second.",
    "Good question, one moment.",
    "Mm, let's see.",
]
tier2_phrases = [
    "Still working through that, one moment.",
    "Almost there, thanks for waiting.",
]

for i, phrase in enumerate(tier1_phrases):
    voice.synthesize(phrase, output_path=f"./assets/fillers/tier1/filler_{i}.wav")

for i, phrase in enumerate(tier2_phrases):
    voice.synthesize(phrase, output_path=f"./assets/fillers/tier2/filler_{i}.wav")
```

Recommendation: record 4–6 phrases per tier so repeated fillers within a single session don't feel obviously scripted, and periodically rotate/expand the phrase set.

---

## 8. Electron application

### 8.1 Process architecture

- **Main process**: manages the application lifecycle, spawns and supervises the Python backend as a child process, owns the local WebSocket connection to it, and creates the renderer window.
- **Renderer process**: pure UI — transcript display, pipeline status indicator (listening / thinking / speaking / interrupted), mute/restart controls. Receives all its data over `ipcRenderer` from the main process, which itself relays messages from the Python backend's WebSocket.
- **Python backend**: a separate OS process running the Pipecat pipeline described in Sections 4–7, exposing a local WebSocket server for control and status messages only. Raw audio never crosses this boundary — the backend talks to the OS audio devices directly.

This split is deliberate: it keeps the latency-critical audio path entirely inside one process (Python) with direct, low-level access to audio devices, while Electron's job is limited to something it's well-suited for — a native desktop UI shell — without adding an extra transport hop for audio data that would only add latency for no benefit in a local, single-machine deployment.

### 8.2 Main process — spawning and supervising the Python backend

```javascript
// main.js
const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn } = require('child_process');
const WebSocket = require('ws');
const path = require('path');

let pythonProcess = null;
let backendSocket = null;
let mainWindow = null;

function startPythonBackend() {
  const scriptPath = path.join(__dirname, '..', 'backend', 'run_pipeline.py');
  pythonProcess = spawn('python', [scriptPath], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  pythonProcess.stdout.on('data', (data) => {
    console.log(`[backend stdout] ${data}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    console.error(`[backend stderr] ${data}`);
  });

  pythonProcess.on('exit', (code) => {
    console.error(`Python backend exited with code ${code}`);
    if (mainWindow) {
      mainWindow.webContents.send('backend-status', { status: 'crashed', code });
    }
    // Optional: auto-restart with backoff, capped at a few attempts.
  });

  // Give the backend a moment to open its WebSocket server before connecting.
  setTimeout(connectToBackend, 1500);
}

function connectToBackend() {
  backendSocket = new WebSocket('ws://localhost:8765');

  backendSocket.on('open', () => {
    if (mainWindow) mainWindow.webContents.send('backend-status', { status: 'connected' });
  });

  backendSocket.on('message', (raw) => {
    const message = JSON.parse(raw.toString());
    // message.type: 'transcript_partial' | 'transcript_final' | 'assistant_text' |
    //               'pipeline_state' | 'interruption' | 'error'
    if (mainWindow) mainWindow.webContents.send('backend-message', message);
  });

  backendSocket.on('close', () => {
    if (mainWindow) mainWindow.webContents.send('backend-status', { status: 'disconnected' });
  });

  backendSocket.on('error', (err) => {
    console.error('Backend socket error:', err);
  });
}

function sendControlMessage(message) {
  if (backendSocket && backendSocket.readyState === WebSocket.OPEN) {
    backendSocket.send(JSON.stringify(message));
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 480,
    height: 720,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
}

app.whenReady().then(() => {
  createWindow();
  startPythonBackend();
});

app.on('window-all-closed', () => {
  if (pythonProcess) pythonProcess.kill();
  if (process.platform !== 'darwin') app.quit();
});

ipcMain.on('control-message', (_event, message) => {
  sendControlMessage(message);
});
```

### 8.3 Preload script — safe bridge between renderer and main

```javascript
// preload.js
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('assistantBridge', {
  onBackendMessage: (callback) => {
    ipcRenderer.on('backend-message', (_event, message) => callback(message));
  },
  onBackendStatus: (callback) => {
    ipcRenderer.on('backend-status', (_event, status) => callback(status));
  },
  sendControl: (message) => {
    ipcRenderer.send('control-message', message);
  },
});
```

### 8.4 Renderer — minimal UI wiring

```javascript
// renderer/app.js
const transcriptEl = document.getElementById('transcript');
const statusEl = document.getElementById('status');

window.assistantBridge.onBackendStatus((status) => {
  statusEl.textContent = `Backend: ${status.status}`;
});

window.assistantBridge.onBackendMessage((message) => {
  switch (message.type) {
    case 'transcript_partial':
      updatePartialLine(message.text);
      break;
    case 'transcript_final':
      appendFinalLine('user', message.text);
      break;
    case 'assistant_text':
      appendFinalLine('assistant', message.text);
      break;
    case 'pipeline_state':
      // 'listening' | 'thinking' | 'speaking' | 'interrupted'
      statusEl.textContent = message.state;
      break;
    case 'interruption':
      statusEl.textContent = 'interrupted — listening';
      break;
    case 'error':
      statusEl.textContent = `error: ${message.detail}`;
      break;
  }
});

function appendFinalLine(role, text) {
  const line = document.createElement('div');
  line.className = `line ${role}`;
  line.textContent = text;
  transcriptEl.appendChild(line);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function updatePartialLine(text) {
  let partial = document.getElementById('partial-line');
  if (!partial) {
    partial = document.createElement('div');
    partial.id = 'partial-line';
    partial.className = 'line user partial';
    transcriptEl.appendChild(partial);
  }
  partial.textContent = text;
}

document.getElementById('mute-btn').addEventListener('click', () => {
  window.assistantBridge.sendControl({ type: 'toggle_mute' });
});

document.getElementById('restart-btn').addEventListener('click', () => {
  window.assistantBridge.sendControl({ type: 'restart_conversation' });
});
```

### 8.5 Python side of the WebSocket bridge

```python
# backend/ws_bridge.py
import asyncio
import json
import websockets

class ControlBridge:
    def __init__(self, pipeline_task):
        self.pipeline_task = pipeline_task
        self.clients = set()

    async def handler(self, websocket):
        self.clients.add(websocket)
        try:
            async for raw in websocket:
                message = json.loads(raw)
                await self.handle_control_message(message)
        finally:
            self.clients.discard(websocket)

    async def handle_control_message(self, message):
        if message["type"] == "toggle_mute":
            await self.pipeline_task.toggle_mute()
        elif message["type"] == "restart_conversation":
            await self.pipeline_task.restart_conversation()

    async def broadcast(self, message: dict):
        if not self.clients:
            return
        raw = json.dumps(message)
        await asyncio.gather(*(client.send(raw) for client in self.clients), return_exceptions=True)

    async def start(self, host="localhost", port=8765):
        return await websockets.serve(self.handler, host, port)
```

This bridge is wired into the Pipecat pipeline via a small custom `FrameProcessor` that watches for transcript, assistant-text, and pipeline-state-changing frames and calls `bridge.broadcast(...)` for each — kept separate from the fallback processor in Section 7 for clarity, though both can be combined into one processor if preferred.

### 8.6 Message protocol reference

| `type` | Direction | Payload | Purpose |
|---|---|---|---|
| `transcript_partial` | backend → renderer | `{ text }` | Live partial ASR output while user is speaking |
| `transcript_final` | backend → renderer | `{ text }` | Finalized user turn after turn-detection confirms completion |
| `assistant_text` | backend → renderer | `{ text }` | Assistant's response text, streamed or finalized |
| `pipeline_state` | backend → renderer | `{ state }` | One of `listening`, `thinking`, `speaking`, `interrupted` |
| `interruption` | backend → renderer | `{}` | Fired when a barge-in interruption occurs |
| `error` | backend → renderer | `{ detail }` | Non-fatal backend error, shown in UI rather than crashing |
| `toggle_mute` | renderer → backend | `{}` | Mutes/unmutes the microphone input |
| `restart_conversation` | renderer → backend | `{}` | Clears conversation context and restarts the pipeline's context aggregator |

### 8.7 Packaging considerations

- Bundle the Python backend as a standalone executable (e.g. via PyInstaller) for distribution, rather than requiring end users to have a Python environment set up, if this is ever distributed beyond the development machine.
- Piper voice models and the fallback filler `.wav` files should ship as app resources (Electron's `extraResources` in `electron-builder` config) so they're available at the expected path regardless of install location.
- Ollama itself is a separate system dependency — the app should detect whether Ollama is running on startup and surface a clear message if it isn't, rather than failing silently.

---

## 9. Project folder structure

```
voice-assistant/
├── backend/
│   ├── run_pipeline.py            # entry point, builds and runs the Pipecat pipeline
│   ├── fallback_processor.py       # custom filler/fallback FrameProcessor
│   ├── ws_bridge.py                 # WebSocket control/status bridge
│   ├── generate_fillers.py          # one-time script to pre-synthesize filler audio
│   ├── requirements.txt
│   └── Modelfile                    # Ollama custom model definition
├── electron/
│   ├── main.js
│   ├── preload.js
│   └── package.json
├── renderer/
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── assets/
│   └── fillers/
│       ├── tier1/
│       └── tier2/
├── implementation.md
├── process.md
└── README.md
```

---

## 10. Dependencies

`backend/requirements.txt`:

```
pipecat-ai
faster-whisper
piper-tts
websockets
```

(Pin exact versions once the pipeline is working and tested — `pipecat-ai` in particular has moved fast enough across releases that pinning is important for reproducibility.)

`electron/package.json` (relevant excerpt):

```json
{
  "name": "voice-assistant",
  "version": "0.1.0",
  "main": "main.js",
  "dependencies": {
    "ws": "^8.0.0"
  },
  "devDependencies": {
    "electron": "^33.0.0",
    "electron-builder": "^25.0.0"
  }
}
```

System-level dependencies (not managed by either package manager above):

- Ollama, installed and running as a background service, with the target model(s) pulled
- Piper's voice model file(s) downloaded and placed under `assets/`

---

## 11. Configuration reference

| Parameter | Location | Default | Notes |
|---|---|---|---|
| `vad.confidence` | VAD | 0.6 | Raise if false triggers from background noise are observed |
| `vad.start_secs` | VAD | 0.2 | Minimum speech duration before VAD reports "started" |
| `vad.stop_secs` | VAD | 0.8 | Minimum silence duration before VAD reports "stopped" (feeds into, but does not replace, Smart Turn) |
| `stt.model` | ASR | `small.en` | Upgrade to `medium.en` if latency budget allows |
| `stt.compute_type` | ASR | `int8` | CPU-optimized quantization |
| `llm.model` | LLM | `voice-assistant` (custom Modelfile on `llama3.2:3b`) | Swap base to `llama3.1:8b-instruct-q4_K_M` for quality testing |
| `llm.num_predict` | LLM | 200 | Caps response length so replies stay conversational, not essay-length |
| `llm.temperature` | LLM | 0.7 | Adjust for more/less varied phrasing |
| `tts.voice_id` | TTS | `en_US-lessac-medium` | Swap for other installed Piper voices |
| `fallback.first_token_timeout_s` | Fallback | 0.5 | Threshold before tier-1 filler plays |
| `fallback.long_delay_timeout_s` | Fallback | 2.5 | Threshold before tier-2 filler plays |
| `interruption.turn_start_sensitivity` | Interruption | high (fires on VAD start, doesn't wait for Smart Turn) | Deliberately more eager than turn-stop |
| `bridge.port` | Electron ↔ backend | 8765 | Local WebSocket port |

---

## 12. Latency budget and benchmarking methodology

Target breakdown (restated from earlier discussion, included here for completeness):

| Stage | Target |
|---|---|
| VAD + turn-end detection | 150–300ms |
| ASR finalization | 50–150ms |
| LLM time-to-first-token | 200–500ms |
| TTS time-to-first-audio-chunk | 150–300ms |
| Orchestration/IPC overhead | 50–150ms |
| **Total** | **~1–1.4s** |

Benchmarking methodology:

1. Instrument each stage boundary with a timestamp (Pipecat's built-in metrics — time-to-first-byte, time-to-first-audio — are a good starting point; check the installed version's metrics module for exact frame/field names).
2. Run a fixed set of representative test utterances (short factual questions, longer open-ended questions, multi-clause sentences with natural pauses) at least 20 times each, logging per-stage latency.
3. Compute p50 and p95 for each stage, not just the average — voice UX is sensitive to tail latency, since a single slow response breaks the illusion of fluid conversation even if the average is fine.
4. Re-run the same benchmark after each model swap (3B vs 8B LLM, `small.en` vs `medium.en` ASR) to make an evidence-based tradeoff decision rather than a guess.
5. Separately benchmark interruption latency: time from the user starting to speak (VAD trigger) to audible silence from the assistant, target well under 200ms.

---

## 13. Testing plan

- **Unit level**: each service wrapper (STT, LLM, TTS) tested in isolation with fixed inputs to confirm expected outputs and streaming behavior.
- **Pipeline integration**: full loop tested end-to-end with scripted audio input (pre-recorded test utterances played through a virtual audio device) to get repeatable latency measurements without relying on a live human tester for every run.
- **Interruption scenarios**: explicitly test —
  - User pauses mid-sentence, resumes before Smart Turn declares the turn over (should not trigger a premature LLM call).
  - User interrupts mid-assistant-response (should flush TTS, cancel LLM generation, and correctly consolidate context).
  - User interrupts during a filler clip (should crossfade/stop the filler correctly, not just the "real" response).
- **Fallback scenarios**: explicitly test —
  - Artificially delay the LLM (e.g. via a slow/loaded model) to confirm tier-1 and tier-2 fillers trigger at the correct thresholds.
  - Kill the Ollama process mid-conversation to confirm graceful degradation rather than a crash or silent hang.
- **Electron integration**: confirm the backend crash/restart path surfaces correctly in the UI, and that the WebSocket reconnects cleanly if the backend restarts.
- **Long-running session test**: run a single conversation session for an extended period (e.g. 30+ minutes of intermittent use) to catch memory leaks or gradual latency drift, particularly in the Python backend process.

---

## 14. Build and rollout order

1. Get the core Pipecat pipeline (Section 5) working headlessly — no Electron, no fallback layer yet. Validate VAD, Smart Turn, ASR, LLM, and TTS are correctly wired and producing audible responses.
2. Measure baseline latency per the methodology in Section 12, before adding any additional layers, to establish a clean baseline.
3. Add and test interruption handling (Section 6) in isolation.
4. Add the fallback/filler processor (Section 7) and test its trigger thresholds explicitly.
5. Build the Electron shell (Section 8): process spawning, WebSocket bridge, minimal UI. Confirm control messages (mute, restart) work correctly.
6. Re-run the full latency and interruption test suite with Electron in the loop, to confirm the WebSocket bridge doesn't introduce a noticeable control-plane delay (it shouldn't, since it's out of the audio path, but verify rather than assume).
7. Iterate on model sizing (3B vs 8B LLM, `small.en` vs `medium.en` ASR, Piper voice selection) using the benchmark data gathered in prior steps.
8. Polish: packaging (Section 8.7), startup health checks (Ollama running, models pulled, audio devices available), and error surfacing in the UI.

---

## 15. Known risks and mitigations

| Risk | Mitigation |
|---|---|
| Smart Turn model misjudges turn completion on unusual speech patterns | Keep a fixed-timeout fallback stop strategy available as a configurable alternative; log false-positive/negative rates during testing |
| 8GB VRAM is exceeded if the LLM model is upgraded carelessly | Stay within the tested model list (Section 4.4); benchmark VRAM headroom explicitly before adopting a larger model |
| CPU contention between ASR, TTS, and turn-detection models degrades latency under load | Benchmark with all CPU-bound stages running simultaneously, not just in isolation; consider reducing Whisper model size if contention is observed |
| Ollama process crash mid-conversation | Graceful degradation path (Section 7, item 4) plus backend auto-restart with backoff (Section 8.2) |
| Electron backend process fails to start (e.g. Python environment misconfigured) | Startup health check in Electron's main process, clear error state surfaced in UI rather than a blank/frozen window |
| Offline requirement becomes infeasible on lower-spec hardware than the target machine | Documented online fallback path exists (cloud ASR/TTS/LLM) as a last resort, not enabled by default |
| Interruption during an in-flight tool/function call corrupts state (relevant once Section 16 extensions are added) | Explicit test case before shipping any tool-use feature; rely on `UninterruptibleFrame` marking for in-flight tool calls |

---

## 16. Future extensions

Documented here as known future directions, out of scope for the current build:

- **Tool/function calling**: giving the LLM the ability to call local tools (e.g. checking the system clock, running a calculation, querying a local file). Requires explicit interruption testing per the risk noted in Section 15.
- **Wake-word activation**: adding a lightweight wake-word detector (e.g. Porcupine or an openWakeWord model) so the assistant doesn't process all ambient audio, only audio following a wake phrase — relevant if always-on listening proves too sensitive or privacy-uncomfortable in practice.
- **Persistent memory across sessions**: storing conversation history or user preferences locally (e.g. in a local SQLite database) so context carries over between app restarts, rather than resetting each session.
- **Retrieval-augmented responses (RAG)**: connecting the LLM to a local document store for domain-specific Q&A, which would sit between the context aggregator and the LLM call in the pipeline.
- **Multi-voice / voice cloning**: swapping Piper for a more expressive TTS engine (e.g. Coqui XTTS) if voice naturalness becomes a priority over the current latency-first tradeoff.
- **Telephony or remote access**: if remote (non-local) access is ever needed, this is the point at which LiveKit Agents' WebRTC transport (rejected for the current local-only scope, see `process.md`) would become the right tool rather than the wrong one.

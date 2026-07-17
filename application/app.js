// renderer/app.js
// Pure UI logic. All data arrives via window.assistantBridge, exposed by
// preload.js over a context-isolated bridge -- this file never touches
// Node or Electron APIs directly (implementation.md Section 8.1).

const transcriptEl = document.getElementById('transcript');
const emptyStateEl = document.getElementById('empty-state');
const statusEl = document.getElementById('status');
const statusSubEl = document.getElementById('status-sub');
const stateRingEl = document.getElementById('state-ring');
const micWave1El = document.getElementById('mic-wave-1');
const micWave2El = document.getElementById('mic-wave-2');
const connIndicatorEl = document.getElementById('conn-indicator');
const connLabelEl = document.getElementById('conn-label');
const audioMeterFillEl = document.getElementById('audio-meter-fill');
const muteBtn = document.getElementById('mute-btn');
const muteLabelEl = document.getElementById('mute-label');
const restartBtn = document.getElementById('restart-btn');
const micSelectEl = document.getElementById('mic-select');

const STATE_COPY = {
  listening: { label: 'Listening', sub: 'Speak naturally, anytime' },
  thinking: { label: 'Thinking', sub: 'Putting a response together' },
  speaking: { label: 'Speaking', sub: 'Interrupt anytime by talking' },
  interrupted: { label: 'Listening', sub: 'Picking up where you left off' },
  filler_tier1: { label: 'Thinking', sub: 'Just a moment longer' },
  filler_tier2: { label: 'Thinking', sub: 'Still working on it' },
  crossfade_to_response: { label: 'Speaking', sub: 'Interrupt anytime by talking' },
};

const CONN_COPY = {
  connecting: 'Connecting',
  connected: 'Connected',
  disconnected: 'Reconnecting…',
  crashed: 'Backend crashed',
  'failed-permanently': 'Backend unavailable',
};

function setPipelineState(state) {
  const copy = STATE_COPY[state] || { label: state, sub: '' };
  statusEl.textContent = copy.label;
  statusSubEl.textContent = copy.sub;

  // The ring only distinguishes the four core states; filler/crossfade
  // states map onto the closest visual (thinking / speaking) so the
  // ring doesn't need a bespoke animation for every sub-state.
  const ringState =
    state === 'filler_tier1' || state === 'filler_tier2'
      ? 'thinking'
      : state === 'crossfade_to_response'
      ? 'speaking'
      : state;
  stateRingEl.dataset.state = ['listening', 'thinking', 'speaking', 'interrupted'].includes(ringState)
    ? ringState
    : 'listening';
}

function setConnStatus(status) {
  connIndicatorEl.dataset.state = status;
  connLabelEl.textContent = CONN_COPY[status] || status;
}

// Drives the two mic-wave rings from the backend's `audio_level` messages
// (pipeline/audio_meter.py -- a single normalized 0..1 RMS float computed
// per mic frame, throttled to ~20/sec). The two rings scale by different
// amounts so the effect reads as a soft outward pulse rather than a single
// rigid ring, purely a visual choice -- there's no meaning to which ring is
// "first" beyond that.
function setAudioLevel(level) {
  const clamped = Math.max(0, Math.min(1, Number(level) || 0));
  const scale1 = 1 + clamped * 0.35;
  const scale2 = 1 + clamped * 0.75;
  const opacity1 = clamped > 0.02 ? 0.18 + clamped * 0.45 : 0;
  const opacity2 = clamped > 0.02 ? 0.1 + clamped * 0.3 : 0;

  micWave1El.style.transform = `translate(-50%, -50%) scale(${scale1})`;
  micWave1El.style.opacity = String(opacity1);
  micWave2El.style.transform = `translate(-50%, -50%) scale(${scale2})`;
  micWave2El.style.opacity = String(opacity2);
  
  if (audioMeterFillEl) {
    audioMeterFillEl.style.width = `${clamped * 100}%`;
  }
}

function clearEmptyState() {
  if (emptyStateEl && emptyStateEl.parentElement) {
    emptyStateEl.remove();
  }
}

function appendFinalLine(role, text) {
  clearEmptyState();
  // A finalized line replaces any in-progress partial line for the same turn.
  const partial = document.getElementById('partial-line');
  if (partial && role === 'user') {
    partial.remove();
  }
  const line = document.createElement('div');
  line.className = `line ${role}`;
  line.textContent = text;
  transcriptEl.appendChild(line);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function updatePartialLine(text) {
  clearEmptyState();
  let partial = document.getElementById('partial-line');
  if (!partial) {
    partial = document.createElement('div');
    partial.id = 'partial-line';
    partial.className = 'line user partial';
    transcriptEl.appendChild(partial);
  }
  partial.textContent = text;
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

window.assistantBridge.onBackendStatus((status) => {
  setConnStatus(status.status);
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
      setPipelineState(message.state);
      break;
    case 'audio_level':
      setAudioLevel(message.level);
      break;
    case 'audio_devices':
      if (micSelectEl) {
        micSelectEl.innerHTML = '';
        message.devices.forEach((d) => {
          const opt = document.createElement('option');
          opt.value = d.index;
          opt.textContent = d.name;
          if (d.index === message.current) {
            opt.selected = true;
          }
          micSelectEl.appendChild(opt);
        });
      }
      break;
    case 'interruption':
      setPipelineState('interrupted');
      break;
    case 'error':
      statusSubEl.textContent = message.detail;
      break;
    default:
      console.warn('Unhandled backend message type:', message.type);
  }
});

muteBtn.addEventListener('click', () => {
  const currentlyMuted = muteBtn.dataset.muted === 'true';
  const next = !currentlyMuted;
  muteBtn.dataset.muted = String(next);
  stateRingEl.dataset.muted = String(next);
  muteLabelEl.textContent = next ? 'Unmute' : 'Mute';
  window.assistantBridge.sendControl({ type: 'toggle_mute' });
});

if (micSelectEl) {
  micSelectEl.addEventListener('change', () => {
    const index = parseInt(micSelectEl.value, 10);
    if (!isNaN(index)) {
      window.assistantBridge.sendControl({ type: 'set_mic', index });
    }
  });
}

restartBtn.addEventListener('click', () => {
  window.assistantBridge.sendControl({ type: 'restart_conversation' });
  transcriptEl.innerHTML = '';
  const fresh = document.createElement('div');
  fresh.className = 'empty-state';
  fresh.id = 'empty-state';
  fresh.textContent = 'Your conversation will appear here once you start speaking.';
  transcriptEl.appendChild(fresh);
  setPipelineState('listening');
});

// Initial state before the first backend message arrives.
setPipelineState('listening');
setConnStatus('connecting');
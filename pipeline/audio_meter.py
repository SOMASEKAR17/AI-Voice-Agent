"""
Microphone input level metering.

Not part of the original implementation.md spec -- added so the Electron UI
can show live "is my audio actually reaching the app" feedback (animated
waves around the mic icon that grow/shrink with input volume) without
violating the Section 8.1 design principle that raw audio never crosses the
Python/Electron boundary. This processor only ever sends a single small
float (a normalized 0..1 RMS level) over the existing WebSocket bridge --
never audio bytes.

Placed in the pipeline right after `transport.input()` (see run_pipeline.py)
so it observes every `InputAudioRawFrame` coming from the mic, computes a
level, and passes the frame through unchanged.
"""

import array
import logging
import time

from pipecat.frames.frames import InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

log = logging.getLogger("audio_meter")


class AudioLevelMeterProcessor(FrameProcessor):
    def __init__(self, on_level, min_interval_s: float = 0.05, gain: float = 4.0):
        """
        Args:
            on_level: async callback(level: float) where level is clamped
                to [0.0, 1.0]. Called at most once every `min_interval_s`
                seconds, regardless of how often audio frames arrive, so
                the WebSocket/IPC path isn't flooded.
            min_interval_s: throttle interval between on_level calls.
            gain: multiplier applied to raw RMS before clamping to 0..1 --
                typical conversational speech RMS on a 16-bit PCM stream is
                well below full scale, so a flat 1:1 mapping would make the
                UI barely move. Tune this if waves feel too flat or too
                maxed-out on your mic/gain setup (VA_AUDIO_METER_GAIN env
                var, see config.py).
        """
        super().__init__()
        self.on_level = on_level
        self.min_interval_s = min_interval_s
        self.gain = gain
        self._last_sent = 0.0

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame):
            level = self._compute_level(frame.audio)
            now = time.monotonic()
            if now - self._last_sent >= self.min_interval_s:
                self._last_sent = now
                try:
                    await self.on_level(level)
                except Exception:
                    log.exception("audio level callback failed")

        await self.push_frame(frame, direction)

    def _compute_level(self, pcm_bytes: bytes) -> float:
        # 16-bit signed PCM, matching Silero/Whisper's expected input
        # (implementation.md Section 4.1: sample_rate 16000, standard
        # 16-bit frames). `array` is used instead of numpy here to avoid
        # pulling in a heavier dependency just for this metering path --
        # numpy is already installed transitively (torch/faster-whisper),
        # but this keeps the module importable even in a minimal env.
        if not pcm_bytes:
            return 0.0
        try:
            samples = array.array("h", pcm_bytes)
        except ValueError:
            # Odd number of bytes -- a truncated/misaligned chunk; skip it
            # rather than crashing the pipeline over a single bad frame.
            return 0.0
        if not samples:
            return 0.0

        sum_sq = 0
        for s in samples:
            sum_sq += s * s
        rms = (sum_sq / len(samples)) ** 0.5

        normalized = (rms / 32768.0) * self.gain
        return max(0.0, min(1.0, normalized))

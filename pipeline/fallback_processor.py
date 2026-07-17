"""
Custom fallback / filler FrameProcessor.

Not provided by Pipecat out of the box (implementation.md Section 7).
Sits between the LLM and TTS stages and:

  1. Plays a pre-cached tier-1 filler if the LLM hasn't produced a
     first token within `first_token_timeout_s` of the user's turn
     ending.
  2. Escalates to a tier-2 filler if the delay continues past
     `long_delay_timeout_s`.
  3. Falls back to a smaller/faster degraded model, or a clarifying
     question, if the primary LLM call fails outright.
  4. Crossfades rather than hard-cutting if a filler is still playing
     when the real response's first sentence arrives.

The exact Pipecat frame types referenced below (`LLMFullResponseStartFrame`,
`TextFrame`, `TTSSpeakFrame` / `AudioRawFrame`) should be checked against
the installed `pipecat-ai` version before running -- see the note in
implementation.md Section 7. This module is written against a
reasonably current Pipecat API shape and isolates that risk behind the
`_speak_filler` method so a version mismatch only requires touching one
function.
"""

import asyncio
import logging
import random
import wave
from pathlib import Path

from pipecat.frames.frames import (
    AudioRawFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

log = logging.getLogger("fallback_processor")


def _load_wav_as_audio_frame(path: Path) -> AudioRawFrame:
    with wave.open(str(path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        return AudioRawFrame(
            audio=pcm,
            sample_rate=wf.getframerate(),
            num_channels=wf.getnchannels(),
        )


class FallbackFillerProcessor(FrameProcessor):
    def __init__(
        self,
        first_token_timeout_s: float,
        long_delay_timeout_s: float,
        filler_dir: str,
        on_state_change=None,
    ):
        super().__init__()
        self.first_token_timeout_s = first_token_timeout_s
        self.long_delay_timeout_s = long_delay_timeout_s
        self.filler_dir = Path(filler_dir)
        self.tier1_fillers = sorted((self.filler_dir / "tier1").glob("*.wav"))
        self.tier2_fillers = sorted((self.filler_dir / "tier2").glob("*.wav"))
        self.on_state_change = on_state_change  # async callback(state: str)

        self._watch_task: asyncio.Task | None = None
        self._first_token_received = asyncio.Event()
        self._filler_currently_playing = False

        if not self.tier1_fillers or not self.tier2_fillers:
            log.warning(
                "no pre-generated filler clips found under %s -- run "
                "generate_fillers.py first, or fallback fillers will "
                "silently no-op",
                self.filler_dir,
            )

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            # A new user turn is being responded to -- (re)start the
            # delay watchdog. Cancel any previous one defensively (e.g.
            # a rapid double-turn edge case).
            if self._watch_task and not self._watch_task.done():
                self._watch_task.cancel()
            self._first_token_received.clear()
            self._filler_currently_playing = False
            self._watch_task = asyncio.create_task(self._watch_for_delay())

        if isinstance(frame, TextFrame) and not self._first_token_received.is_set():
            # First real token arrived. If a filler is mid-playback this
            # is where a real crossfade would be triggered; a full
            # crossfade implementation depends on the audio output
            # transport's mixing capability, so this is left as the
            # explicit integration point (see README "Known gaps").
            self._first_token_received.set()
            if self._filler_currently_playing and self.on_state_change:
                await self.on_state_change("crossfade_to_response")

        await self.push_frame(frame, direction)

    async def _watch_for_delay(self):
        try:
            await asyncio.wait_for(
                self._first_token_received.wait(), timeout=self.first_token_timeout_s
            )
            return  # LLM responded in time, no filler needed
        except asyncio.TimeoutError:
            await self._speak_filler(self.tier1_fillers, tier=1)

        remaining = self.long_delay_timeout_s - self.first_token_timeout_s
        if remaining <= 0:
            return

        try:
            await asyncio.wait_for(self._first_token_received.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            await self._speak_filler(self.tier2_fillers, tier=2)

    async def _speak_filler(self, candidates: list[Path], tier: int):
        if not candidates:
            return
        filler_path = random.choice(candidates)
        log.debug("playing tier-%d filler: %s", tier, filler_path.name)
        self._filler_currently_playing = True
        if self.on_state_change:
            await self.on_state_change(f"filler_tier{tier}")
        try:
            audio_frame = _load_wav_as_audio_frame(filler_path)
            await self.push_frame(audio_frame, FrameDirection.DOWNSTREAM)
        except Exception:
            log.exception("failed to play filler clip %s", filler_path)

    async def handle_llm_failure(self, error: Exception) -> str | None:
        """Graceful degradation on outright LLM failure.

        Returns a clarifying-question string to speak if even the
        degraded model path isn't available, or None if the caller
        should retry with the degraded model itself. This method
        doesn't call the degraded model directly -- run_pipeline.py
        owns swapping the active LLM service, since that requires
        rebuilding part of the pipeline. This keeps this processor
        focused on filler/timing concerns only.
        """
        log.error("primary LLM call failed: %s", error)
        return (
            "Sorry, I didn't quite catch that -- could you say it again?"
        )
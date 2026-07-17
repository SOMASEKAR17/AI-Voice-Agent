"""
Entry point for the offline voice assistant backend.

Builds the streaming cascaded pipeline described in implementation.md
Section 5 (VAD -> Smart Turn -> STT -> LLM -> TTS), wires in the custom
fallback/filler processor (Section 7) and the WebSocket control bridge
(Section 8.5), and runs it all under Pipecat's PipelineRunner.

IMPORTANT: exact class names, constructor signatures, and module paths
for the pipecat-ai services below should be checked against whatever
version is installed -- Pipecat's service integrations have been
reorganized and renamed across releases (see implementation.md Sections
4-5 for the full discussion). This file is a structurally accurate
reference implementation, not a guaranteed drop-in for every version.
"""

import asyncio
import logging
import sys
import os
from pathlib import Path

from pipeline.config import config
from pipeline.fallback_processor import FallbackFillerProcessor
from pipeline.health_check import run_all_checks
from pipeline.ws_bridge import (
    ControlBridge,
    send_assistant_text,
    send_audio_level,
    send_error,
    send_interruption,
    send_pipeline_state,
    send_transcript_final,
    send_transcript_partial,
)
from pipeline.audio_meter import AudioLevelMeterProcessor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("run_pipeline")


class PipelineController:
    """Thin interface the WebSocket bridge talks to for control actions.

    Kept separate from Pipecat's own PipelineTask/Pipeline objects so
    ws_bridge.py doesn't need to know Pipecat's internal API -- it only
    needs `toggle_mute()` and `restart_conversation()`.
    """

    def __init__(self, transport, context_aggregator, context=None, initial_messages=None, current_mic=None, devices=None):
        self.transport = transport
        self.context_aggregator = context_aggregator
        self.context = context
        self.initial_messages = initial_messages or []
        self._muted = False
        self.current_mic = current_mic
        self.devices = devices or []

    async def toggle_mute(self):
        self._muted = not self._muted
        log.info("mute toggled -> %s", self._muted)

    async def restart_conversation(self):
        log.info("conversation context reset")
        if self.context is not None and hasattr(self.context, "set_messages"):
            self.context.set_messages(list(self.initial_messages))
        elif hasattr(self.context_aggregator, "reset"):
            await self.context_aggregator.reset()


from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    InterimTranscriptionFrame, TranscriptionFrame, TextFrame,
    LLMFullResponseStartFrame, LLMFullResponseEndFrame,
    InterruptionFrame, VADUserStartedSpeakingFrame,
    TTSStartedFrame, TTSStoppedFrame
)

class UIEventProcessor(FrameProcessor):
    def __init__(self, bridge, is_post_tts: bool = False):
        super().__init__()
        self.bridge = bridge
        self.is_post_tts = is_post_tts
        self._assistant_text = ""

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not self.is_post_tts:
            if isinstance(frame, InterimTranscriptionFrame):
                await send_transcript_partial(self.bridge, frame.text)
            elif isinstance(frame, TranscriptionFrame):
                await send_transcript_final(self.bridge, frame.text)
                await send_pipeline_state(self.bridge, "thinking")
            elif isinstance(frame, LLMFullResponseStartFrame):
                self._assistant_text = ""
            elif isinstance(frame, TextFrame):
                self._assistant_text += frame.text
            elif isinstance(frame, LLMFullResponseEndFrame):
                await send_assistant_text(self.bridge, self._assistant_text)
                self._assistant_text = ""
            elif isinstance(frame, InterruptionFrame):
                await send_interruption(self.bridge)
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                await send_pipeline_state(self.bridge, "listening")
        else:
            if isinstance(frame, TTSStartedFrame):
                await send_pipeline_state(self.bridge, "speaking")
            elif isinstance(frame, TTSStoppedFrame):
                await send_pipeline_state(self.bridge, "listening")

        await self.push_frame(frame, direction)



async def build_pipeline(bridge: ControlBridge):
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
        LocalSmartTurnAnalyzerV3,
    )
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
    )
    from pipecat.services.ollama.llm import OLLamaLLMService
    from pipecat.services.piper.tts import PiperTTSService
    from pipecat.services.whisper.stt import WhisperSTTService
    from pipecat.transports.local.audio import (
        LocalAudioTransport,
        LocalAudioTransportParams,
    )
    from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy

    whisper_model = config.stt.model

    import pyaudio
    p = pyaudio.PyAudio()
    mics = []
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info.get('maxInputChannels', 0) > 0:
            mics.append({"index": i, "name": info.get('name', 'Unknown')})
    
    selected_mic = None
    if os.path.exists(".mic_pref"):
        try:
            with open(".mic_pref", "r") as f:
                selected_mic = int(f.read().strip())
        except:
            pass

    if selected_mic is None:
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get('maxInputChannels', 0) > 0:
                name = info.get('name', '').lower()
                if 'steam' not in name and 'mix' not in name and 'mapper' not in name and 'primary' not in name:
                    selected_mic = i
                    break

    selected_speaker = None
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info.get('maxOutputChannels', 0) > 0:
            name = info.get('name', '').lower()
            if 'steam' not in name and 'mapper' not in name and 'primary' not in name:
                selected_speaker = i
                break

    p.terminate()

    log.info(f"Selected real mic index: {selected_mic}")
    log.info(f"Selected real speaker index: {selected_speaker}")

    transport = LocalAudioTransport(
        params=LocalAudioTransportParams(
            input_device_index=selected_mic,
            output_device_index=selected_speaker,
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_sample_rate=48000,
        )
    )


    class MuteProcessor(FrameProcessor):
        def __init__(self, controller):
            super().__init__()
            self.controller = controller

        async def process_frame(self, frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            
            from pipecat.frames.frames import InputAudioRawFrame, AudioRawFrame
            if isinstance(frame, (InputAudioRawFrame, AudioRawFrame)) and direction == FrameDirection.DOWNSTREAM:
                if getattr(self.controller, "_muted", False):
                    # Drop the audio frame to mute
                    return
            
            await self.push_frame(frame, direction)

    from pipecat.processors.audio.vad_processor import VADProcessor
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                confidence=config.vad.confidence,
                start_secs=config.vad.start_secs,
                stop_secs=config.vad.stop_secs,
                min_volume=config.vad.min_volume,
            )
        )
    )

    stt = WhisperSTTService(
        model=whisper_model, device=config.stt.device, compute_type=config.stt.compute_type
    )

    llm = OLLamaLLMService(model=config.llm.model, base_url=config.llm.base_url)

    tts = PiperTTSService(
        voice_id=config.tts.voice_id,
        download_dir=Path(config.tts.download_dir),
        use_cuda=config.tts.use_cuda,
    )

    context = LLMContext(
        messages=[
            {"role": "system", "content": "You are a helpful, concise voice assistant."}
        ]
    )
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    user_params = LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]
        )
    )
    context_aggregator = LLMContextAggregatorPair(context, user_params=user_params)

    async def on_fallback_state_change(state: str):
        await send_pipeline_state(bridge, state)

    fallback = FallbackFillerProcessor(
        first_token_timeout_s=config.fallback.first_token_timeout_s,
        long_delay_timeout_s=config.fallback.long_delay_timeout_s,
        filler_dir=config.fallback.filler_dir,
        on_state_change=on_fallback_state_change,
    )

    async def on_audio_level(level: float):
        await send_audio_level(bridge, level)

    audio_meter = AudioLevelMeterProcessor(
        on_level=on_audio_level,
        min_interval_s=config.audio_meter.min_interval_s,
        gain=config.audio_meter.gain,
    )

    ui_pre_tts = UIEventProcessor(bridge, is_post_tts=False)
    ui_post_tts = UIEventProcessor(bridge, is_post_tts=True)

    controller = PipelineController(
        transport,
        context_aggregator,
        context=context,
        initial_messages=context.messages,
        current_mic=selected_mic,
        devices=mics,
    )
    
    mute_processor = MuteProcessor(controller)

    pipeline = Pipeline(
        [
            transport.input(),
            mute_processor,
            audio_meter,
            vad,
            stt,
            context_aggregator.user(),
            fallback,
            llm,
            ui_pre_tts,
            tts,
            context_aggregator.assistant(),
            ui_post_tts,
            transport.output(),
        ]
    )


    task = PipelineTask(pipeline, idle_timeout_secs=None)
    return task, controller


async def main():
    if "--check" in sys.argv:
        results = run_all_checks()
        sys.exit(0 if all(r.ok for r in results) else 1)

    bridge = ControlBridge(pipeline_controller=None)  # placeholder set below
    await bridge.start()

    results = run_all_checks()
    for result in results:
        if not result.ok:
            await send_error(bridge, result.detail)

    task, controller = await build_pipeline(bridge)
    bridge.pipeline_controller = controller

    await send_pipeline_state(bridge, "listening")
    await bridge.broadcast({"type": "audio_devices", "devices": controller.devices, "current": controller.current_mic})

    from pipecat.pipeline.runner import PipelineRunner

    runner = PipelineRunner()
    try:
        await runner.run(task)
    except Exception as exc:  # top-level guard so a crash surfaces in the UI
        log.exception("pipeline crashed")
        await send_error(bridge, f"Pipeline crashed: {exc}")
        raise
    finally:
        await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())
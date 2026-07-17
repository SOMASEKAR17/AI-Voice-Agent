"""
Central configuration for the offline voice assistant backend.

Every tunable parameter documented in implementation.md, Section 11
("Configuration reference") lives here as a single source of truth, so
run_pipeline.py, fallback_processor.py, and ws_bridge.py never hardcode
a magic number. Override any of these via environment variables without
touching code (see `_env` helper below), which is the easiest path for
per-machine tuning (e.g. a lower-spec laptop wanting `base.en` instead
of `small.en`).
"""

import os
from dataclasses import dataclass, field


def _env(name: str, default, cast=str):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


@dataclass
class VADConfig:
    sample_rate: int = _env("VA_VAD_SAMPLE_RATE", 16000, int)
    confidence: float = _env("VA_VAD_CONFIDENCE", 0.6, float)
    start_secs: float = _env("VA_VAD_START_SECS", 0.2, float)
    stop_secs: float = _env("VA_VAD_STOP_SECS", 0.8, float)
    min_volume: float = _env("VA_VAD_MIN_VOLUME", 0.6, float)


@dataclass
class TurnConfig:
    # "smart" uses the Smart Turn model; "fixed" falls back to a
    # conservative fixed-timeout strategy if Smart Turn proves unsuitable
    # on the deployed hardware (see implementation.md Section 4.2).
    strategy: str = _env("VA_TURN_STRATEGY", "smart", str)
    fixed_timeout_s: float = _env("VA_TURN_FIXED_TIMEOUT_S", 1.0, float)
    fixed_min_words: int = _env("VA_TURN_FIXED_MIN_WORDS", 2, int)
    # Interruption (turn-start) is deliberately more eager than turn-stop.
    interruption_turn_start_sensitivity: str = "high"


@dataclass
class STTConfig:
    model: str = _env("VA_STT_MODEL", "small.en", str)  # or "medium.en"
    device: str = _env("VA_STT_DEVICE", "cpu", str)
    compute_type: str = _env("VA_STT_COMPUTE_TYPE", "int8", str)


@dataclass
class LLMConfig:
    model: str = _env("VA_LLM_MODEL", "voice-assistant", str)
    base_url: str = _env("VA_LLM_BASE_URL", "http://localhost:11434/v1", str)
    num_predict: int = _env("VA_LLM_NUM_PREDICT", 200, int)
    temperature: float = _env("VA_LLM_TEMPERATURE", 0.7, float)
    # Used only by the graceful-degradation path in the fallback processor
    # if the primary model errors out completely.
    degraded_model: str = _env("VA_LLM_DEGRADED_MODEL", "llama3.2:3b", str)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class TTSConfig:
    voice_id: str = _env("VA_TTS_VOICE_ID", "en_US-lessac-medium", str)
    # PiperTTSService loads the voice model in-process (no separate Piper
    # HTTP server in the installed pipecat version) -- this is the folder
    # it looks in for `{voice_id}.onnx`, downloading it there if missing.
    download_dir: str = _env("VA_TTS_DOWNLOAD_DIR", "./assets/voices", str)
    use_cuda: bool = _env_bool("VA_TTS_USE_CUDA", False)


@dataclass
class FallbackConfig:
    first_token_timeout_s: float = _env("VA_FALLBACK_TIER1_TIMEOUT_S", 0.5, float)
    long_delay_timeout_s: float = _env("VA_FALLBACK_TIER2_TIMEOUT_S", 2.5, float)
    filler_dir: str = _env("VA_FALLBACK_FILLER_DIR", "./assets/fillers", str)


@dataclass
class AudioMeterConfig:
    # Not part of the original Section 11 config table -- controls the
    # mic-input-level metering added for the UI's audio waves (see
    # pipeline/audio_meter.py). Not the same thing as vad.min_volume, which
    # gates speech detection rather than driving a visual.
    gain: float = _env("VA_AUDIO_METER_GAIN", 4.0, float)
    min_interval_s: float = _env("VA_AUDIO_METER_INTERVAL_S", 0.05, float)


@dataclass
class BridgeConfig:
    host: str = _env("VA_BRIDGE_HOST", "localhost", str)
    port: int = _env("VA_BRIDGE_PORT", 8765, int)


@dataclass
class AppConfig:
    vad: VADConfig = field(default_factory=VADConfig)
    turn: TurnConfig = field(default_factory=TurnConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    fallback: FallbackConfig = field(default_factory=FallbackConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    audio_meter: AudioMeterConfig = field(default_factory=AudioMeterConfig)


config = AppConfig()
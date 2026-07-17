"""
Startup health checks (implementation.md Section 8.7 / Section 15 risk
table): the app should detect whether Ollama is running and the target
model is pulled, and whether audio devices are available, surfacing a
clear message in the UI rather than failing silently or freezing.

`run_pipeline.py` calls `run_all_checks()` before building the Pipecat
pipeline and broadcasts any failures over the WebSocket bridge as
`error` messages, so the Electron UI can show a real explanation
instead of a blank window.
"""

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

from pipeline.config import config

log = logging.getLogger("health_check")


@dataclass
class HealthCheckResult:
    ok: bool
    detail: str


def check_ollama_running(timeout_s: float = 2.0) -> HealthCheckResult:
    base = config.llm.base_url.rstrip("/")
    # base_url is the OpenAI-compatible path (.../v1); the root API
    # lives one level up.
    root = base[:-3] if base.endswith("/v1") else base
    try:
        with urllib.request.urlopen(f"{root}/api/tags", timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return HealthCheckResult(
            ok=False,
            detail=(
                "Can't reach Ollama at "
                f"{root}. Is Ollama installed and running? ({exc})"
            ),
        )

    if config.llm.model not in body:
        return HealthCheckResult(
            ok=False,
            detail=(
                f"Ollama is running, but the model '{config.llm.model}' "
                "hasn't been created/pulled yet. Run: "
                f"`ollama create {config.llm.model} -f ./pipeline/Modelfile`"
            ),
        )
    return HealthCheckResult(ok=True, detail="Ollama reachable and model available.")


def check_audio_devices() -> HealthCheckResult:
    try:
        import sounddevice as sd
    except ImportError:
        return HealthCheckResult(
            ok=False,
            detail=(
                "The `sounddevice` package isn't installed, so audio "
                "device availability can't be verified. This is only "
                "used for the startup check; Pipecat's own audio "
                "transport may use a different backend."
            ),
        )

    try:
        devices = sd.query_devices()
    except Exception as exc:  # pragma: no cover - depends on host audio stack
        return HealthCheckResult(ok=False, detail=f"Could not query audio devices: {exc}")

    has_input = any(d.get("max_input_channels", 0) > 0 for d in devices)
    has_output = any(d.get("max_output_channels", 0) > 0 for d in devices)

    if not has_input or not has_output:
        missing = []
        if not has_input:
            missing.append("microphone input")
        if not has_output:
            missing.append("speaker output")
        return HealthCheckResult(
            ok=False, detail=f"No {' or '.join(missing)} device found."
        )
    return HealthCheckResult(ok=True, detail="Microphone and speaker detected.")


def check_filler_assets() -> HealthCheckResult:
    from pathlib import Path

    filler_dir = Path(config.fallback.filler_dir)
    tier1 = list((filler_dir / "tier1").glob("*.wav"))
    tier2 = list((filler_dir / "tier2").glob("*.wav"))
    if not tier1 or not tier2:
        return HealthCheckResult(
            ok=False,
            detail=(
                "No pre-generated filler clips found under "
                f"{filler_dir}. Run `python generate_fillers.py --voice "
                "<path-to-voice.onnx>` first. The assistant will still "
                "run, but delayed responses will go silent instead of "
                "playing a filler."
            ),
        )
    return HealthCheckResult(ok=True, detail="Filler clips present.")


def run_all_checks() -> list[HealthCheckResult]:
    checks = [check_ollama_running(), check_audio_devices(), check_filler_assets()]
    for result in checks:
        level = logging.INFO if result.ok else logging.WARNING
        log.log(level, result.detail)
    return checks
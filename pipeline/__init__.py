"""Offline voice assistant backend package (Pipecat pipeline host).

Kept intentionally empty -- submodules (config, run_pipeline,
fallback_processor, ws_bridge, health_check, generate_fillers) are
imported directly, e.g. `from pipeline.config import config`. This file
only needs to exist so `pipeline` resolves as a regular package when
the app is launched from the project root via `python -m pipeline.run_pipeline`.
"""

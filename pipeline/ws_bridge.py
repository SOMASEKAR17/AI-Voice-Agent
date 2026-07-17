"""
Local WebSocket bridge between the Python/Pipecat backend and the
Electron renderer.

Per implementation.md Section 8.1: raw audio never crosses this
boundary. Only control messages (renderer -> backend) and text/state
updates (backend -> renderer) travel over this socket. See Section 8.6
for the full message protocol reference this module implements.
"""

import asyncio
import json
import logging

import websockets

from pipeline.config import config

log = logging.getLogger("ws_bridge")


class ControlBridge:
    """Owns the local WebSocket server and both directions of traffic.

    `pipeline_controller` is any object exposing async `toggle_mute()`
    and `restart_conversation()` methods -- in this project that's the
    `PipelineController` defined in run_pipeline.py, kept separate from
    Pipecat's own `PipelineTask` so this bridge has no dependency on
    Pipecat's internals beyond that thin interface.
    """

    def __init__(self, pipeline_controller):
        self.pipeline_controller = pipeline_controller
        self.clients: set[websockets.WebSocketServerProtocol] = set()
        self._server = None

    async def handler(self, websocket):
        self.clients.add(websocket)
        log.info("renderer connected (%d client(s))", len(self.clients))
        try:
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("dropped malformed control message: %r", raw)
                    continue
                await self.handle_control_message(message)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            log.info("renderer disconnected (%d client(s))", len(self.clients))

    async def handle_control_message(self, message: dict):
        msg_type = message.get("type")
        if msg_type == "toggle_mute":
            await self.pipeline_controller.toggle_mute()
        elif msg_type == "restart_conversation":
            await self.pipeline_controller.restart_conversation()
        elif msg_type == "set_mic":
            idx = message.get("index")
            if idx is not None:
                log.info("setting mic pref to %s and restarting", idx)
                with open(".mic_pref", "w") as f:
                    f.write(str(idx))
                import sys
                sys.exit(0)
        else:
            log.warning("unknown control message type: %s", msg_type)

    async def broadcast(self, message: dict):
        """Send a status/text update to every connected renderer.

        Silently no-ops if nothing is connected yet (e.g. Electron
        hasn't finished its 1.5s startup delay before connecting) --
        state broadcasts are not queued or replayed, since the renderer
        re-syncs to the latest `pipeline_state` on every subsequent event.
        """
        if not self.clients:
            return
        raw = json.dumps(message)
        results = await asyncio.gather(
            *(client.send(raw) for client in list(self.clients)),
            return_exceptions=True,
        )
        for client, result in zip(list(self.clients), results):
            if isinstance(result, Exception):
                self.clients.discard(client)

    async def start(self):
        self._server = await websockets.serve(
            self.handler, config.bridge.host, config.bridge.port
        )
        log.info("control bridge listening on ws://%s:%d", config.bridge.host, config.bridge.port)
        return self._server

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


# --- Convenience broadcast helpers matching the Section 8.6 protocol ---

async def send_transcript_partial(bridge: ControlBridge, text: str):
    await bridge.broadcast({"type": "transcript_partial", "text": text})


async def send_transcript_final(bridge: ControlBridge, text: str):
    await bridge.broadcast({"type": "transcript_final", "text": text})


async def send_assistant_text(bridge: ControlBridge, text: str):
    await bridge.broadcast({"type": "assistant_text", "text": text})


async def send_pipeline_state(bridge: ControlBridge, state: str):
    # state: 'listening' | 'thinking' | 'speaking' | 'interrupted'
    await bridge.broadcast({"type": "pipeline_state", "state": state})


async def send_interruption(bridge: ControlBridge):
    await bridge.broadcast({"type": "interruption"})


async def send_error(bridge: ControlBridge, detail: str):
    await bridge.broadcast({"type": "error", "detail": detail})


async def send_audio_level(bridge: ControlBridge, level: float):
    # Not part of the original Section 8.6 protocol table -- added so the UI
    # can render live mic-input waves. `level` is a single normalized 0..1
    # float, never raw audio (see pipeline/audio_meter.py).
    await bridge.broadcast({"type": "audio_level", "level": level})
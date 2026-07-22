#!/usr/bin/env python3
"""
Raspberry Pi Zero 2W Voice Assistant Client

A lightweight WebSocket client that connects to the voice-assistant-app
running on a laptop. This script is designed to run on the Pi Zero 2W with
minimal dependencies.

Usage:
    python zero2w_client.py ws://LAPTOP_IP:8765

The client performs the following:
  1. Connects to the app's WebSocket server
  2. Sends HELLO with device info
  3. Waits for HELLO_ACK (session config)
  4. Enters a main loop: sends periodic DEVICE_STATUS, handles commands
  5. Reconnects with exponential backoff on disconnection
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import platform
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from audio_capture import AudioCapture, AudioCaptureError, CHUNK_BYTES
from audio_gating import AudioGating, CalibrationPhase, CalibrationStep
from audio_playback import PlaybackError, PlaybackManager
from calibration_prompt import PROMPT_TEXT, get_calibration_prompt_pcm, prompt_asset_status

try:
    import websockets
    from websockets.exceptions import (
        ConnectionClosed,
        ConnectionClosedError,
        InvalidURI,
    )
except ImportError:
    print("ERROR: 'websockets' library is required.")
    print("Install it with: pip install websockets>=15.0")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEVICE_TYPE = "pi_zero_2w"
FIRMWARE_VERSION = "0.1.0"
CAPABILITIES = ["audio_capture", "audio_playback"]
STATUS_INTERVAL_SECONDS = 10
MAX_RECONNECT_ATTEMPTS = 5
MAX_CALIBRATION_RETRIES = 5
MIN_PROMPT_PCM_BYTES = 4000
INITIAL_BACKOFF_SECONDS = 1.0
# Silence enforced after the "say hello to start" prompt finishes playing, so
# the speaker buffer + acoustic tail decay before the mic starts listening.
PROMPT_SETTLE_SEC = 0.6
# SET_VOLUME's 0-100 range maps onto [0, MAX_PLAYBACK_GAIN], not [0, 1.0].
# Loopback echoes the raw, unnormalized mic signal (quiet on this hardware),
# so 100% must be able to boost above unity, not just "no attenuation".
# _apply_gain() hard-clips, so pushing past comfortable volume distorts
# loudly rather than damaging anything.
MAX_PLAYBACK_GAIN = 3.0
# SET_MIC_GAIN's 0-100 range maps onto [0, MAX_INPUT_GAIN]. Measured raw mic
# signal on this hardware peaks around 0.5% of full scale, so meaningful
# gain needs to reach well into the tens, not just up to 1.0. The deployed
# .env default (20.0) sits at 40% of this range, leaving room in both
# directions to tune by ear.
MAX_INPUT_GAIN = 50.0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("zero2w_client")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_start_time = time.monotonic()


def get_device_id() -> str:
    """Use the hostname as a stable device identifier."""
    return socket.gethostname()


def load_device_env() -> None:
    """Load optional .env from the device/ directory (does not override existing env)."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_cpu_temp() -> float | None:
    """Read CPU temperature from the Pi's thermal zone.

    Returns None if the thermal zone file is not available (e.g. on macOS/Linux desktop).
    """
    thermal_path = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        raw = thermal_path.read_text().strip()
        return int(raw) / 1000.0
    except (FileNotFoundError, ValueError, PermissionError):
        return None


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def make_message(msg_type: str, payload: dict | None = None) -> str:
    """Serialize a protocol message to JSON."""
    msg = {
        "type": msg_type,
        "payload": payload or {},
        "timestamp": utc_now_iso(),
    }
    return json.dumps(msg)


def parse_message(raw: str) -> dict:
    """Deserialize a JSON protocol message."""
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Protocol Messages
# ---------------------------------------------------------------------------


def make_hello() -> str:
    """Create a HELLO message with device info."""
    return make_message("HELLO", {
        "device_id": get_device_id(),
        "device_type": DEVICE_TYPE,
        "firmware_version": FIRMWARE_VERSION,
        "capabilities": CAPABILITIES,
    })


def make_device_status(is_recording: bool) -> str:
    """Create a DEVICE_STATUS heartbeat message."""
    return make_message("DEVICE_STATUS", {
        "battery_percent": None,
        "cpu_temp": get_cpu_temp(),
        "is_recording": is_recording,
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
    })


def make_pong(ping_timestamp: str) -> str:
    """Create a PONG response to a PING."""
    return make_message("PONG", {
        "timestamp": ping_timestamp,
    })


def make_audio_frame(
    pcm_bytes: bytes,
    sequence_number: int,
    capture_timestamp: str | None = None,
) -> str:
    """Create an AUDIO_FRAME message with base64-encoded PCM16 audio."""
    capture_ts = capture_timestamp or utc_now_iso()
    return make_message("AUDIO_FRAME", {
        "audio": base64.b64encode(pcm_bytes).decode("ascii"),
        "sequence_number": sequence_number,
        "timestamp": capture_ts,
    })


def make_error(code: str, message: str, recoverable: bool) -> str:
    """Create an ERROR message for device-side failures."""
    return make_message("ERROR", {
        "code": code,
        "message": message,
        "recoverable": recoverable,
    })


def make_playback_complete(sequence_number: int, duration_ms: int) -> str:
    """Notify the app that speaker playback of a final chunk has finished."""
    return make_message("PLAYBACK_COMPLETE", {
        "sequence_number": sequence_number,
        "duration_ms": duration_ms,
    })


def make_calibration_status(phase: str) -> str:
    """Notify the app of the current calibration phase (quiet or speak)."""
    return make_message("CALIBRATION_STATUS", {"phase": phase})


def make_calibration_complete(metrics: dict) -> str:
    """Send calibrated noise/voice levels to the laptop."""
    return make_message("CALIBRATION_COMPLETE", metrics)


# ---------------------------------------------------------------------------
# Client Logic
# ---------------------------------------------------------------------------


class Zero2WClient:
    """WebSocket client that implements the device side of the protocol."""

    def __init__(self, server_url: str):
        self.server_url = server_url
        self.session_id: str | None = None
        self.is_recording = False
        self._running = False
        self._ws = None
        self._audio_capture = AudioCapture()
        self._playback = PlaybackManager()
        self._audio_gating = AudioGating(
            quiet_sec=float(os.getenv("CALIBRATION_QUIET_SEC", "1.0")),
            speak_sec=float(os.getenv("CALIBRATION_SPEAK_SEC", "10.0")),
        )
        self._audio_task: asyncio.Task | None = None
        self._sequence_number = 0
        self._mic_muted = False
        self._calibration_playing_prompt = False
        self._calibration_retries = 0
        self._stream_to_laptop = False

    async def run(self) -> None:
        """Connect to the server and run the main loop.

        Handles reconnection with exponential backoff.
        """
        attempt = 0

        while attempt < MAX_RECONNECT_ATTEMPTS:
            try:
                logger.info(
                    "Connecting to %s (attempt %d/%d)...",
                    self.server_url, attempt + 1, MAX_RECONNECT_ATTEMPTS,
                )
                async with websockets.connect(
                    self.server_url,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    attempt = 0  # Reset on successful connection
                    await self._session(ws)

            except (OSError, ConnectionRefusedError) as e:
                attempt += 1
                if attempt >= MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        "Failed to connect after %d attempts. Giving up.",
                        MAX_RECONNECT_ATTEMPTS,
                    )
                    break
                backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Connection failed (%s). Retrying in %.1fs...", e, backoff,
                )
                await asyncio.sleep(backoff)

            except ConnectionClosed as e:
                attempt += 1
                if attempt >= MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        "Lost connection after %d reconnect attempts. Giving up.",
                        MAX_RECONNECT_ATTEMPTS,
                    )
                    break
                backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Connection closed (%s). Reconnecting in %.1fs...", e, backoff,
                )
                await asyncio.sleep(backoff)

            except InvalidURI as e:
                logger.error("Invalid WebSocket URL: %s", e)
                break

        self._running = False
        logger.info("Client stopped.")

    async def _session(self, ws) -> None:
        """Perform handshake then enter the main loop."""
        # --- Handshake ---
        await self._handshake(ws)

        # --- Main loop ---
        self._running = True
        logger.info("Entering main loop. Sending status every %ds.", STATUS_INTERVAL_SECONDS)

        status_task = asyncio.create_task(self._status_loop(ws))
        try:
            await self._receive_loop(ws)
        finally:
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass
            await self._stop_audio()

    async def _handshake(self, ws) -> None:
        """Send HELLO and wait for HELLO_ACK."""
        hello = make_hello()
        logger.info("Sending HELLO (device_id=%s)", get_device_id())
        await ws.send(hello)

        # Wait for HELLO_ACK (with a timeout)
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Timeout waiting for HELLO_ACK")
            raise ConnectionError("No HELLO_ACK received within 10s")

        msg = parse_message(raw)
        if msg.get("type") != "HELLO_ACK":
            logger.error("Expected HELLO_ACK, got: %s", msg.get("type"))
            raise ConnectionError(f"Unexpected message type: {msg.get('type')}")

        payload = msg.get("payload", {})
        self.session_id = payload.get("session_id")
        audio_config = payload.get("audio_config", {})
        logger.info(
            "Handshake complete! session_id=%s, audio_config=%s",
            self.session_id, audio_config,
        )

    async def _status_loop(self, ws) -> None:
        """Send DEVICE_STATUS messages at a regular interval."""
        while True:
            await asyncio.sleep(STATUS_INTERVAL_SECONDS)
            status = make_device_status(self.is_recording)
            try:
                await ws.send(status)
                logger.debug("Sent DEVICE_STATUS (recording=%s)", self.is_recording)
            except ConnectionClosed:
                break

    async def _receive_loop(self, ws) -> None:
        """Listen for messages from the app and handle commands."""
        async for raw in ws:
            msg = parse_message(raw)
            msg_type = msg.get("type")
            payload = msg.get("payload", {})

            if msg_type == "START_AUDIO_STREAM":
                await self._start_audio(ws, payload)

            elif msg_type == "STOP_AUDIO_STREAM":
                await self._stop_audio()
                logger.info("Audio streaming stopped")

            elif msg_type == "SET_VOLUME":
                volume = payload.get("volume")
                if volume is None:
                    logger.warning("SET_VOLUME received with no volume value")
                else:
                    pct = max(0, min(100, int(volume))) / 100.0
                    gain = pct * MAX_PLAYBACK_GAIN
                    self._playback.playback_gain = gain
                    logger.info("Volume set to %s%% (playback_gain=%.2f)", volume, gain)

            elif msg_type == "SET_MIC_GAIN":
                mic_gain = payload.get("gain")
                if mic_gain is None:
                    logger.warning("SET_MIC_GAIN received with no gain value")
                else:
                    pct = max(0, min(100, int(mic_gain))) / 100.0
                    gain = pct * MAX_INPUT_GAIN
                    self._audio_capture.input_gain = gain
                    logger.info("Mic gain set to %s%% (input_gain=%.2f)", mic_gain, gain)

            elif msg_type == "PLAY_AUDIO":
                await self._handle_play_audio(ws, payload)

            elif msg_type == "MUTE_MIC":
                self._mic_muted = True
                logger.info("Mic muted (AI speaking)")

            elif msg_type == "UNMUTE_MIC":
                self._mic_muted = False
                self._stream_to_laptop = True
                logger.info("Mic unmuted — streaming to laptop enabled")

            elif msg_type == "SHUTDOWN_DEVICE":
                logger.info("Shutdown requested by app. Disconnecting...")
                await self._stop_audio()
                self._running = False
                await ws.close()
                return

            elif msg_type == "PING":
                ping_ts = payload.get("timestamp", utc_now_iso())
                pong = make_pong(ping_ts)
                await ws.send(pong)
                logger.debug("Responded to PING with PONG")

            else:
                logger.warning("Unknown message type: %s", msg_type)

    async def _start_audio(self, ws, payload: dict | None = None) -> None:
        """Start microphone capture and stream AUDIO_FRAME messages.

        When the app sends ``skip_calibration: true`` (a resume of a paused
        session) the device must NOT re-run calibration — no prompt, no
        re-measuring — and should begin streaming live audio immediately.
        """
        if self.is_recording:
            logger.debug("Audio stream already active")
            return

        skip_calibration = bool((payload or {}).get("skip_calibration", False))

        try:
            await self._audio_capture.start()
        except AudioCaptureError as exc:
            logger.error("Failed to start audio capture: %s", exc)
            await ws.send(make_error("MIC_UNAVAILABLE", str(exc), recoverable=False))
            return

        self.is_recording = True
        self._sequence_number = 0
        self._calibration_retries = 0
        self._mic_muted = False

        if skip_calibration:
            # Resume: levels are already known on the app side. Stream live
            # audio straight away instead of replaying "say hello to start".
            self._stream_to_laptop = True
            logger.info("Audio streaming resumed (skip_calibration) — no prompt")
        else:
            self._stream_to_laptop = False
            self._audio_gating.start_calibration()
            await ws.send(make_calibration_status("quiet"))
            logger.info("Audio streaming started with calibration (%d-byte chunks)", CHUNK_BYTES)

        self._audio_task = asyncio.create_task(self._audio_stream_loop(ws))

    async def _stop_audio(self) -> None:
        """Stop capture task and terminate arecord."""
        self.is_recording = False

        if self._audio_task is not None:
            self._audio_task.cancel()
            try:
                await self._audio_task
            except asyncio.CancelledError:
                pass
            self._audio_task = None

        await self._audio_capture.stop()
        await self._playback.stop()

    async def _audio_stream_loop(self, ws) -> None:
        """Read PCM chunks from arecord and send AUDIO_FRAME messages."""
        try:
            while self.is_recording:
                chunk = await self._audio_capture.read_chunk()
                if chunk is None:
                    if self.is_recording:
                        logger.warning("Audio capture ended unexpectedly")
                        await ws.send(make_error(
                            "MIC_ERROR",
                            "Microphone capture stopped unexpectedly",
                            recoverable=True,
                        ))
                    break

                if self._mic_muted:
                    continue

                if self._audio_gating.is_calibrating:
                    if self._calibration_playing_prompt:
                        continue

                    step = self._audio_gating.process_calibration_chunk(chunk)
                    if step == CalibrationStep.PLAY_PROMPT:
                        if not await self._play_calibration_prompt(ws):
                            await self._fail_calibration(
                                ws,
                                "SPEAKER_ERROR",
                                "Could not play calibration prompt on speaker",
                            )
                    elif step == CalibrationStep.SPEECH_TIMEOUT:
                        await self._retry_calibration_prompt(ws)
                    elif step == CalibrationStep.COMPLETE:
                        # Report levels only. We deliberately do NOT forward the
                        # captured hello audio: the app greets first and then
                        # listens live, so replaying calibration audio would just
                        # inject the prompt tail / a stale "hello" into the chat.
                        metrics = self._audio_gating.calibration_payload()
                        await ws.send(make_calibration_complete(metrics))
                        logger.info(
                            "Calibration complete — noise=%.0f voice=%.0f",
                            metrics.get("noise_floor", 0.0),
                            metrics.get("user_speech_peak", 0.0),
                        )
                    continue

                if not self._stream_to_laptop or self._mic_muted:
                    continue

                # Stream continuous PCM including silence — OpenAI server VAD needs
                # quiet periods after speech to detect end-of-turn.
                self._sequence_number += 1
                capture_ts = utc_now_iso()
                frame = make_audio_frame(chunk, self._sequence_number, capture_ts)
                await ws.send(frame)
                logger.debug("Sent AUDIO_FRAME #%d (%d bytes)", self._sequence_number, len(chunk))
        except ConnectionClosed:
            logger.debug("WebSocket closed during audio streaming")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Audio stream loop error: %s", exc)
            try:
                await ws.send(make_error("MIC_ERROR", str(exc), recoverable=True))
            except ConnectionClosed:
                pass

    async def _play_calibration_prompt(self, ws) -> bool:
        """Play 'Say hello to start' on the Pi speaker, then listen for hello."""
        self._calibration_playing_prompt = True
        await ws.send(make_calibration_status("prompt"))
        # Nothing else reads the mic while this coroutine is running (the
        # audio loop that calls read_chunk() is blocked awaiting us). Keep
        # draining arecord's stdout concurrently so its pipe never backs up
        # into an overrun on the shared capture/playback codec -- see
        # AudioCapture.drain_continuously() for the full mechanism.
        drain_task = asyncio.create_task(self._audio_capture.drain_continuously())
        try:
            pcm = await get_calibration_prompt_pcm()
            if len(pcm) < MIN_PROMPT_PCM_BYTES:
                raise PlaybackError(
                    f"Calibration prompt audio too short ({len(pcm)} bytes)",
                )

            device = self._playback.device or "(default ALSA device)"
            logger.info(
                "Playing calibration prompt: %d bytes (~%.1fs) on %s",
                len(pcm),
                len(pcm) / (24000 * 2),
                device,
            )
            await self._playback.play_pcm16_chunk(pcm, is_final=True)
            logger.info("Calibration prompt finished")
            # Let the speaker's ALSA buffer and the room's acoustic tail fully
            # decay before we start listening. Without this the mic captures
            # the prompt's own "...to start" tail and treats it as the user's
            # hello — calibration then "completes" even in total silence.
            await asyncio.sleep(PROMPT_SETTLE_SEC)
        except (PlaybackError, RuntimeError, OSError) as exc:
            logger.error("Calibration prompt playback failed: %s", exc)
            await ws.send(make_error("SPEAKER_ERROR", str(exc), recoverable=True))
            return False
        finally:
            self._calibration_playing_prompt = False
            drain_task.cancel()
            try:
                await drain_task
            except asyncio.CancelledError:
                pass

        # The mic kept recording (including the prompt's own echo) the entire
        # time we were blocked playing it. Drop that backlog so the speak phase
        # only evaluates fresh, real-time audio — otherwise the buffered echo is
        # replayed in a burst and instantly "completes" calibration on silence.
        discarded = await self._audio_capture.drain_buffered_audio()
        if discarded:
            logger.info(
                "Discarded %d bytes (~%.1fs) of buffered mic audio before listening",
                discarded,
                discarded / (24000 * 2),
            )

        await ws.send(make_calibration_status("speak"))
        self._audio_gating.begin_speak_phase()
        return True

    async def _retry_calibration_prompt(self, ws) -> None:
        """Replay the prompt when the user did not say hello in time."""
        self._calibration_retries += 1
        if self._calibration_retries >= MAX_CALIBRATION_RETRIES:
            await self._fail_calibration(
                ws,
                "CALIBRATION_FAILED",
                f'No speech detected after {MAX_CALIBRATION_RETRIES} attempts. '
                f'Wait for "{PROMPT_TEXT}" then say hello.',
            )
            return

        logger.info(
            "Calibration retry %d/%d — replaying prompt",
            self._calibration_retries,
            MAX_CALIBRATION_RETRIES,
        )
        await ws.send(make_calibration_status("retry"))
        self._audio_gating.reset_for_prompt_retry()
        if not await self._play_calibration_prompt(ws):
            await self._fail_calibration(
                ws,
                "SPEAKER_ERROR",
                "Could not replay calibration prompt on speaker",
            )

    async def _fail_calibration(self, ws, code: str, message: str) -> None:
        """Abort calibration and stop the audio stream."""
        logger.error("Calibration failed: %s", message)
        self._audio_gating.cancel_calibration()
        await ws.send(make_error(code, message, recoverable=True))
        await self._stop_audio()

    async def _handle_play_audio(self, ws, payload: dict) -> None:
        """Decode PLAY_AUDIO payload and pipe PCM to aplay."""
        seq = payload.get("sequence_number")
        audio_b64 = payload.get("audio", "")
        is_final = payload.get("is_final", False)

        try:
            pcm_bytes = base64.b64decode(audio_b64)

            if is_final and self._playback.is_streaming:
                if pcm_bytes:
                    await self._playback.play_pcm16_chunk(pcm_bytes, is_final=False)
                duration_sec = await self._playback.finalize_streaming()
            elif is_final:
                if not pcm_bytes:
                    logger.warning(
                        "PLAY_AUDIO is_final=True with empty audio and no active stream",
                    )
                    duration_sec = 0.0
                else:
                    duration_sec = await self._playback.play_pcm16_chunk(
                        pcm_bytes,
                        is_final=True,
                    )
            else:
                duration_sec = await self._playback.play_pcm16_chunk(
                    pcm_bytes,
                    is_final=False,
                )

            logger.debug(
                "Played PLAY_AUDIO frame #%s (%d bytes, final=%s)",
                seq, len(pcm_bytes), is_final,
            )

            if is_final:
                recovery_sec = 0.3
                await asyncio.sleep(recovery_sec)
                duration_ms = int((duration_sec + recovery_sec) * 1000)
                await ws.send(make_playback_complete(seq or 0, duration_ms))
                logger.info(
                    "Sent PLAYBACK_COMPLETE seq=%s duration_ms=%d",
                    seq, duration_ms,
                )
        except PlaybackError as exc:
            logger.error("Playback error: %s", exc)
            await ws.send(make_error("SPEAKER_ERROR", str(exc), recoverable=True))
        except Exception as exc:
            logger.error("Failed to play audio frame #%s: %s", seq, exc)
            await ws.send(make_error("AUDIO_FORMAT_ERROR", str(exc), recoverable=True))

    async def shutdown(self) -> None:
        """Gracefully close the connection."""
        self._running = False
        await self._stop_audio()
        if self._ws:
            await self._ws.close()
            logger.info("WebSocket connection closed.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Pi Zero 2W Voice Assistant Client",
        epilog="Example: python zero2w_client.py ws://192.168.1.100:8765",
    )
    parser.add_argument(
        "server_url",
        help="WebSocket URL of the voice-assistant-app (e.g. ws://LAPTOP_IP:8765)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug-level logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    load_device_env()

    logger.info("Pi Zero 2W Voice Assistant Client v%s", FIRMWARE_VERSION)
    logger.info("Device ID: %s | Platform: %s", get_device_id(), platform.platform())
    logger.info("Target server: %s", args.server_url)
    logger.info(
        "Audio devices: input=%s output=%s",
        os.environ.get("AUDIO_INPUT_DEVICE", "(default)"),
        os.environ.get("AUDIO_OUTPUT_DEVICE", "(default)"),
    )
    logger.info("Calibration prompt asset: %s", prompt_asset_status())

    client = Zero2WClient(args.server_url)

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()

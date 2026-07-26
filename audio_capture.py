"""Microphone capture via arecord subprocess (24 kHz PCM16, mono left channel).

Neither the channel selection nor the gain happens here. Both are ALSA's
work now -- a `route` plugin picks the mic's channel out of the I2S frame and
a `softvol` control applies the gain, so this module only moves bytes. See
`config/asoundrc.softvol` and `alsa_mixer`.
"""

from __future__ import annotations

import asyncio
import logging
import os

from alsa_mixer import SoftvolControl, mixer_card

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
FORMAT = "S16_LE"
BYTES_PER_SAMPLE = 2

# The I2S hardware (ICS-43434-class mic) is genuinely 2-channel: the mic
# occupies the left slot and the right slot is silent by design. Requesting
# `-c 1` from `plughw:` lets the plug plugin downmix by averaging left+right,
# which halves the already-quiet mic signal. The `route` plugin in
# AUDIO_INPUT_DEVICE selects the left slot instead, so arecord hands us mono
# with no loss -- and half the bytes, since the silent slot never crosses the
# pipe.
CAPTURE_CHANNELS = 1

CHUNK_BYTES = 4800  # 100 ms at 24 kHz mono S16_LE -- the size callers expect

DEFAULT_INPUT_GAIN = 1.0

# The mic's raw signal is very quiet by hardware design, so this control
# boosts rather than attenuates: +34 dB is a gain of ~50, matching the top of
# the client's mic slider. See `config/asoundrc.softvol`; these must agree.
#
# One behaviour change comes with it. The Python path soft-limited peaks past
# 85% of full scale; ALSA saturates at the ceiling instead. Measured on this
# hardware, that only ever mattered for the transient in arecord's first
# chunk (raw peak ~1755, i.e. past the ceiling once multiplied): steady-state
# room noise sits near 80-100 raw and speech well under that ceiling, so the
# knee never engaged in normal use. Calibration already ignores that first
# chunk -- `_observe_quiet` only lowers the noise floor toward *quiet* chunks.
CAPTURE_MIN_DB = -51.0
CAPTURE_MAX_DB = 34.0
CAPTURE_STEPS = 255  # index range 0..CAPTURE_STEPS (`resolution 256`)
DEFAULT_MIC_MIXER_CONTROL = "Mic Capture Volume"
DEFAULT_MIC_MIXER_PRIME_DEVICE = "mic_prime"


class AudioCaptureError(Exception):
    """Raised when capture cannot start or fails unexpectedly."""


class AudioCapture:
    """Capture raw PCM16 audio using an arecord subprocess.

    Reads mono chunks of ``CHUNK_BYTES``: the mic's channel is selected and
    its gain applied inside ALSA, upstream of arecord.
    """

    def __init__(self, device: str | None = None, *, input_gain: float | None = None):
        self._device = device if device is not None else os.environ.get("AUDIO_INPUT_DEVICE")
        if input_gain is None:
            input_gain = float(os.environ.get("INPUT_GAIN", DEFAULT_INPUT_GAIN))
        self._input_gain = input_gain
        self._gain_control = SoftvolControl(
            card=os.environ.get("AUDIO_MIC_MIXER_CARD") or mixer_card(),
            control=os.environ.get("AUDIO_MIC_MIXER_CONTROL", DEFAULT_MIC_MIXER_CONTROL),
            prime_device=os.environ.get(
                "AUDIO_MIC_MIXER_PRIME_DEVICE", DEFAULT_MIC_MIXER_PRIME_DEVICE,
            ),
            min_db=CAPTURE_MIN_DB,
            max_db=CAPTURE_MAX_DB,
            steps=CAPTURE_STEPS,
            capture=True,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None

    @property
    def input_gain(self) -> float:
        """The gain last handed to ALSA (or the .env startup value).

        Kept so the client can report the device's level to the app in HELLO;
        the audio itself is scaled by softvol, not by this number.
        """
        return self._input_gain

    async def apply_input_gain(self, gain: float) -> bool:
        """Set the ALSA mic gain (e.g. from SET_MIC_GAIN).

        Takes effect on audio not yet captured; arecord does not restart.
        """
        self._input_gain = gain
        return await self._gain_control.apply_gain(gain)

    def _build_command(self) -> list[str]:
        cmd = [
            "arecord",
            "-f", FORMAT,
            "-r", str(SAMPLE_RATE),
            "-c", str(CAPTURE_CHANNELS),
            "-t", "raw",
        ]
        if self._device:
            cmd.extend(["-D", self._device])
        return cmd

    async def start(self) -> None:
        """Start the arecord subprocess."""
        if self.is_running:
            return

        cmd = self._build_command()
        logger.info("Starting audio capture: %s", " ".join(cmd))

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AudioCaptureError("arecord not found; install alsa-utils") from exc

        if self._process.returncode is not None:
            stderr = await self._read_stderr()
            raise AudioCaptureError(f"arecord failed to start: {stderr}")

        self._running = True

    async def stop(self) -> None:
        """Stop capture and terminate the arecord subprocess."""
        self._running = False
        process = self._process
        self._process = None

        if process is None:
            return

        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        stderr = await self._read_process_stderr(process)
        if stderr:
            logger.debug("arecord stderr on stop: %s", stderr.strip())

    async def read_chunk(self) -> bytes | None:
        """Read one fixed-size mono chunk, already channel-selected and gained."""
        if not self.is_running or self._process is None or self._process.stdout is None:
            return None

        if self._process.returncode is not None:
            self._running = False
            return None

        try:
            return await self._process.stdout.readexactly(CHUNK_BYTES)
        except asyncio.IncompleteReadError:
            self._running = False
            return None

    async def drain_continuously(self) -> None:
        """Keep consuming arecord's stdout until cancelled; discards everything.

        Nothing else reads the mic while the calibration prompt plays through
        the speaker (the audio loop is blocked awaiting playback), but arecord
        keeps capturing regardless. Left undrained, its stdout pipe fills and
        arecord's writes block, which stops it pulling data off the hardware's
        capture ring buffer -- causing an overrun (XRUN) there. On this shared
        capture/playback I2S codec, that overrun can wedge the concurrent
        aplay stream too ("aplay pipe broken during final playback").

        Run this as a background task for the duration of anything that would
        otherwise leave the mic unread (e.g. prompt playback); cancel it
        immediately after. `drain_buffered_audio()` remains as a fast, final
        sweep for the brief gap between cancellation and actually stopping.
        """
        if not self.is_running or self._process is None or self._process.stdout is None:
            return
        try:
            while True:
                data = await self._process.stdout.read(CHUNK_BYTES)
                if not data:
                    return
        except asyncio.CancelledError:
            raise

    async def drain_buffered_audio(self, max_drain_sec: float = 1.5) -> int:
        """Discard audio that piled up while nobody was reading the mic.

        ``arecord`` keeps capturing into its pipe even while the audio loop is
        busy elsewhere — notably while the calibration prompt plays through the
        speaker. That backlog contains the prompt's own acoustic echo, and if it
        were fed to the calibrator it would be replayed in a burst and mistaken
        for the user's hello. Read and throw it away until reads start blocking,
        i.e. we've caught up to real time. Returns the number of bytes dropped.
        """
        if not self.is_running or self._process is None or self._process.stdout is None:
            return 0

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_drain_sec
        discarded = 0
        while loop.time() < deadline:
            try:
                data = await asyncio.wait_for(
                    self._process.stdout.read(CHUNK_BYTES),
                    timeout=0.05,
                )
            except asyncio.TimeoutError:
                # No data within 50 ms → the backlog is gone and fresh audio is
                # now arriving at real-time cadence. We're caught up.
                break
            if not data:
                break
            discarded += len(data)
        return discarded

    async def _read_stderr(self) -> str:
        if self._process is None or self._process.stderr is None:
            return ""
        data = await self._process.stderr.read()
        return data.decode("utf-8", errors="replace")

    async def _read_process_stderr(self, process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        data = await process.stderr.read()
        return data.decode("utf-8", errors="replace")

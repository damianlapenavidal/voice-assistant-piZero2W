"""Speaker playback via aplay subprocess (24 kHz PCM16 mono).

Volume is *not* applied here. It is applied by ALSA, in the `softvol` plugin
this module drives with `amixer` -- see `apply_playback_gain`.
"""

from __future__ import annotations

import asyncio
import logging
import os

from alsa_mixer import SoftvolControl, mixer_card

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
BYTE_RATE = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
FORMAT = "S16_LE"
BUFFER_US = 300000

DEFAULT_PLAYBACK_GAIN = 1.0

# --- ALSA volume ----------------------------------------------------------
#
# Volume used to be a per-sample multiply in Python. Measured on the device,
# that cost ~10% of one core for as long as the assistant was speaking (see
# docs/battery-plan.md) -- pure interpreter overhead, since the same
# arithmetic in C is free. ALSA's `softvol` plugin does it in the playback
# chain instead, leaving this module to move bytes and nothing else.
#
# It is also strictly more responsive. softvol sits *downstream* of aplay's
# buffer, so a volume change affects audio already handed to aplay. The
# Python path could only ever scale samples not yet written, which is why it
# needed sub-chunked, wall-clock-paced writes to feel live at all; none of
# that machinery is necessary now, and it is gone.
#
# The plugin, the control it creates and the range below are defined in
# `config/asoundrc.softvol`, installed as `~/.asoundrc` on the device. These
# constants must match that file.
DEFAULT_MIXER_CONTROL = "PCM Playback Volume"
DEFAULT_MIXER_PRIME_DEVICE = "softvol_prime"
# 0 dB at the top makes playback attenuation-only in the hardware rather than
# by convention: softvol cannot boost, so nothing can drive an already
# normalized source past full scale. That is why the soft-knee limiter this
# path used to need is gone.
PLAYBACK_MIN_DB = -51.0
PLAYBACK_MAX_DB = 0.0
PLAYBACK_STEPS = 100  # index range 0..PLAYBACK_STEPS (`resolution 101`)


class PlaybackError(Exception):
    """Raised when playback cannot start or fails unexpectedly."""


class PlaybackManager:
    """Play raw PCM16 audio by piping chunks to an aplay subprocess."""

    def __init__(self, device: str | None = None, *, playback_gain: float | None = None):
        self._device = device if device is not None else os.environ.get("AUDIO_OUTPUT_DEVICE")
        if playback_gain is None:
            playback_gain = float(os.environ.get("PLAYBACK_GAIN", DEFAULT_PLAYBACK_GAIN))
        self._playback_gain = playback_gain
        self._volume = SoftvolControl(
            card=mixer_card(),
            control=os.environ.get("AUDIO_MIXER_CONTROL", DEFAULT_MIXER_CONTROL),
            prime_device=os.environ.get(
                "AUDIO_MIXER_PRIME_DEVICE", DEFAULT_MIXER_PRIME_DEVICE,
            ),
            min_db=PLAYBACK_MIN_DB,
            max_db=PLAYBACK_MAX_DB,
            steps=PLAYBACK_STEPS,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._streamed_bytes = 0

    @property
    def device(self) -> str | None:
        return self._device

    @property
    def playback_gain(self) -> float:
        """The gain last handed to ALSA (or the .env startup value).

        Kept only so the client can report the device's level to the app in
        HELLO; the audio itself is scaled by softvol, not by this number.
        """
        return self._playback_gain

    @property
    def is_streaming(self) -> bool:
        """True while a long-lived aplay process is receiving streamed chunks."""
        return self._process is not None and self._process.returncode is None

    async def apply_playback_gain(self, gain: float) -> bool:
        """Set the ALSA volume to `gain` (linear amplitude, 0.0-1.0).

        Takes effect immediately, including on audio already buffered inside
        aplay -- nothing needs to restart and nothing waits for the current
        response to drain.

        Returns whether ALSA accepted it; see `SoftvolControl.apply_gain` for
        why a failure is not fatal.
        """
        self._playback_gain = gain
        return await self._volume.apply_gain(gain)

    def _build_command(self, *, quiet: bool = False) -> list[str]:
        cmd = [
            "aplay",
            "-f", FORMAT,
            "-r", str(SAMPLE_RATE),
            "-c", str(CHANNELS),
            "-t", "raw",
            f"--buffer-time={BUFFER_US}",
        ]
        if quiet:
            cmd.append("-q")
        if self._device:
            cmd.extend(["-D", self._device])
        return cmd

    async def _ensure_process(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return

        self._streamed_bytes = 0
        cmd = self._build_command()
        logger.info("Starting audio playback: %s", " ".join(cmd))

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise PlaybackError("aplay not found; install alsa-utils") from exc

        if self._process.returncode is not None:
            stderr = await self._read_stderr()
            raise PlaybackError(f"aplay failed to start: {stderr}")

    async def play_pcm16_chunk(
        self,
        pcm_bytes: bytes,
        *,
        is_final: bool = False,
    ) -> float:
        """Write PCM16 audio to aplay.

        For streaming (non-final) chunks, pipes to a long-lived aplay process.
        For final whole-response chunks, starts a dedicated aplay, drains fully,
        and returns the playback duration in seconds.
        """
        if not pcm_bytes and not is_final:
            return 0.0

        # These bytes reach aplay exactly as they arrived -- volume is ALSA's
        # job now -- so the duration is simply their length.
        duration_sec = len(pcm_bytes) / BYTE_RATE

        if is_final:
            await self._play_final(pcm_bytes)
            return duration_sec

        if not pcm_bytes:
            return 0.0

        await self._ensure_process()
        if self._process is None or self._process.stdin is None:
            raise PlaybackError("aplay stdin is not available")

        if self._process.returncode is not None:
            await self._ensure_process()
            if self._process is None or self._process.stdin is None:
                raise PlaybackError("aplay process is not running")

        self._process.stdin.write(pcm_bytes)
        await self._process.stdin.drain()
        self._streamed_bytes += len(pcm_bytes)
        return duration_sec

    async def finalize_streaming(self) -> float:
        """Close streaming stdin and wait for aplay to finish.

        Returns total playback duration in seconds for all streamed bytes.
        """
        if self._process is None:
            return 0.0

        process = self._process
        duration_sec = self._streamed_bytes / BYTE_RATE
        self._process = None
        self._streamed_bytes = 0

        stderr_data = b""
        try:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
                await process.stdin.wait_closed()

            _, stderr_data = await process.communicate()
        except (BrokenPipeError, ConnectionResetError):
            if process.returncode is None:
                process.terminate()
                await process.communicate()
            raise PlaybackError("aplay pipe broken during streaming finalize") from None

        if process.returncode not in (0, None, -15):
            detail = stderr_data.decode("utf-8", errors="replace").strip()
            raise PlaybackError(
                f"aplay exited with code {process.returncode}"
                + (f": {detail}" if detail else ""),
            )

        return duration_sec

    async def _play_final(self, pcm_bytes: bytes) -> None:
        """Play one complete response: feed sub-chunks, close stdin, wait for drain."""
        await self.stop()

        cmd = self._build_command(quiet=True)
        logger.info("Starting final playback (%d bytes): %s", len(pcm_bytes), " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise PlaybackError("aplay not found; install alsa-utils") from exc

        stderr_data = b""
        try:
            if pcm_bytes and process.stdin is not None:
                process.stdin.write(pcm_bytes)
                await process.stdin.drain()

            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
                await process.stdin.wait_closed()

            _, stderr_data = await process.communicate()
        except (BrokenPipeError, ConnectionResetError):
            if process.returncode is None:
                process.terminate()
                await process.communicate()
            raise PlaybackError("aplay pipe broken during final playback") from None

        if process.returncode not in (0, None, -15):
            detail = stderr_data.decode("utf-8", errors="replace").strip()
            raise PlaybackError(
                f"aplay exited with code {process.returncode}"
                + (f": {detail}" if detail else ""),
            )

    async def stop(self) -> None:
        """Close aplay stdin and terminate the subprocess."""
        process = self._process
        self._process = None
        self._streamed_bytes = 0

        if process is None:
            return

        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            await process.stdin.wait_closed()

        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        if process.stderr is not None:
            stderr = await process.stderr.read()
            if stderr:
                logger.debug(
                    "aplay stderr on stop: %s",
                    stderr.decode("utf-8", errors="replace").strip(),
                )

    async def _read_stderr(self) -> str:
        if self._process is None or self._process.stderr is None:
            return ""
        data = await self._process.stderr.read()
        return data.decode("utf-8", errors="replace")

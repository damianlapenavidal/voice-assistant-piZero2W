"""Speaker playback via aplay subprocess (24 kHz PCM16 mono)."""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
BYTE_RATE = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
FORMAT = "S16_LE"
BUFFER_US = 300000
WRITE_CHUNK_BYTES = 4800  # 100 ms sub-chunks for stdin writes


class PlaybackError(Exception):
    """Raised when playback cannot start or fails unexpectedly."""


class PlaybackManager:
    """Play raw PCM16 audio by piping chunks to an aplay subprocess."""

    def __init__(self, device: str | None = None):
        self._device = device if device is not None else os.environ.get("AUDIO_OUTPUT_DEVICE")
        self._process: asyncio.subprocess.Process | None = None
        self._streamed_bytes = 0

    @property
    def device(self) -> str | None:
        return self._device

    @property
    def is_streaming(self) -> bool:
        """True while a long-lived aplay process is receiving streamed chunks."""
        return self._process is not None and self._process.returncode is None

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
                for offset in range(0, len(pcm_bytes), WRITE_CHUNK_BYTES):
                    part = pcm_bytes[offset:offset + WRITE_CHUNK_BYTES]
                    process.stdin.write(part)
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

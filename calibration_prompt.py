"""Load or synthesize the calibration voice prompt for the Pi speaker."""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPT_TEXT = "Say hello to start"
ASSET_PATH = Path(__file__).resolve().parent / "assets" / "say_hello_prompt.pcm"
TARGET_RATE = 24000
MIN_PROMPT_PCM_BYTES = 4000


def _wav_to_pcm16_mono_24k(wav_bytes: bytes) -> bytes:
    import audioop

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        sample_width = wf.getsampwidth()
        channels = wf.getnchannels()
        rate = wf.getframerate()

    if channels == 2:
        frames = audioop.tomono(frames, sample_width, 0.5, 0.5)
        channels = 1
    if rate != TARGET_RATE:
        frames, _ = audioop.ratecv(frames, sample_width, channels, rate, TARGET_RATE, None)
    return frames


async def _synthesize_with_espeak(text: str) -> bytes:
    for binary in ("espeak-ng", "espeak"):
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "-v",
                "en-us+f3" if binary == "espeak-ng" else "en",
                "-s",
                "100",
                "-w",
                "/dev/stdout",
                text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            continue

        wav_bytes, _ = await process.communicate()
        if process.returncode == 0 and wav_bytes:
            pcm = _wav_to_pcm16_mono_24k(wav_bytes)
            if pcm:
                logger.info("Synthesized calibration prompt with %s", binary)
                return pcm

    raise RuntimeError(
        "Calibration prompt unavailable: install espeak-ng or ship assets/say_hello_prompt.pcm",
    )


async def get_calibration_prompt_pcm() -> bytes:
    """Return 24 kHz PCM16 mono audio for the calibration prompt."""
    if ASSET_PATH.is_file() and ASSET_PATH.stat().st_size >= MIN_PROMPT_PCM_BYTES:
        return ASSET_PATH.read_bytes()
    return await _synthesize_with_espeak(PROMPT_TEXT)


def prompt_asset_status() -> str:
    """Human-readable status of the bundled prompt asset."""
    if not ASSET_PATH.is_file():
        return f"missing ({ASSET_PATH.name}); will try espeak-ng"
    size = ASSET_PATH.stat().st_size
    if size < MIN_PROMPT_PCM_BYTES:
        return f"too small ({size} bytes); will try espeak-ng"
    return f"ok ({size} bytes at {ASSET_PATH})"

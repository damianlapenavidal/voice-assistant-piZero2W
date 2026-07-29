"""Load the calibration voice prompt for the Pi speaker.

The prompt is a bundled PCM asset, played from local flash. There is no
synthesis fallback: the one this module used to carry could not run on this
device under any circumstances -- it imported `audioop`, removed in Python
3.13 (the device runs 3.13.5), and neither `espeak-ng` nor `espeak` is
installed. A missing asset would therefore have raised ImportError from
inside the fallback rather than the clean error the fallback existed to
avoid. Failing directly is both honest and easier to act on.

Keeping the asset on the device is also the cheap option: 65 KB from flash
costs no radio time, where fetching it from the app would add a transfer per
session.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPT_TEXT = "Say hello to start"
ASSET_PATH = Path(__file__).resolve().parent / "assets" / "say_hello_prompt.pcm"
TARGET_RATE = 24000
MIN_PROMPT_PCM_BYTES = 4000


class CalibrationPromptError(Exception):
    """Raised when the calibration prompt asset is missing or unusable."""


async def get_calibration_prompt_pcm() -> bytes:
    """Return 24 kHz PCM16 mono audio for the calibration prompt."""
    if not ASSET_PATH.is_file():
        raise CalibrationPromptError(
            f"Calibration prompt asset missing: {ASSET_PATH}. "
            "Restore it from the repo (git checkout -- assets/).",
        )

    size = ASSET_PATH.stat().st_size
    if size < MIN_PROMPT_PCM_BYTES:
        raise CalibrationPromptError(
            f"Calibration prompt asset truncated: {ASSET_PATH} is {size} bytes, "
            f"expected at least {MIN_PROMPT_PCM_BYTES}. "
            "Restore it from the repo (git checkout -- assets/).",
        )

    return ASSET_PATH.read_bytes()


def prompt_asset_ok() -> bool:
    """Whether the asset is present and large enough to be the real prompt."""
    return ASSET_PATH.is_file() and ASSET_PATH.stat().st_size >= MIN_PROMPT_PCM_BYTES


def prompt_asset_status() -> str:
    """Human-readable status of the bundled prompt asset."""
    if not ASSET_PATH.is_file():
        return f"MISSING ({ASSET_PATH}) — calibration will fail"
    size = ASSET_PATH.stat().st_size
    if size < MIN_PROMPT_PCM_BYTES:
        return f"TRUNCATED ({size} bytes, need >= {MIN_PROMPT_PCM_BYTES}) — calibration will fail"
    return f"ok ({size} bytes at {ASSET_PATH})"

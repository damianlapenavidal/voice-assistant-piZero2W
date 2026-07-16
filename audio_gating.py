"""RMS-based mic calibration with quiet-then-speak phases (Pi Zero 2W pattern)."""

from __future__ import annotations

import logging
import struct
from enum import Enum

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
CHUNK_MS = 100
CHUNK_SEC = CHUNK_MS / 1000
CHUNK_BYTES = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * CHUNK_MS // 1000

DEFAULT_QUIET_SEC = 1.0
DEFAULT_SPEAK_SEC = 10.0
SPEAK_SILENCE_CHUNKS_TO_FINISH = 5
# Chunks (100 ms each) of mic audio discarded at the very start of the speak
# phase. Combined with the client's post-prompt settle delay, this keeps the
# prompt's speaker tail from being mistaken for the user's hello.
POST_PROMPT_GRACE_CHUNKS = 8
MIN_SPEECH_CHUNKS = 2
MIN_VOICE_ABOVE_NOISE = 50.0


class CalibrationPhase(str, Enum):
    QUIET = "quiet"
    WAITING_SPEAK = "waiting_speak"
    SPEAK = "speak"


class CalibrationStep(str, Enum):
    """Signals returned to the device client during calibration."""

    PLAY_PROMPT = "play_prompt"
    COMPLETE = "complete"
    SPEECH_TIMEOUT = "speech_timeout"


def chunk_rms(chunk: bytes) -> float:
    """Root-mean-square level for a PCM16 little-endian chunk."""
    if len(chunk) < BYTES_PER_SAMPLE:
        return 0.0
    count = len(chunk) // BYTES_PER_SAMPLE
    samples = struct.unpack(f"<{count}h", chunk[: count * BYTES_PER_SAMPLE])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


class AudioGating:
    """Two-phase calibration: measure quiet noise, then sample the user's voice."""

    def __init__(
        self,
        *,
        quiet_sec: float = DEFAULT_QUIET_SEC,
        speak_sec: float = DEFAULT_SPEAK_SEC,
    ) -> None:
        self.noise_floor = 400.0
        self.user_speech_peak = 0.0
        self.speech_margin = 350.0
        self._quiet_sec = quiet_sec
        self._speak_sec = speak_sec
        self._calibrating = False
        self._phase = CalibrationPhase.QUIET
        self._chunks_left = 0
        self._calibration_samples: list[float] = []
        self._calibration_speech_seen = False
        self._calibration_speech_streak = 0
        self._calibration_silence_streak = 0
        self._post_prompt_grace_chunks = 0
        self._calibration_peak_level = 0.0

    @property
    def is_waiting_for_prompt(self) -> bool:
        return self._calibrating and self._phase == CalibrationPhase.WAITING_SPEAK

    @property
    def is_calibrating(self) -> bool:
        return self._calibrating

    @property
    def calibration_phase(self) -> CalibrationPhase | None:
        return self._phase if self._calibrating else None

    def start_calibration(
        self,
        *,
        quiet_sec: float | None = None,
        speak_sec: float | None = None,
    ) -> None:
        """Begin quiet-then-speak calibration; audio is not streamed until done."""
        if quiet_sec is not None:
            self._quiet_sec = quiet_sec
        if speak_sec is not None:
            self._speak_sec = speak_sec

        self._calibrating = True
        self._phase = CalibrationPhase.QUIET
        self._chunks_left = max(1, int(self._quiet_sec / CHUNK_SEC))
        self._calibration_samples = []
        self._calibration_speech_seen = False
        self._calibration_speech_streak = 0
        self._calibration_silence_streak = 0
        self._post_prompt_grace_chunks = 0
        self._calibration_peak_level = 0.0
        logger.info(
            "Audio calibration: stay quiet for %.1fs (%d chunks)",
            self._quiet_sec,
            self._chunks_left,
        )

    def reset_for_prompt_retry(self) -> None:
        """Return to waiting for the speaker prompt after a failed hello attempt."""
        if not self._calibrating:
            return
        self._phase = CalibrationPhase.WAITING_SPEAK
        self._calibration_samples = []
        self._calibration_speech_seen = False
        self._calibration_speech_streak = 0
        self._calibration_silence_streak = 0
        self._post_prompt_grace_chunks = 0
        self._calibration_peak_level = 0.0

    def cancel_calibration(self) -> None:
        """Abort calibration without producing metrics."""
        self._calibrating = False
        self._phase = CalibrationPhase.QUIET

    def process_calibration_chunk(self, chunk: bytes) -> CalibrationStep | None:
        """Advance calibration for one audio chunk."""
        if not self._calibrating:
            return None

        if self._phase == CalibrationPhase.WAITING_SPEAK:
            return None

        level = chunk_rms(chunk)
        if self._phase == CalibrationPhase.QUIET:
            self._observe_quiet(level)
            self._chunks_left -= 1
            if self._chunks_left > 0:
                return None

            self._phase = CalibrationPhase.WAITING_SPEAK
            logger.info("Audio calibration: quiet phase done — play prompt next")
            return CalibrationStep.PLAY_PROMPT

        if self._post_prompt_grace_chunks > 0:
            self._post_prompt_grace_chunks -= 1
            return None

        self._calibration_samples.append(level)
        speech_floor = self._speech_detection_floor()
        if level >= speech_floor:
            self._calibration_peak_level = max(self._calibration_peak_level, level)
            self._calibration_speech_streak += 1
            if self._calibration_speech_streak >= MIN_SPEECH_CHUNKS:
                self._calibration_speech_seen = True
                self._calibration_silence_streak = 0
        else:
            self._calibration_speech_streak = 0
            if self._calibration_speech_seen:
                self._calibration_silence_streak += 1

        if (
            self._calibration_speech_seen
            and self._calibration_silence_streak >= SPEAK_SILENCE_CHUNKS_TO_FINISH
        ):
            return self._complete_with_voice()

        self._chunks_left -= 1
        if self._chunks_left > 0:
            return None

        if not self._calibration_speech_seen:
            logger.info(
                "Audio calibration: no speech detected within %.1fs",
                self._speak_sec,
            )
            return CalibrationStep.SPEECH_TIMEOUT

        return self._complete_with_voice()

    def begin_speak_phase(self) -> None:
        """Start listening for hello after the speaker prompt finishes."""
        if not self._calibrating or self._phase != CalibrationPhase.WAITING_SPEAK:
            return

        self._phase = CalibrationPhase.SPEAK
        self._chunks_left = max(1, int(self._speak_sec / CHUNK_SEC))
        self._calibration_samples = []
        self._calibration_speech_seen = False
        self._calibration_speech_streak = 0
        self._calibration_silence_streak = 0
        self._post_prompt_grace_chunks = POST_PROMPT_GRACE_CHUNKS
        self._calibration_peak_level = 0.0
        logger.info(
            "Audio calibration: waiting for hello (up to %.1fs after prompt)",
            self._speak_sec,
        )

    def calibration_payload(self) -> dict[str, float]:
        """Metrics sent to the laptop after calibration completes."""
        return {
            "noise_floor": round(self.noise_floor, 1),
            "user_speech_peak": round(self.user_speech_peak, 1),
            "speech_threshold": round(self.speech_start_threshold(), 1),
            "speech_detected": True,
        }

    def speech_start_threshold(self) -> float:
        return self.noise_floor + self.speech_margin * 0.32

    def _speech_detection_floor(self) -> float:
        """Threshold for calibration hello — tuned to calibrated noise, not defaults."""
        return self.noise_floor + max(40.0, min(100.0, self.noise_floor * 0.4 + 35.0))

    def _observe_quiet(self, rms: float) -> None:
        if rms < self.noise_floor + self.speech_margin * 0.25:
            self.noise_floor = 0.9 * self.noise_floor + 0.1 * rms

    def _complete_with_voice(self) -> CalibrationStep:
        peak = self._calibration_peak_level
        if peak <= 0 and self._calibration_samples:
            peak = max(self._calibration_samples)
        if peak < self.noise_floor + MIN_VOICE_ABOVE_NOISE:
            logger.info(
                "Audio calibration: voice level too low (peak=%.0f, need>=%.0f, floor=%.0f)",
                peak,
                self.noise_floor + MIN_VOICE_ABOVE_NOISE,
                self.noise_floor,
            )
            return CalibrationStep.SPEECH_TIMEOUT

        self._finish_calibration(peak)
        return CalibrationStep.COMPLETE

    def _finish_calibration(self, speech_peak: float) -> None:
        self.user_speech_peak = max(speech_peak, self.noise_floor + 250.0)
        self.user_speech_peak = min(self.user_speech_peak, self.noise_floor + 1200.0)
        self.speech_margin = self.user_speech_peak - self.noise_floor
        self._calibrating = False
        logger.info(
            "Audio calibrated: noise=%.0f voice≈%.0f speak>%.0f",
            self.noise_floor,
            self.user_speech_peak,
            self.speech_start_threshold(),
        )

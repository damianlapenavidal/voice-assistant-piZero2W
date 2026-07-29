"""Driving ALSA `softvol` controls from Python, via `amixer`.

Both audio paths apply their gain in ALSA rather than per-sample in Python
(see docs/battery-plan.md): playback attenuates, capture boosts, and the two
need exactly the same three things -- convert a linear gain to a control
index, push it with `amixer`, and create the control if it does not exist
yet. That is this module.

The plugins themselves live in `config/asoundrc.softvol`, installed as
`~/.asoundrc` on the device. The dB range and step count of each control are
stated in both places and must agree.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os

logger = logging.getLogger(__name__)

DEFAULT_CARD = "0"
# Any valid parameters will do for priming: the prime PCM is slaved to `null`,
# so nothing downstream cares what they are.
_PRIME_FORMAT = "S16_LE"
_PRIME_RATE = "24000"
_PRIME_CHANNELS = "1"


def mixer_card() -> str:
    """The ALSA card the mixer controls live on."""
    return os.environ.get("AUDIO_MIXER_CARD", DEFAULT_CARD)


class SoftvolControl:
    """One `softvol` volume control, addressed by name on a card.

    `min_db`/`max_db`/`steps` mirror the plugin's `min_dB`, `max_dB` and
    `resolution - 1`. softvol maps its index range onto that dB range
    linearly, except index 0, which is exact silence rather than the quietest
    step. Both were verified against the real plugin on the device by pushing
    a known signal through it and measuring the output at each index.
    """

    def __init__(
        self,
        *,
        card: str,
        control: str,
        prime_device: str,
        min_db: float,
        max_db: float,
        steps: int,
        capture: bool = False,
    ) -> None:
        self._card = card
        self._control = control
        self._prime_device = prime_device
        self._min_db = min_db
        self._max_db = max_db
        self._steps = steps
        self._capture = capture

    @property
    def control(self) -> str:
        return self._control

    def gain_to_index(self, gain: float) -> int:
        """Linear amplitude gain -> control index.

        Gains are the unit the client's sliders already work in (and what the
        old per-sample multiply consumed), so they are converted to dB here
        to reach the same level through a dB-linear control.

        A gain past the top of the range pins to the loudest step -- softvol
        cannot exceed its own `max_dB`. Only a gain of exactly 0 maps to the
        silent index: anything quieter than the floor but still positive pins
        to the quietest audible step, so "turned right down" never silently
        becomes "off".
        """
        if gain <= 0:
            return 0

        db = 20 * math.log10(gain)
        if db >= self._max_db:
            return self._steps

        span = self._max_db - self._min_db
        index = round((db - self._min_db) / span * self._steps)
        return max(1, min(self._steps, index))

    async def apply_gain(self, gain: float) -> bool:
        """Set the control to `gain`. True if ALSA took it.

        A failure is logged and swallowed rather than raised: losing a level
        change is a far better outcome than losing the session, and this must
        stay callable on a laptop running the tests, where there is no
        `amixer` at all.
        """
        index = self.gain_to_index(gain)

        # The first attempt is expected to fail once per boot, so it stays
        # quiet: softvol registers its control the first time the plugin is
        # opened, and until then there is nothing for amixer to set. Create
        # the control and retry -- otherwise the level agreed with the app at
        # handshake would not reach the hardware until something had already
        # played or recorded at the wrong one.
        if await self._set_index(index, quiet=True):
            return True

        if not await self._prime():
            return False
        logger.info("Created ALSA control %r", self._control)
        return await self._set_index(index)

    async def _set_index(self, index: int, *, quiet: bool = False) -> bool:
        return await self._run(
            [
                "amixer",
                "-c", self._card,
                "-q",
                "cset", f"name={self._control}",
                str(index),
            ],
            quiet=quiet,
        )

    async def _prime(self) -> bool:
        """Open the softvol chain once, so it registers its control.

        The prime PCM is slaved to `null`, so this touches no hardware: it
        cannot make a sound, cannot record one, and cannot fail with "device
        busy" against a stream already running.
        """
        if self._capture:
            cmd = [
                "arecord", "-D", self._prime_device,
                "-f", _PRIME_FORMAT, "-r", _PRIME_RATE, "-c", _PRIME_CHANNELS,
                "-t", "raw", "-q", "--samples=1", os.devnull,
            ]
        else:
            cmd = [
                "aplay", "-D", self._prime_device,
                "-f", _PRIME_FORMAT, "-r", _PRIME_RATE, "-c", _PRIME_CHANNELS,
                "-t", "raw", "-q", os.devnull,
            ]
        return await self._run(cmd)

    async def _run(self, cmd: list[str], *, quiet: bool = False) -> bool:
        """Run a short-lived ALSA helper; True if it exited cleanly.

        `quiet` demotes a failure to debug, for the one call whose failure is
        a normal state rather than a problem.
        """
        log = logger.debug if quiet else logger.warning
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:  # includes FileNotFoundError: no alsa-utils
            log("Could not run %s: %s", cmd[0], exc)
            return False

        _, stderr_data = await process.communicate()
        if process.returncode != 0:
            detail = stderr_data.decode("utf-8", errors="replace").strip()
            log(
                "%s exited with code %s%s",
                cmd[0], process.returncode, f": {detail}" if detail else "",
            )
            return False
        return True

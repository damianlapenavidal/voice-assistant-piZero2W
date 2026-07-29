#!/usr/bin/env python3
"""
Tests for Pi Zero 2W audio capture/playback and AUDIO_FRAME message format.

Run with: python test_audio.py
"""

import asyncio
import base64
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

import struct

from alsa_mixer import SoftvolControl
from audio_capture import (
    CAPTURE_CHANNELS,
    CAPTURE_MAX_DB,
    CAPTURE_MIN_DB,
    CAPTURE_STEPS,
    CHUNK_BYTES,
    AudioCapture,
)
from audio_playback import (
    BYTE_RATE,
    PLAYBACK_MAX_DB,
    PLAYBACK_MIN_DB,
    PLAYBACK_STEPS,
    PlaybackManager,
)
from zero2w_client import make_audio_frame, parse_message


def _control(min_db: float, max_db: float, steps: int) -> SoftvolControl:
    """A control with the shipped range, for asking what index a gain maps to."""
    return SoftvolControl(
        card="0", control="probe", prime_device="probe",
        min_db=min_db, max_db=max_db, steps=steps,
    )


_VOLUME = _control(PLAYBACK_MIN_DB, PLAYBACK_MAX_DB, PLAYBACK_STEPS)
_MIC = _control(CAPTURE_MIN_DB, CAPTURE_MAX_DB, CAPTURE_STEPS)

gain_to_softvol_index = _VOLUME.gain_to_index  # playback: the common case here


class _FakeAlsaTool:
    """Stands in for the ALSA tools, recording what would have been run.

    Both levels now leave the process, so tests must intercept them: on the
    device the real tools exist, and an unpatched test run would leave the
    speaker and the mic at whatever level the last assertion happened to use.

    Patching reaches every module at once -- `audio_capture.asyncio` and
    `alsa_mixer.asyncio` are the same object -- so this also stands in for
    the long-lived `aplay`/`arecord` streams when given a `stream_process`.

    `mixer_failures` makes that many leading `amixer` calls report failure,
    which is how a device that has not yet created its softvol control
    behaves.
    """

    def __init__(self, *, mixer_failures: int = 0, stream_process=None):
        self.commands: list[list[str]] = []
        self._mixer_failures = mixer_failures
        self._stream_process = stream_process

    async def __call__(self, *cmd: str, **kwargs):
        self.commands.append(list(cmd))

        # Priming opens a `*_prime` PCM; anything else on aplay/arecord is a
        # real stream, and gets the caller's mock process.
        if self._stream_process is not None and not self._is_prime(cmd):
            if cmd[0] in ("aplay", "arecord"):
                return self._stream_process

        returncode = 0
        if cmd[0] == "amixer" and self._mixer_failures > 0:
            self._mixer_failures -= 1
            returncode = 1

        process = MagicMock()
        process.returncode = returncode
        process.communicate = AsyncMock(return_value=(b"", b""))
        return process

    @staticmethod
    def _is_prime(cmd) -> bool:
        return any(str(arg).endswith("_prime") for arg in cmd)

    @property
    def streams_started(self) -> list[list[str]]:
        """aplay/arecord invocations that were real streams, not priming."""
        return [
            cmd for cmd in self.commands
            if cmd[0] in ("aplay", "arecord") and not self._is_prime(cmd)
        ]

    @property
    def mixer_indexes(self) -> list[int]:
        """The softvol index each `amixer` call would have set."""
        return [int(cmd[-1]) for cmd in self.commands if cmd[0] == "amixer"]

    @property
    def tools_run(self) -> list[str]:
        return [cmd[0] for cmd in self.commands]


def _patch_alsa(tool: _FakeAlsaTool):
    """Route every mixer subprocess -- capture's and playback's -- to `tool`."""
    return patch("alsa_mixer.asyncio.create_subprocess_exec", new=tool)


def test_audio_frame_message_structure():
    """AUDIO_FRAME matches protocol: type, payload fields, timestamp."""
    pcm = b"\x00\x01" * (CHUNK_BYTES // 2)
    raw = make_audio_frame(pcm, sequence_number=1, capture_timestamp="2026-06-30T15:30:00.123Z")
    msg = json.loads(raw)

    assert msg["type"] == "AUDIO_FRAME"
    assert set(msg.keys()) == {"type", "payload", "timestamp"}
    assert msg["payload"]["sequence_number"] == 1
    assert msg["payload"]["timestamp"] == "2026-06-30T15:30:00.123Z"
    assert isinstance(msg["payload"]["audio"], str)
    assert "T" in msg["timestamp"]

    print("  PASS: test_audio_frame_message_structure")


def test_audio_frame_base64_roundtrip():
    """Payload audio decodes to the original PCM bytes."""
    pcm = bytes(range(256)) * 19  # 4864 bytes, trim to chunk
    pcm = pcm[:CHUNK_BYTES]

    raw = make_audio_frame(pcm, sequence_number=42)
    msg = parse_message(raw)
    decoded = base64.b64decode(msg["payload"]["audio"])

    assert decoded == pcm
    assert msg["payload"]["sequence_number"] == 42

    print("  PASS: test_audio_frame_base64_roundtrip")


def test_audio_frame_chunk_size():
    """Typical capture chunk is 4800 bytes (100 ms at 24 kHz mono)."""
    assert CHUNK_BYTES == 4800

    pcm = b"\x00" * CHUNK_BYTES
    raw = make_audio_frame(pcm, sequence_number=1)
    msg = json.loads(raw)
    decoded = base64.b64decode(msg["payload"]["audio"])

    assert len(decoded) == CHUNK_BYTES

    print("  PASS: test_audio_frame_chunk_size")


async def _test_audio_capture_read_chunk():
    """read_chunk() returns exactly CHUNK_BYTES, exactly as arecord produced them.

    Channel selection and gain both happen upstream in ALSA now, so anything
    this method did to the samples would be a bug: it would be scaling audio
    a second time.
    """
    n = CHUNK_BYTES // 2  # 2 bytes per sample
    mono = struct.pack(f"<{n}h", *([100] * n))

    fake_stdout = asyncio.StreamReader()
    fake_stdout.feed_data(mono)
    fake_stdout.feed_eof()

    fake_process = MagicMock()
    fake_process.returncode = None
    fake_process.stdout = fake_stdout
    fake_process.stderr = asyncio.StreamReader()

    capture = AudioCapture(device="mic_in", input_gain=4.0)
    capture._process = fake_process
    capture._running = True

    chunk = await capture.read_chunk()
    assert chunk == mono, "read_chunk must not touch the samples"
    assert len(chunk) == CHUNK_BYTES
    assert capture._build_command()[-2:] == ["-D", "mic_in"]

    print("  PASS: test_audio_capture_read_chunk")


async def _test_apply_input_gain_drives_alsa_mixer():
    """SET_MIC_GAIN becomes an amixer call on the mic's softvol control.

    Unlike playback this has to boost, not attenuate: the deployed gain of
    20 is +26 dB. The index is the one measured from the real plugin.
    """
    alsa = _FakeAlsaTool()
    capture = AudioCapture(input_gain=1.0)

    with _patch_alsa(alsa):
        assert await capture.apply_input_gain(20.0) is True

    assert alsa.tools_run == ["amixer"]
    assert "name=Mic Capture Volume" in alsa.commands[0]
    assert alsa.mixer_indexes == [231]  # 19.952x on hardware; INPUT_GAIN=20.0
    assert capture.input_gain == 20.0

    print("  PASS: test_apply_input_gain_drives_alsa_mixer")


async def _test_mic_control_is_created_when_missing():
    """A fresh boot has no mic control either; priming uses arecord, silently."""
    alsa = _FakeAlsaTool(mixer_failures=1)
    capture = AudioCapture(input_gain=1.0)

    with _patch_alsa(alsa):
        assert await capture.apply_input_gain(20.0) is True

    assert alsa.tools_run == ["amixer", "arecord", "amixer"]
    assert alsa.commands[1][:3] == ["arecord", "-D", "mic_prime"]
    # --samples=1 so priming returns instantly instead of recording anything.
    assert "--samples=1" in alsa.commands[1]

    print("  PASS: test_mic_control_is_created_when_missing")


async def _test_audio_capture_start_uses_arecord():
    """start() spawns arecord with S16_LE 24000 Hz, mono (ALSA already picked
    the mic's channel -- see CAPTURE_CHANNELS) raw format."""
    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdout = asyncio.StreamReader()
    mock_process.stderr = asyncio.StreamReader()

    with patch(
        "audio_capture.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ) as mock_exec:
        capture = AudioCapture()
        await capture.start()

        mock_exec.assert_awaited_once()
        cmd = mock_exec.await_args.args
        assert cmd[0] == "arecord"
        assert "-f" in cmd and "S16_LE" in cmd
        assert "-r" in cmd and "24000" in cmd
        assert "-c" in cmd and str(CAPTURE_CHANNELS) in cmd
        assert "-t" in cmd and "raw" in cmd

    print("  PASS: test_audio_capture_start_uses_arecord")


async def _test_playback_manager_pipes_to_aplay():
    """play_pcm16_chunk() writes PCM bytes to aplay stdin."""
    fake_stdin = MagicMock()
    fake_stdin.write = MagicMock()
    fake_stdin.drain = AsyncMock()
    fake_stdin.is_closing = MagicMock(return_value=False)
    fake_stdin.close = MagicMock()
    fake_stdin.wait_closed = AsyncMock()

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdin = fake_stdin
    mock_process.stderr = asyncio.StreamReader()

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ) as mock_exec:
        playback = PlaybackManager(device="plughw:0,0")
        pcm = b"\x00\x01" * 100
        await playback.play_pcm16_chunk(pcm)

        mock_exec.assert_awaited_once()
        cmd = mock_exec.await_args.args
        assert cmd[0] == "aplay"
        assert "-f" in cmd and "S16_LE" in cmd
        assert "-r" in cmd and "24000" in cmd
        fake_stdin.write.assert_called_once_with(pcm)
        fake_stdin.drain.assert_awaited_once()

    print("  PASS: test_playback_manager_pipes_to_aplay")


def _test_gain_to_softvol_index_matches_measured_curve():
    """An amplitude gain maps onto the softvol step of the same level.

    The expected values here are not derived from the implementation: they
    were measured on the device, by pushing a known signal through the real
    plugins and reading back the result at each index. softvol is dB-linear
    across its range, with index 0 a special case that is exactly silent
    rather than merely very quiet.
    """
    # --- playback: 0..100 -> -51..0 dB (attenuation only) ---
    assert gain_to_softvol_index(1.0) == PLAYBACK_STEPS  # 0 dB, source untouched
    assert gain_to_softvol_index(0.0) == 0  # true silence, measured as zeros

    # Measured: index 22 -> 0.0102, index 50 -> 0.0530, index 76 -> ~0.245.
    assert gain_to_softvol_index(0.01) == 22  # -40.0 dB
    assert gain_to_softvol_index(0.053) == 50  # -25.5 dB
    assert gain_to_softvol_index(0.25) == 76  # -12.0 dB

    # --- capture: 0..255 -> -51..+34 dB, because the mic needs boosting ---
    assert _MIC.gain_to_index(0.0) == 0
    assert _MIC.gain_to_index(1.0) == 153  # 0 dB: the plugin's own default
    assert _MIC.gain_to_index(20.0) == 231  # deployed INPUT_GAIN; measured 19.952x
    assert _MIC.gain_to_index(50.0) == CAPTURE_STEPS  # +34 dB, top of the mic slider

    # Unity is mid-scale on the mic control, unreachable-past on the volume
    # one: the two ranges really do point in different directions.
    assert _MIC.gain_to_index(20.0) > _MIC.gain_to_index(1.0)

    # Monotonic, and every step in range, across both sliders' full travel.
    for control, gains in (
        (_VOLUME, [(pct / 100) ** 2 for pct in range(101)]),  # square-law taper
        (_MIC, [pct / 100 * 50.0 for pct in range(101)]),  # linear taper
    ):
        prev = -1
        for gain in gains:
            index = control.gain_to_index(gain)
            assert 0 <= index <= control._steps
            assert index >= prev
            prev = index

    # Below the plugin's floor but not silent: pinned to the quietest audible
    # step, never to mute. Turning a slider down must not act like off.
    for control, min_db in ((_VOLUME, PLAYBACK_MIN_DB), (_MIC, CAPTURE_MIN_DB)):
        assert control.gain_to_index(10 ** ((min_db - 6) / 20)) == 1

    print("  PASS: test_gain_to_softvol_index_matches_measured_curve")


def _test_loud_source_cannot_be_boosted_into_clipping():
    """Reproduces the reported bug: an already near-full-scale source (like
    OpenAI's normalized TTS output) combined with gain left over from tuning
    a much quieter source (loopback's raw mic echo) must not be driven into
    the ceiling.

    It used to be soft-limited on the way past. Now it cannot get there:
    softvol only attenuates, so any gain at or above unity is the same 0 dB
    step and the samples reach the card as they arrived.
    """
    assert gain_to_softvol_index(1.0) == PLAYBACK_STEPS
    for over_driven in (1.0001, 2.5, 20.0):
        assert gain_to_softvol_index(over_driven) == PLAYBACK_STEPS

    print("  PASS: test_loud_source_cannot_be_boosted_into_clipping")


async def _test_playback_manager_writes_samples_untouched():
    """play_pcm16_chunk() hands aplay exactly the bytes it was given.

    Volume is ALSA's job now. Scaling here is what cost ~10% of one core
    while the assistant spoke, and it forced the write path to dribble audio
    out in paced sub-chunks so a mid-response change could still land -- so
    a single unmodified write is the point, not an incidental detail.
    """
    mock_process, fake_stdin = _make_mock_streaming_process()

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        playback = PlaybackManager(playback_gain=0.5)
        # Several 100 ms sub-chunks' worth: the old path would have split it.
        pcm = struct.pack("<6h", 1000, -1000, 20000, -20000, 32767, -32768) * 2000
        await playback.play_pcm16_chunk(pcm)

    fake_stdin.write.assert_called_once_with(pcm)

    print("  PASS: test_playback_manager_writes_samples_untouched")


async def _test_apply_playback_gain_drives_alsa_mixer():
    """A gain change becomes one `amixer` call on the softvol control."""
    alsa = _FakeAlsaTool()
    playback = PlaybackManager(playback_gain=1.0)

    with _patch_alsa(alsa):
        assert await playback.apply_playback_gain(0.25) is True

    assert alsa.tools_run == ["amixer"], "expected exactly one amixer call"
    cmd = alsa.commands[0]
    assert cmd[:2] == ["amixer", "-c"]
    assert "cset" in cmd
    # The control is named as one argv element, spaces and all.
    assert "name=PCM Playback Volume" in cmd
    assert alsa.mixer_indexes == [gain_to_softvol_index(0.25)]

    # The value is still readable on the device side, for HELLO.
    assert playback.playback_gain == 0.25

    print("  PASS: test_apply_playback_gain_drives_alsa_mixer")


async def _test_mixer_control_is_created_when_missing():
    """A gain change on a freshly booted device creates the control first.

    softvol only registers its mixer control when the plugin is first
    opened, so until something has played there is nothing for amixer to
    set. Without this recovery the level agreed at handshake would not reach
    the hardware until the second response of the session -- and the first
    would play at whatever the control powers up at, which is full scale.
    """
    alsa = _FakeAlsaTool(mixer_failures=1)
    playback = PlaybackManager(playback_gain=1.0)

    with _patch_alsa(alsa):
        assert await playback.apply_playback_gain(0.25) is True

    # Failed, primed (a silent aplay that touches no hardware), retried.
    assert alsa.tools_run == ["amixer", "aplay", "amixer"]
    assert alsa.commands[1][:3] == ["aplay", "-D", "softvol_prime"]
    assert alsa.mixer_indexes == [gain_to_softvol_index(0.25)] * 2

    print("  PASS: test_mixer_control_is_created_when_missing")


async def _test_playback_survives_a_missing_mixer():
    """No amixer (or no softvol control) loses the volume change, not the session."""
    async def _no_such_tool(*cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    playback = PlaybackManager(playback_gain=1.0)
    with patch("audio_playback.asyncio.create_subprocess_exec", new=_no_such_tool):
        assert await playback.apply_playback_gain(0.25) is False

    # Reported level still tracks what was asked for, so HELLO stays honest
    # about what the app requested even when the hardware ignored it.
    assert playback.playback_gain == 0.25

    print("  PASS: test_playback_survives_a_missing_mixer")


def _make_mock_streaming_process():
    """Return a mock aplay process suitable for streaming chunk tests."""
    fake_stdin = MagicMock()
    fake_stdin.write = MagicMock()
    fake_stdin.drain = AsyncMock()
    fake_stdin.is_closing = MagicMock(return_value=False)
    fake_stdin.close = MagicMock()
    fake_stdin.wait_closed = AsyncMock()

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdin = fake_stdin
    mock_process.stderr = asyncio.StreamReader()
    mock_process.communicate = AsyncMock(return_value=(b"", b""))

    return mock_process, fake_stdin


async def _test_streaming_chunks_then_finalize():
    """N streaming chunks then finalize plays all bytes through one aplay process."""
    mock_process, fake_stdin = _make_mock_streaming_process()
    chunk = b"\x00\x01" * (CHUNK_BYTES // 2)  # 4800 bytes
    num_chunks = 3

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ) as mock_exec:
        playback = PlaybackManager()
        for _ in range(num_chunks):
            await playback.play_pcm16_chunk(chunk, is_final=False)

        assert playback.is_streaming
        assert mock_exec.await_count == 1
        assert fake_stdin.write.call_count == num_chunks

        duration = await playback.finalize_streaming()

        assert not playback.is_streaming
        expected_bytes = num_chunks * len(chunk)
        assert duration == expected_bytes / BYTE_RATE
        fake_stdin.close.assert_called_once()
        mock_process.communicate.assert_awaited_once()

    print("  PASS: test_streaming_chunks_then_finalize")


async def _test_single_blob_still_works():
    """One is_final=True blob uses _play_final (dedicated aplay), not streaming."""
    final_stdin = MagicMock()
    final_stdin.write = MagicMock()
    final_stdin.drain = AsyncMock()
    final_stdin.is_closing = MagicMock(return_value=False)
    final_stdin.close = MagicMock()
    final_stdin.wait_closed = AsyncMock()

    final_process = MagicMock()
    final_process.returncode = 0
    final_process.stdin = final_stdin
    final_process.stderr = asyncio.StreamReader()
    final_process.communicate = AsyncMock(return_value=(b"", b""))

    pcm = b"\x00\x01" * 5000  # single-blob response

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=final_process),
    ) as mock_exec:
        playback = PlaybackManager()
        duration = await playback.play_pcm16_chunk(pcm, is_final=True)

        assert not playback.is_streaming
        assert duration == len(pcm) / BYTE_RATE
        mock_exec.assert_awaited_once()
        cmd = mock_exec.await_args.args
        assert cmd[0] == "aplay"
        assert "-q" in cmd
        final_stdin.write.assert_called()
        final_stdin.close.assert_called_once()
        final_process.communicate.assert_awaited_once()

    print("  PASS: test_single_blob_still_works")


async def _test_finalize_returns_correct_duration():
    """finalize_streaming() returns duration from all streamed bytes, including last."""
    mock_process, fake_stdin = _make_mock_streaming_process()
    chunks = [b"\x00" * 4800, b"\x01" * 4800, b"\x02" * 1200]

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        playback = PlaybackManager()
        for chunk in chunks[:-1]:
            await playback.play_pcm16_chunk(chunk, is_final=False)
        await playback.play_pcm16_chunk(chunks[-1], is_final=False)

        total_bytes = sum(len(c) for c in chunks)
        duration = await playback.finalize_streaming()

        assert duration == total_bytes / BYTE_RATE
        assert not playback.is_streaming

    print("  PASS: test_finalize_returns_correct_duration")


async def _test_streaming_finalize_with_empty_final_chunk():
    """Empty is_final body after chunks still finalizes the full stream."""
    mock_process, fake_stdin = _make_mock_streaming_process()
    chunk = b"\x00" * 4800

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        playback = PlaybackManager()
        await playback.play_pcm16_chunk(chunk, is_final=False)
        await playback.play_pcm16_chunk(chunk, is_final=False)

        duration = await playback.finalize_streaming()
        assert duration == (2 * len(chunk)) / BYTE_RATE

    print("  PASS: test_streaming_finalize_with_empty_final_chunk")


class _FakeWebSocket:
    """Minimal async-iterable fake WS: yields preset messages, then closes."""

    def __init__(self, messages: list[str]):
        self._messages = messages
        self.sent: list[str] = []
        self._recv_index = 0

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m

    async def recv(self) -> str:
        """Pop the next preset message, for code that awaits recv() directly
        (the handshake) rather than iterating."""
        if self._recv_index >= len(self._messages):
            raise AssertionError("recv() called with no messages left")
        msg = self._messages[self._recv_index]
        self._recv_index += 1
        return msg

    async def send(self, msg: str) -> None:
        self.sent.append(msg)


def _one_chunk_then_block(chunk: bytes):
    """A read_chunk stand-in that yields one chunk, then blocks forever.

    A real capture blocks until arecord has audio, which paces the stream
    loop at real time. A mock that returns instantly instead spins the loop
    as fast as the event loop allows -- millions of iterations and unbounded
    memory in the time it takes to cancel it. Blocking after the first chunk
    keeps the send count deterministic and the test honest about pacing.
    """
    async def read_chunk():
        if not sent_one:
            sent_one.append(True)
            return chunk
        await asyncio.Event().wait()

    sent_one: list = []
    return read_chunk


async def _test_set_volume_updates_playback_gain():
    """SET_VOLUME (0-100) maps onto [0, MAX_PLAYBACK_GAIN] with a square-law
    taper, so the audible change is spread across the slider rather than
    crammed into the bottom of its travel."""
    from zero2w_client import MAX_PLAYBACK_GAIN, Zero2WClient

    client = Zero2WClient("ws://test")
    alsa = _FakeAlsaTool()

    with _patch_alsa(alsa):
        ws = _FakeWebSocket([json.dumps({"type": "SET_VOLUME", "payload": {"volume": 70}})])
        await client._receive_loop(ws)
        assert abs(client._playback.playback_gain - 0.49 * MAX_PLAYBACK_GAIN) < 1e-9

        # The endpoints stay exact: full scale is the source untouched, 0 is silence.
        ws = _FakeWebSocket([json.dumps({"type": "SET_VOLUME", "payload": {"volume": 100}})])
        await client._receive_loop(ws)
        assert abs(client._playback.playback_gain - MAX_PLAYBACK_GAIN) < 1e-9

        ws = _FakeWebSocket([json.dumps({"type": "SET_VOLUME", "payload": {"volume": 0}})])
        await client._receive_loop(ws)
        assert client._playback.playback_gain == 0.0

        # Out-of-range values are clamped, not rejected.
        ws = _FakeWebSocket([json.dumps({"type": "SET_VOLUME", "payload": {"volume": 150}})])
        await client._receive_loop(ws)
        assert abs(client._playback.playback_gain - MAX_PLAYBACK_GAIN) < 1e-9

        # Monotonic across the whole range -- raising the slider never gets
        # quieter, at the ALSA step actually applied as well as in the gain.
        prev = -1.0
        prev_index = -1
        for pct in range(0, 101, 10):
            ws = _FakeWebSocket([json.dumps({"type": "SET_VOLUME", "payload": {"volume": pct}})])
            await client._receive_loop(ws)
            assert client._playback.playback_gain > prev
            prev = client._playback.playback_gain
            assert alsa.mixer_indexes[-1] > prev_index
            prev_index = alsa.mixer_indexes[-1]

    # Every one of those reached ALSA -- the endpoints as the endpoints.
    assert alsa.mixer_indexes[0] == gain_to_softvol_index(0.49)
    assert alsa.mixer_indexes[1] == PLAYBACK_STEPS
    assert alsa.mixer_indexes[2] == 0

    print("  PASS: test_set_volume_updates_playback_gain")


async def _test_set_mic_gain_updates_input_gain():
    """SET_MIC_GAIN (0-100) maps onto [0, MAX_INPUT_GAIN], applied live to capture."""
    from zero2w_client import MAX_INPUT_GAIN, Zero2WClient

    client = Zero2WClient("ws://test")
    alsa = _FakeAlsaTool()

    with _patch_alsa(alsa):
        ws = _FakeWebSocket([json.dumps({"type": "SET_MIC_GAIN", "payload": {"gain": 40}})])
        await client._receive_loop(ws)
        assert abs(client._audio_capture.input_gain - 0.4 * MAX_INPUT_GAIN) < 1e-9

        # Out-of-range values are clamped, not rejected.
        ws = _FakeWebSocket([json.dumps({"type": "SET_MIC_GAIN", "payload": {"gain": 150}})])
        await client._receive_loop(ws)
        assert abs(client._audio_capture.input_gain - MAX_INPUT_GAIN) < 1e-9

    # Both reached ALSA, and 40% really is the deployed INPUT_GAIN of 20.
    assert alsa.mixer_indexes == [_MIC.gain_to_index(20.0), CAPTURE_STEPS]

    print("  PASS: test_set_mic_gain_updates_input_gain")


async def _test_skip_calibration_streams_immediately():
    """START_AUDIO_STREAM with skip_calibration bypasses the prompt (resume)."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")

    sent: list[dict] = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(json.loads(msg)))

    started = {"value": False}

    async def fake_start():
        started["value"] = True

    client._audio_capture.start = AsyncMock(side_effect=fake_start)

    await client._start_audio(ws, {"skip_calibration": True})

    # No calibration prompt / status when resuming; stream live immediately.
    assert started["value"] is True
    assert client.is_recording is True
    assert client._stream_to_laptop is True
    assert client._audio_gating.is_calibrating is False
    assert not any(m["type"] == "CALIBRATION_STATUS" for m in sent)

    await client._stop_audio()
    print("  PASS: test_skip_calibration_streams_immediately")


async def _test_fresh_start_runs_calibration():
    """START_AUDIO_STREAM without skip_calibration begins calibration."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")

    sent: list[dict] = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(json.loads(msg)))

    client._audio_capture.start = AsyncMock()

    await client._start_audio(ws, None)

    assert client.is_recording is True
    assert client._stream_to_laptop is False
    assert client._audio_gating.is_calibrating is True
    assert any(
        m["type"] == "CALIBRATION_STATUS" and m["payload"].get("phase") == "quiet"
        for m in sent
    )

    await client._stop_audio()
    print("  PASS: test_fresh_start_runs_calibration")


async def _test_drain_buffered_audio_discards_backlog():
    """drain_buffered_audio() drops piled-up mic bytes, then stops once reads block.

    Reproduces the calibration echo bug: while the prompt plays, arecord keeps
    filling its pipe. That backlog must be dropped before the speak phase so it
    is not replayed in a burst and mistaken for the user's hello.
    """
    fake_stdout = asyncio.StreamReader()
    backlog = b"\x11" * (CHUNK_BYTES * 2 + 100)
    fake_stdout.feed_data(backlog)
    # Deliberately no feed_eof(): after the backlog is read, the next read
    # blocks — mimicking real-time capture — so drain should time out and stop.

    fake_process = MagicMock()
    fake_process.returncode = None
    fake_process.stdout = fake_stdout
    fake_process.stderr = asyncio.StreamReader()

    capture = AudioCapture()
    capture._process = fake_process
    capture._running = True

    discarded = await capture.drain_buffered_audio(max_drain_sec=1.0)
    assert discarded == len(backlog), discarded

    print("  PASS: test_drain_buffered_audio_discards_backlog")


async def _test_drain_continuously_keeps_pipe_empty():
    """drain_continuously() keeps consuming so the pipe never backs up, until cancelled.

    Regression test: without a concurrent drain, capture blocked behind
    prompt playback overflowed the pipe once capture became stereo (2x the
    byte rate), leading to an ALSA overrun that broke concurrent aplay
    ("aplay pipe broken during final playback").
    """
    fake_stdout = asyncio.StreamReader()
    # Feed far more than one CHUNK_BYTES read would consume,
    # simulating arecord producing data continuously in real time.
    fake_stdout.feed_data(b"\x22" * (CHUNK_BYTES * 5))

    fake_process = MagicMock()
    fake_process.returncode = None
    fake_process.stdout = fake_stdout
    fake_process.stderr = asyncio.StreamReader()

    capture = AudioCapture()
    capture._process = fake_process
    capture._running = True

    task = asyncio.create_task(capture.drain_continuously())
    await asyncio.sleep(0.05)  # let it consume the fed backlog
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # All previously-fed data was consumed; a follow-up read gets nothing more
    # (proves the pipe was actually drained, not just that the task ran).
    fake_stdout.feed_eof()
    remaining = await fake_stdout.read(1)
    assert remaining == b""

    print("  PASS: test_drain_continuously_keeps_pipe_empty")


async def _test_drain_buffered_audio_noop_when_idle():
    """drain_buffered_audio() returns 0 when capture is not running."""
    capture = AudioCapture()
    assert await capture.drain_buffered_audio() == 0

    print("  PASS: test_drain_buffered_audio_noop_when_idle")


async def _test_set_volume_not_blocked_by_active_playback():
    """A SET_VOLUME is applied even while a PLAY_AUDIO frame is still playing.

    PLAY_AUDIO is drained by a background worker, so the receive loop handles
    SET_VOLUME immediately instead of stalling behind buffered audio. This is
    what makes the volume slider respond mid-response, not only once the
    assistant pauses.
    """
    from zero2w_client import MAX_PLAYBACK_GAIN, Zero2WClient

    client = Zero2WClient("ws://test")

    # Make the (background) playback hang so the frame is still "in flight"
    # while the receive loop moves on to the SET_VOLUME message.
    gate = asyncio.Event()

    async def _hang(ws, payload):
        await gate.wait()

    client._handle_play_audio = _hang

    b64 = base64.b64encode(b"\x00\x01" * 10).decode()
    ws = _FakeWebSocket([
        json.dumps({"type": "PLAY_AUDIO", "payload": {"audio": b64}}),
        json.dumps({"type": "SET_VOLUME", "payload": {"volume": 50}}),
    ])

    alsa = _FakeAlsaTool()
    with _patch_alsa(alsa):
        await client._receive_loop(ws)

    # The volume landed despite playback never having completed.
    assert abs(client._playback.playback_gain - 0.25 * MAX_PLAYBACK_GAIN) < 1e-9
    assert alsa.mixer_indexes == [gain_to_softvol_index(0.25 * MAX_PLAYBACK_GAIN)]

    gate.set()
    await client._stop_playback_worker()

    print("  PASS: test_set_volume_not_blocked_by_active_playback")


async def _test_volume_change_applies_partway_through_a_response():
    """Reproduces the reported bug: a SET_VOLUME during a whole-response blob
    must be audible before that response ends, not only on the next one.

    The response used to be scaled by playback_gain in one pass before any
    bytes were written, so a mid-response change could not be heard until
    playback drained. softvol removes the problem rather than working around
    it: the audio is never scaled here, and the control that scales it sits
    downstream of aplay's buffer, so the change reaches audio aplay has
    already accepted -- which no amount of care in this process could.
    """
    alsa = _FakeAlsaTool()
    mock_process, fake_stdin = _make_mock_streaming_process()
    mock_process.returncode = 0

    # 5 s of audio, of which aplay has swallowed some and is still playing:
    # hold the write open so the response is genuinely mid-flight.
    still_playing = asyncio.Event()
    fake_stdin.drain = AsyncMock(side_effect=still_playing.wait)
    pcm = b"\x00\x01" * int(BYTE_RATE * 5.0 // 2)

    playback = PlaybackManager(playback_gain=1.0)

    async def _aplay_or_alsa_tool(*cmd, **kwargs):
        if cmd[0] == "aplay" and "softvol_prime" not in cmd:
            return mock_process  # the response being played
        return await alsa(*cmd, **kwargs)  # amixer, or a prime attempt

    with patch("audio_playback.asyncio.create_subprocess_exec", new=_aplay_or_alsa_tool):
        response = asyncio.create_task(playback.play_pcm16_chunk(pcm, is_final=True))
        await asyncio.sleep(0)  # let the write start

        await playback.apply_playback_gain(0.25)
        # Landed while the response is still playing, without waiting on it.
        assert alsa.mixer_indexes == [gain_to_softvol_index(0.25)]
        assert not response.done()

        still_playing.set()
        await response

    # ...and the audio itself went out untouched, at any volume.
    assert fake_stdin.write.call_args[0][0] == pcm

    print("  PASS: test_volume_change_applies_partway_through_a_response")


async def _test_set_mic_gain_not_blocked_by_active_playback():
    """SET_MIC_GAIN is applied while a PLAY_AUDIO frame is still playing.

    Mic sensitivity has no baked-in-gain problem (gain is applied in
    read_chunk, as each chunk is pulled off arecord), so the only thing that
    could delay it is the receive loop stalling behind playback. Pin that it
    doesn't.
    """
    from zero2w_client import MAX_INPUT_GAIN, Zero2WClient

    client = Zero2WClient("ws://test")

    gate = asyncio.Event()

    async def _hang(ws, payload):
        await gate.wait()

    client._handle_play_audio = _hang

    b64 = base64.b64encode(b"\x00\x01" * 10).decode()
    ws = _FakeWebSocket([
        json.dumps({"type": "PLAY_AUDIO", "payload": {"audio": b64}}),
        json.dumps({"type": "SET_MIC_GAIN", "payload": {"gain": 60}}),
    ])

    alsa = _FakeAlsaTool()
    with _patch_alsa(alsa):
        await client._receive_loop(ws)

    assert abs(client._audio_capture.input_gain - 0.6 * MAX_INPUT_GAIN) < 1e-9
    assert alsa.mixer_indexes == [_MIC.gain_to_index(0.6 * MAX_INPUT_GAIN)]

    gate.set()
    await client._stop_playback_worker()

    print("  PASS: test_set_mic_gain_not_blocked_by_active_playback")


async def _test_mic_gain_change_does_not_restart_capture():
    """A mid-stream mic gain change goes to ALSA, leaving arecord alone.

    The gain lands upstream of arecord, on audio not yet captured, so there
    is nothing to restart and nothing already in flight to re-scale -- and
    chunks keep arriving byte-for-byte as the hardware produced them.
    """
    mono = struct.pack("<2400h", *([1000] * 2400))

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdout = MagicMock()
    mock_process.stdout.readexactly = AsyncMock(return_value=mono)
    mock_process.stderr = asyncio.StreamReader()

    alsa = _FakeAlsaTool(stream_process=mock_process)
    with _patch_alsa(alsa):
        capture = AudioCapture(input_gain=1.0)
        await capture.start()

        assert await capture.read_chunk() == mono
        await capture.apply_input_gain(3.0)
        assert await capture.read_chunk() == mono  # unchanged: ALSA did the scaling

    assert alsa.mixer_indexes == [_MIC.gain_to_index(3.0)]
    assert len(alsa.streams_started) == 1, "capture restarted for a gain change"

    print("  PASS: test_mic_gain_change_does_not_restart_capture")


def _test_volume_percent_gain_roundtrip():
    """gain_to_volume_percent inverts volume_percent_to_gain.

    HELLO reports the device's startup gain as a slider position, so the two
    directions must agree -- otherwise the app draws its slider somewhere the
    hardware isn't and the mismatch we're fixing comes back.
    """
    from zero2w_client import gain_to_volume_percent, volume_percent_to_gain

    for pct in range(0, 101):
        assert gain_to_volume_percent(volume_percent_to_gain(pct)) == pct

    # The deployed .env fallback maps to a sensible slider position.
    assert gain_to_volume_percent(0.35) == 59
    # Degenerate inputs don't blow up or report a bogus position.
    assert gain_to_volume_percent(0.0) == 0
    assert gain_to_volume_percent(-1.0) == 0
    assert gain_to_volume_percent(99.0) == 100

    print("  PASS: test_volume_percent_gain_roundtrip")


async def _test_handshake_adopts_app_levels():
    """The device takes the app's remembered levels from HELLO_ACK.

    This is what stops the first slider touch of a session from jumping the
    volume: the app's value is applied at handshake, before any audio plays.
    """
    from zero2w_client import (
        MAX_INPUT_GAIN,
        Zero2WClient,
        volume_percent_to_gain,
    )

    client = Zero2WClient("ws://test")
    client._playback._playback_gain = 0.35  # .env startup fallback

    ws = _FakeWebSocket([json.dumps({
        "type": "HELLO_ACK",
        "payload": {
            "session_id": "sess_1",
            "audio_config": {"sample_rate": 24000, "volume": 30, "mic_gain": 60},
        },
    })])

    alsa = _FakeAlsaTool()
    with _patch_alsa(alsa):
        await client._handshake(ws)

    assert abs(client._playback.playback_gain - volume_percent_to_gain(30)) < 1e-9
    assert abs(client._audio_capture.input_gain - 0.6 * MAX_INPUT_GAIN) < 1e-9

    # Adopting a level is not the same as the hardware being at it: softvol
    # holds whatever it was last set to, across restarts. Push both, once.
    assert alsa.mixer_indexes == [
        gain_to_softvol_index(volume_percent_to_gain(30)),
        _MIC.gain_to_index(0.6 * MAX_INPUT_GAIN),
    ]

    # HELLO advertised the device's pre-adoption level so the app can sync too.
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "HELLO"
    assert sent["payload"]["volume"] == 59

    print("  PASS: test_handshake_adopts_app_levels")


async def _test_handshake_keeps_own_levels_when_app_sends_none():
    """No levels in HELLO_ACK -> the .env startup value stands, unchanged."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")
    client._playback._playback_gain = 0.35

    ws = _FakeWebSocket([json.dumps({
        "type": "HELLO_ACK",
        "payload": {"session_id": "s", "audio_config": {"sample_rate": 24000}},
    })])

    alsa = _FakeAlsaTool()
    with _patch_alsa(alsa):
        await client._handshake(ws)
        assert client._playback.playback_gain == 0.35

        # A malformed value is ignored rather than crashing the handshake.
        ws = _FakeWebSocket([json.dumps({
            "type": "HELLO_ACK",
            "payload": {"session_id": "s", "audio_config": {"volume": "loud"}},
        })])
        await client._handshake(ws)
        assert client._playback.playback_gain == 0.35

    # The .env levels are still pushed to ALSA both times: they are what the
    # device told the app it was at, so the hardware has to actually be there.
    default_mic = _MIC.gain_to_index(client._audio_capture.input_gain)
    assert alsa.mixer_indexes == [gain_to_softvol_index(0.35), default_mic] * 2

    print("  PASS: test_handshake_keeps_own_levels_when_app_sends_none")


async def _test_status_loop_silent_while_idle():
    """No DEVICE_STATUS at all while idle -- Phase 5a drops the every-10s
    radio wake for its own sake, since idle is most of the device's life."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")
    ws = _FakeWebSocket([])

    task = asyncio.create_task(client._status_loop(ws))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ws.sent == []
    print("  PASS: test_status_loop_silent_while_idle")


async def _test_status_loop_sends_immediately_on_recording_transitions():
    """A recording-state change wakes the loop immediately, so the app learns
    is_recording changed within one tick rather than up to
    STATUS_INTERVAL_SECONDS late."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")
    ws = _FakeWebSocket([])

    with patch("zero2w_client.STATUS_INTERVAL_SECONDS", 999):
        task = asyncio.create_task(client._status_loop(ws))
        await asyncio.sleep(0.01)
        assert ws.sent == []  # still idle, no timer-driven send

        client.is_recording = True
        client._status_event.set()
        await asyncio.sleep(0.01)
        assert len(ws.sent) == 1
        assert json.loads(ws.sent[0])["payload"]["is_recording"] is True

        client.is_recording = False
        client._status_event.set()
        await asyncio.sleep(0.01)
        assert len(ws.sent) == 2
        assert json.loads(ws.sent[1])["payload"]["is_recording"] is False

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    print("  PASS: test_status_loop_sends_immediately_on_recording_transitions")


async def _test_status_loop_sends_periodically_while_recording():
    """cpu_temp only matters mid-session, so recording keeps the old
    STATUS_INTERVAL_SECONDS cadence even with no new state-change events."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")
    ws = _FakeWebSocket([])
    client.is_recording = True

    with patch("zero2w_client.STATUS_INTERVAL_SECONDS", 0.02):
        task = asyncio.create_task(client._status_loop(ws))
        await asyncio.sleep(0.07)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ~0.07s at a 0.02s interval -> at least 3 sends, none event-triggered.
    assert len(ws.sent) >= 3
    assert all(json.loads(m)["payload"]["is_recording"] is True for m in ws.sent)
    print("  PASS: test_status_loop_sends_periodically_while_recording")


async def _test_start_and_stop_audio_trigger_status_event():
    """_start_audio/_stop_audio actually set _status_event -- the wiring
    _status_loop depends on to notice a recording transition at all."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")
    ws = MagicMock()
    ws.send = AsyncMock()
    client._audio_capture.start = AsyncMock()

    assert not client._status_event.is_set()
    await client._start_audio(ws, {"skip_calibration": True})
    assert client._status_event.is_set()

    client._status_event.clear()
    await client._stop_audio()
    assert client._status_event.is_set()

    print("  PASS: test_start_and_stop_audio_trigger_status_event")


async def _test_handshake_negotiates_binary_audio():
    """HELLO_ACK carrying negotiated_capabilities: [binary_audio] flips the
    client's send path to binary for subsequent AUDIO_FRAMEs."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")
    ws = _FakeWebSocket([json.dumps({
        "type": "HELLO_ACK",
        "payload": {
            "session_id": "s",
            "audio_config": {"sample_rate": 24000},
            "negotiated_capabilities": ["binary_audio"],
        },
    })])

    alsa = _FakeAlsaTool()
    with _patch_alsa(alsa):
        await client._handshake(ws)

    assert client._binary_audio_enabled is True
    print("  PASS: test_handshake_negotiates_binary_audio")


async def _test_handshake_without_negotiation_stays_json():
    """No negotiated_capabilities (today's app, or a Pi-5-shaped negotiation)
    leaves the client on JSON framing."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")
    ws = _FakeWebSocket([json.dumps({
        "type": "HELLO_ACK",
        "payload": {"session_id": "s", "audio_config": {"sample_rate": 24000}},
    })])

    alsa = _FakeAlsaTool()
    with _patch_alsa(alsa):
        await client._handshake(ws)

    assert client._binary_audio_enabled is False
    print("  PASS: test_handshake_without_negotiation_stays_json")


async def _test_audio_stream_loop_sends_binary_frame_when_negotiated():
    """Once binary_audio is negotiated, AUDIO_FRAME goes out as a packed
    header + raw PCM instead of base64-in-JSON."""
    from zero2w_client import AUDIO_FRAME_TAG, HEADER_VERSION, Zero2WClient

    client = Zero2WClient("ws://test")
    client._binary_audio_enabled = True
    client.is_recording = True
    client._stream_to_laptop = True

    chunk = b"\x11\x22\x33\x44" * 100
    client._audio_capture.read_chunk = _one_chunk_then_block(chunk)

    sent: list = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

    task = asyncio.create_task(client._audio_stream_loop(ws))
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(sent) == 1
    frame = sent[0]
    assert isinstance(frame, bytes)
    header = struct.Struct(">BBIQI")
    tag, version, seq, _capture_ms, reserved = header.unpack_from(frame, 0)
    assert tag == AUDIO_FRAME_TAG
    assert version == HEADER_VERSION
    assert seq == 1
    assert reserved == 0
    assert frame[header.size:] == chunk

    print("  PASS: test_audio_stream_loop_sends_binary_frame_when_negotiated")


async def _test_audio_stream_loop_sends_json_frame_when_not_negotiated():
    """Default (not negotiated) behavior is unchanged: base64-in-JSON."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")
    client.is_recording = True
    client._stream_to_laptop = True

    chunk = b"\xAA\xBB"
    client._audio_capture.read_chunk = _one_chunk_then_block(chunk)

    sent: list = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

    task = asyncio.create_task(client._audio_stream_loop(ws))
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(sent) == 1
    assert isinstance(sent[0], str)
    msg = json.loads(sent[0])
    assert msg["type"] == "AUDIO_FRAME"
    assert base64.b64decode(msg["payload"]["audio"]) == chunk

    print("  PASS: test_audio_stream_loop_sends_json_frame_when_not_negotiated")


async def _test_receive_loop_queues_binary_play_audio():
    """A binary PLAY_AUDIO frame through _receive_loop decodes correctly and
    reaches the playback queue, same shape as the JSON path.

    Pre-creates the queue and stubs out _ensure_playback_worker so nothing
    concurrently drains it -- this test is about the decode+dispatch, not the
    worker (covered elsewhere).
    """
    from zero2w_client import HEADER_VERSION, PLAY_AUDIO_TAG, Zero2WClient

    client = Zero2WClient("ws://test")
    client._playback_queue = asyncio.Queue()
    client._ensure_playback_worker = lambda: None

    pcm = b"\x01\x02\x03\x04"
    header = struct.pack(">BBIBII", PLAY_AUDIO_TAG, HEADER_VERSION, 5, 0x01, 100, 0)
    ws = _FakeWebSocket([header + pcm])

    await client._receive_loop(ws)

    _, payload = client._playback_queue.get_nowait()
    assert payload["audio"] == pcm
    assert payload["sequence_number"] == 5
    assert payload["is_final"] is True
    assert payload["duration_ms"] == 100

    print("  PASS: test_receive_loop_queues_binary_play_audio")


async def _test_receive_loop_drops_malformed_binary_frame():
    """A malformed binary frame is dropped silently -- no crash, and it never
    even reaches the playback worker."""
    from zero2w_client import Zero2WClient

    client = Zero2WClient("ws://test")
    called = {"value": False}
    client._ensure_playback_worker = lambda: called.__setitem__("value", True)

    ws = _FakeWebSocket([b"\x02"])  # too short to be a valid header
    await client._receive_loop(ws)  # must not raise

    assert called["value"] is False
    print("  PASS: test_receive_loop_drops_malformed_binary_frame")


def run_async_test(coro):
    asyncio.run(coro)


def main():
    sync_tests = [
        test_audio_frame_message_structure,
        test_audio_frame_base64_roundtrip,
        test_audio_frame_chunk_size,
        _test_gain_to_softvol_index_matches_measured_curve,
        _test_loud_source_cannot_be_boosted_into_clipping,
        _test_volume_percent_gain_roundtrip,
    ]
    async_tests = [
        _test_audio_capture_read_chunk,
        _test_apply_input_gain_drives_alsa_mixer,
        _test_mic_control_is_created_when_missing,
        _test_audio_capture_start_uses_arecord,
        _test_playback_manager_pipes_to_aplay,
        _test_playback_manager_writes_samples_untouched,
        _test_apply_playback_gain_drives_alsa_mixer,
        _test_mixer_control_is_created_when_missing,
        _test_playback_survives_a_missing_mixer,
        _test_streaming_chunks_then_finalize,
        _test_single_blob_still_works,
        _test_finalize_returns_correct_duration,
        _test_streaming_finalize_with_empty_final_chunk,
        _test_set_volume_updates_playback_gain,
        _test_set_volume_not_blocked_by_active_playback,
        _test_volume_change_applies_partway_through_a_response,
        _test_set_mic_gain_updates_input_gain,
        _test_set_mic_gain_not_blocked_by_active_playback,
        _test_mic_gain_change_does_not_restart_capture,
        _test_handshake_adopts_app_levels,
        _test_handshake_keeps_own_levels_when_app_sends_none,
        _test_skip_calibration_streams_immediately,
        _test_fresh_start_runs_calibration,
        _test_drain_buffered_audio_discards_backlog,
        _test_drain_buffered_audio_noop_when_idle,
        _test_drain_continuously_keeps_pipe_empty,
        _test_status_loop_silent_while_idle,
        _test_status_loop_sends_immediately_on_recording_transitions,
        _test_status_loop_sends_periodically_while_recording,
        _test_start_and_stop_audio_trigger_status_event,
        _test_handshake_negotiates_binary_audio,
        _test_handshake_without_negotiation_stays_json,
        _test_audio_stream_loop_sends_binary_frame_when_negotiated,
        _test_audio_stream_loop_sends_json_frame_when_not_negotiated,
        _test_receive_loop_queues_binary_play_audio,
        _test_receive_loop_drops_malformed_binary_frame,
    ]

    total = len(sync_tests) + len(async_tests)
    print(f"Running {total} tests...\n")

    passed = 0
    failed = 0

    for test in sync_tests:
        try:
            test()
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL: {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR: {test.__name__}: {exc}")
            failed += 1

    for test in async_tests:
        try:
            run_async_test(test())
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL: {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR: {test.__name__}: {exc}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed, {total} total")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

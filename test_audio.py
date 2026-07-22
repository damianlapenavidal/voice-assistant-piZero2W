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

from audio_capture import CAPTURE_CHANNELS, CHUNK_BYTES, AudioCapture
from audio_capture import _soft_limit as _capture_soft_limit
from audio_playback import BYTE_RATE, PlaybackManager, _apply_gain
from zero2w_client import make_audio_frame, parse_message


def _stereo_pcm(left_values: list[int], right_values: list[int]) -> bytes:
    """Build interleaved stereo PCM16 bytes: L0 R0 L1 R1 ..."""
    assert len(left_values) == len(right_values)
    interleaved = []
    for left, right in zip(left_values, right_values):
        interleaved.extend([left, right])
    return struct.pack(f"<{len(interleaved)}h", *interleaved)


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
    """read_chunk() reads stereo bytes but returns exactly CHUNK_BYTES (mono, left channel)."""
    n = CHUNK_BYTES // 2  # 2 bytes per sample
    stereo = _stereo_pcm(left_values=[100] * n, right_values=[999] * n)
    assert len(stereo) == CHUNK_BYTES * CAPTURE_CHANNELS

    fake_stdout = asyncio.StreamReader()
    fake_stdout.feed_data(stereo)
    fake_stdout.feed_eof()

    fake_process = MagicMock()
    fake_process.returncode = None
    fake_process.stdout = fake_stdout
    fake_process.stderr = asyncio.StreamReader()

    capture = AudioCapture(device="plughw:2,0")
    capture._process = fake_process
    capture._running = True

    chunk = await capture.read_chunk()
    assert chunk is not None
    assert len(chunk) == CHUNK_BYTES
    # Only the left channel's values (100) survive; the right channel (999) is dropped.
    assert set(struct.unpack(f"<{n}h", chunk)) == {100}
    assert capture._build_command()[-2:] == ["-D", "plughw:2,0"]

    print("  PASS: test_audio_capture_read_chunk")


async def _test_audio_capture_read_chunk_applies_input_gain():
    """read_chunk() scales left-channel samples by INPUT_GAIN, soft-limited at the ceiling."""
    n = CHUNK_BYTES // 2
    stereo = _stereo_pcm(left_values=[10000] * n, right_values=[0] * n)

    fake_stdout = asyncio.StreamReader()
    fake_stdout.feed_data(stereo)
    fake_stdout.feed_eof()

    fake_process = MagicMock()
    fake_process.returncode = None
    fake_process.stdout = fake_stdout
    fake_process.stderr = asyncio.StreamReader()

    capture = AudioCapture(input_gain=4.0)  # 10000 * 4 = 40000, well past the ceiling
    capture._process = fake_process
    capture._running = True

    chunk = await capture.read_chunk()
    values = set(struct.unpack(f"<{n}h", chunk))
    assert len(values) == 1
    (result,) = values
    knee = int(0.85 * 32767)
    assert knee < result <= 32767  # soft-limited, not hard-clipped flat

    print("  PASS: test_audio_capture_read_chunk_applies_input_gain")


async def _test_audio_capture_start_uses_arecord():
    """start() spawns arecord with S16_LE 24000 Hz, 2-channel (see CAPTURE_CHANNELS) raw format."""
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


def _test_apply_gain_scales_and_clips():
    """_apply_gain() scales PCM16 samples; overshoots soft-limit, never exceed ceiling."""
    knee = int(0.85 * 32767)  # _LIMITER_KNEE_FRACTION
    pcm = struct.pack("<3h", 1000, -1000, 20000)

    unity = _apply_gain(pcm, 1.0)
    assert unity == pcm  # no-op, same bytes (not just same values)

    scaled = _apply_gain(pcm, 2.0)
    values = struct.unpack("<3h", scaled)
    assert values[0] == 2000  # well under the knee -> untouched linear scaling
    assert values[1] == -2000
    # 20000 * 2.0 = 40000, far past the ceiling. Soft-limited: bounded at
    # 32767, but for this finite an overshoot strictly under it too --
    # proves it's compressed smoothly, not hard-clipped flat at the wall.
    assert knee < values[2] < 32767

    # An extreme overshoot still never exceeds the int16 ceiling.
    extreme = _apply_gain(struct.pack("<1h", 20000), 20.0)
    assert struct.unpack("<1h", extreme)[0] <= 32767

    print("  PASS: test_apply_gain_scales_and_clips")


def _test_soft_limit_shape():
    """_soft_limit(): passthrough below the knee, smooth + bounded above it."""
    knee = int(0.85 * 32767)

    # Below the knee: exact passthrough, both signs.
    assert _capture_soft_limit(1000.0) == 1000
    assert _capture_soft_limit(-1000.0) == -1000
    assert _capture_soft_limit(float(knee)) == knee

    # Just past the knee: compressed, but only slightly -- still very close
    # to the input value, not snapped straight to the ceiling.
    just_over = _capture_soft_limit(knee + 500.0)
    assert knee < just_over < knee + 500

    # Monotonic: a louder input never produces a quieter output.
    prev = 0
    for magnitude in (0, 5000, 20000, 32767, 50000, 100000):
        result = _capture_soft_limit(float(magnitude))
        assert result >= prev
        prev = result

    # No finite input ever reaches or exceeds the true ceiling.
    assert _capture_soft_limit(1_000_000.0) <= 32767

    print("  PASS: test_soft_limit_shape")


def _test_openai_style_loud_source_does_not_hard_clip():
    """Reproduces the reported bug: an already near-full-scale source (like
    OpenAI's normalized TTS output) combined with gain left over from tuning
    a much quieter source (loopback's raw mic echo) must round off smoothly,
    not produce a run of samples hard-clipped flat at the ceiling.
    """
    # Simulate "hot" TTS-style audio already near full scale.
    loud_source = struct.pack("<4h", 30000, -30000, 31000, -31000)

    # Gain leftover from tuning the (much quieter) loopback source.
    over_driven = _apply_gain(loud_source, 2.5)
    values = struct.unpack("<4h", over_driven)

    # Every sample stays within range...
    assert all(-32768 <= v <= 32767 for v in values)
    # ...but a hard-clip bug would flatten ALL of these to exactly the
    # ceiling/floor (since every input here is already past the knee once
    # scaled by 2.5x). The soft limiter must not do that.
    assert len(set(values)) > 1, "all samples flattened to the same value -- hard clipping regression"

    print("  PASS: test_openai_style_loud_source_does_not_hard_clip")


async def _test_playback_manager_applies_playback_gain():
    """play_pcm16_chunk() scales bytes by playback_gain before writing to aplay."""
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
    ):
        playback = PlaybackManager(playback_gain=0.5)
        pcm = struct.pack("<2h", 1000, -1000)
        await playback.play_pcm16_chunk(pcm)

        written = fake_stdin.write.call_args[0][0]
        assert struct.unpack("<2h", written) == (500, -500)

    print("  PASS: test_playback_manager_applies_playback_gain")


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

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m

    async def send(self, msg: str) -> None:
        self.sent.append(msg)


async def _test_set_volume_updates_playback_gain():
    """SET_VOLUME (0-100) maps onto [0, MAX_PLAYBACK_GAIN] so 100% can boost
    above unity -- loopback echoes the raw, unnormalized (quiet) mic signal,
    so unity alone isn't enough headroom."""
    from zero2w_client import MAX_PLAYBACK_GAIN, Zero2WClient

    client = Zero2WClient("ws://test")

    ws = _FakeWebSocket([json.dumps({"type": "SET_VOLUME", "payload": {"volume": 70}})])
    await client._receive_loop(ws)
    assert abs(client._playback.playback_gain - 0.7 * MAX_PLAYBACK_GAIN) < 1e-9

    # Out-of-range values are clamped, not rejected.
    ws = _FakeWebSocket([json.dumps({"type": "SET_VOLUME", "payload": {"volume": 150}})])
    await client._receive_loop(ws)
    assert abs(client._playback.playback_gain - MAX_PLAYBACK_GAIN) < 1e-9

    print("  PASS: test_set_volume_updates_playback_gain")


async def _test_set_mic_gain_updates_input_gain():
    """SET_MIC_GAIN (0-100) maps onto [0, MAX_INPUT_GAIN], applied live to capture."""
    from zero2w_client import MAX_INPUT_GAIN, Zero2WClient

    client = Zero2WClient("ws://test")

    ws = _FakeWebSocket([json.dumps({"type": "SET_MIC_GAIN", "payload": {"gain": 40}})])
    await client._receive_loop(ws)
    assert abs(client._audio_capture.input_gain - 0.4 * MAX_INPUT_GAIN) < 1e-9

    # Out-of-range values are clamped, not rejected.
    ws = _FakeWebSocket([json.dumps({"type": "SET_MIC_GAIN", "payload": {"gain": 150}})])
    await client._receive_loop(ws)
    assert abs(client._audio_capture.input_gain - MAX_INPUT_GAIN) < 1e-9

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
    # Feed far more than one CHUNK_BYTES*CAPTURE_CHANNELS read would consume,
    # simulating arecord producing data continuously in real time.
    fake_stdout.feed_data(b"\x22" * (CHUNK_BYTES * CAPTURE_CHANNELS * 5))

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


def run_async_test(coro):
    asyncio.run(coro)


def main():
    sync_tests = [
        test_audio_frame_message_structure,
        test_audio_frame_base64_roundtrip,
        test_audio_frame_chunk_size,
        _test_apply_gain_scales_and_clips,
        _test_soft_limit_shape,
        _test_openai_style_loud_source_does_not_hard_clip,
    ]
    async_tests = [
        _test_audio_capture_read_chunk,
        _test_audio_capture_read_chunk_applies_input_gain,
        _test_audio_capture_start_uses_arecord,
        _test_playback_manager_pipes_to_aplay,
        _test_playback_manager_applies_playback_gain,
        _test_streaming_chunks_then_finalize,
        _test_single_blob_still_works,
        _test_finalize_returns_correct_duration,
        _test_streaming_finalize_with_empty_final_chunk,
        _test_set_volume_updates_playback_gain,
        _test_set_mic_gain_updates_input_gain,
        _test_skip_calibration_streams_immediately,
        _test_fresh_start_runs_calibration,
        _test_drain_buffered_audio_discards_backlog,
        _test_drain_buffered_audio_noop_when_idle,
        _test_drain_continuously_keeps_pipe_empty,
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

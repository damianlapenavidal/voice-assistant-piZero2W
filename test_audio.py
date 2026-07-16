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

from audio_capture import CHUNK_BYTES, AudioCapture
from audio_playback import BYTE_RATE, PlaybackManager
from zero2w_client import make_audio_frame, parse_message


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
    """read_chunk() returns exactly CHUNK_BYTES from mocked arecord stdout."""
    fake_stdout = asyncio.StreamReader()
    fake_stdout.feed_data(b"\x01\x02" * (CHUNK_BYTES // 2))
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
    assert capture._build_command()[-2:] == ["-D", "plughw:2,0"]

    print("  PASS: test_audio_capture_read_chunk")


async def _test_audio_capture_start_uses_arecord():
    """start() spawns arecord with S16_LE 24000 Hz mono raw format."""
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
        assert "-c" in cmd and "1" in cmd
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
    ]
    async_tests = [
        _test_audio_capture_read_chunk,
        _test_audio_capture_start_uses_arecord,
        _test_playback_manager_pipes_to_aplay,
        _test_streaming_chunks_then_finalize,
        _test_single_blob_still_works,
        _test_finalize_returns_correct_duration,
        _test_streaming_finalize_with_empty_final_chunk,
        _test_skip_calibration_streams_immediately,
        _test_fresh_start_runs_calibration,
        _test_drain_buffered_audio_discards_backlog,
        _test_drain_buffered_audio_noop_when_idle,
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

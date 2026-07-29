#!/usr/bin/env python3
"""
Tests for the Pi Zero 2W client's message creation and parsing.

Run with: python test_client.py
No external dependencies required beyond the standard library.
"""

import base64
import json
import struct
import sys
from datetime import datetime
from pathlib import Path

# Add device/ to path so we can import zero2w_client
sys.path.insert(0, str(Path(__file__).parent))

from zero2w_client import (
    AUDIO_FRAME_TAG,
    HEADER_VERSION,
    PLAY_AUDIO_TAG,
    as_pcm_bytes,
    decode_play_audio_binary,
    get_device_id,
    make_audio_frame,
    make_device_status,
    make_hello,
    make_message,
    make_pong,
    parse_message,
)


def test_hello_message():
    """HELLO message has correct structure and fields."""
    raw = make_hello()
    msg = json.loads(raw)

    assert msg["type"] == "HELLO", f"Expected type HELLO, got {msg['type']}"
    assert "timestamp" in msg, "Missing timestamp"
    assert "payload" in msg, "Missing payload"

    payload = msg["payload"]
    assert payload["device_id"] == get_device_id()
    assert payload["device_type"] == "pi_zero_2w"
    assert payload["firmware_version"] == "0.1.0"
    assert "audio_capture" in payload["capabilities"]
    assert "audio_playback" in payload["capabilities"]
    assert "binary_audio" in payload["capabilities"]

    print("  PASS: test_hello_message")


def test_device_status_message():
    """DEVICE_STATUS message has correct structure."""
    raw = make_device_status(is_recording=False)
    msg = json.loads(raw)

    assert msg["type"] == "DEVICE_STATUS"
    assert "timestamp" in msg

    payload = msg["payload"]
    assert payload["battery_percent"] is None
    assert isinstance(payload["cpu_temp"], (float, int, type(None)))
    assert payload["is_recording"] is False
    assert isinstance(payload["uptime_seconds"], (float, int))
    assert payload["uptime_seconds"] >= 0

    print("  PASS: test_device_status_message")


def test_device_status_recording():
    """DEVICE_STATUS reflects is_recording=True."""
    raw = make_device_status(is_recording=True)
    msg = json.loads(raw)

    assert msg["payload"]["is_recording"] is True

    print("  PASS: test_device_status_recording")


def test_pong_message():
    """PONG message echoes the original ping timestamp."""
    ping_ts = "2026-06-30T15:30:00.000Z"
    raw = make_pong(ping_ts)
    msg = json.loads(raw)

    assert msg["type"] == "PONG"
    assert msg["payload"]["timestamp"] == ping_ts

    print("  PASS: test_pong_message")


def test_make_message_generic():
    """make_message produces valid JSON with expected fields."""
    raw = make_message("START_AUDIO_STREAM")
    msg = json.loads(raw)

    assert msg["type"] == "START_AUDIO_STREAM"
    assert msg["payload"] == {}
    assert "timestamp" in msg

    print("  PASS: test_make_message_generic")


def test_parse_message():
    """parse_message correctly deserializes a JSON message."""
    original = {
        "type": "SET_VOLUME",
        "payload": {"volume": 75},
        "timestamp": "2026-06-30T15:00:02.000Z",
    }
    raw = json.dumps(original)
    parsed = parse_message(raw)

    assert parsed["type"] == "SET_VOLUME"
    assert parsed["payload"]["volume"] == 75
    assert parsed["timestamp"] == "2026-06-30T15:00:02.000Z"

    print("  PASS: test_parse_message")


def test_message_format_matches_protocol():
    """Verify the message structure matches the protocol spec exactly.

    Protocol requires: {"type": "...", "payload": {...}, "timestamp": "ISO8601"}
    """
    raw = make_hello()
    msg = json.loads(raw)

    expected_keys = {"type", "payload", "timestamp"}
    actual_keys = set(msg.keys())
    assert actual_keys == expected_keys, (
        f"Message keys {actual_keys} don't match expected {expected_keys}"
    )

    # Timestamp should be ISO 8601 format
    ts = msg["timestamp"]
    assert "T" in ts, f"Timestamp doesn't look like ISO 8601: {ts}"
    assert ts.endswith("+00:00") or ts.endswith("Z"), (
        f"Timestamp should be UTC: {ts}"
    )

    print("  PASS: test_message_format_matches_protocol")


def test_hello_ack_parsing():
    """Verify we can parse a HELLO_ACK from the server."""
    hello_ack = json.dumps({
        "type": "HELLO_ACK",
        "payload": {
            "session_id": "sess_test_123",
            "audio_config": {
                "sample_rate": 24000,
                "format": "pcm16",
                "channels": 1,
            },
        },
        "timestamp": "2026-06-30T15:00:00.050Z",
    })

    msg = parse_message(hello_ack)
    assert msg["type"] == "HELLO_ACK"
    assert msg["payload"]["session_id"] == "sess_test_123"
    assert msg["payload"]["audio_config"]["sample_rate"] == 24000

    print("  PASS: test_hello_ack_parsing")


def test_command_parsing():
    """Verify we can parse all command types from the server."""
    commands = [
        {"type": "START_AUDIO_STREAM", "payload": {}, "timestamp": "2026-06-30T15:00:01.000Z"},
        {"type": "STOP_AUDIO_STREAM", "payload": {}, "timestamp": "2026-06-30T15:05:00.000Z"},
        {"type": "SET_VOLUME", "payload": {"volume": 50}, "timestamp": "2026-06-30T15:00:02.000Z"},
        {"type": "SHUTDOWN_DEVICE", "payload": {}, "timestamp": "2026-06-30T16:00:00.000Z"},
        {"type": "PING", "payload": {"timestamp": "2026-06-30T15:30:00.000Z"}, "timestamp": "2026-06-30T15:30:00.000Z"},
    ]

    for cmd in commands:
        raw = json.dumps(cmd)
        parsed = parse_message(raw)
        assert parsed["type"] == cmd["type"], f"Type mismatch for {cmd['type']}"

    print("  PASS: test_command_parsing")


def test_make_audio_frame_json_default():
    """make_audio_frame without binary=True is unchanged: base64-in-JSON."""
    raw = make_audio_frame(b"\x01\x02\x03\x04", 7, "2026-07-29T12:00:00+00:00")
    assert isinstance(raw, str)
    msg = json.loads(raw)

    assert msg["type"] == "AUDIO_FRAME"
    assert msg["payload"]["sequence_number"] == 7
    assert msg["payload"]["timestamp"] == "2026-07-29T12:00:00+00:00"
    assert base64.b64decode(msg["payload"]["audio"]) == b"\x01\x02\x03\x04"

    print("  PASS: test_make_audio_frame_json_default")


def test_make_audio_frame_binary_round_trip():
    """binary=True packs the exact header layout docs/protocol.md defines."""
    pcm = b"\xAA\xBB\xCC\xDD\xEE"
    ts = "2026-07-29T12:00:00+00:00"
    frame = make_audio_frame(pcm, 12345, ts, binary=True)

    assert isinstance(frame, bytes)
    header = struct.Struct(">BBIQI")
    tag, version, seq, capture_ms, reserved = header.unpack_from(frame, 0)

    assert tag == AUDIO_FRAME_TAG
    assert version == HEADER_VERSION
    assert seq == 12345
    assert reserved == 0
    expected_ms = int(datetime.fromisoformat(ts).timestamp() * 1000)
    assert capture_ms == expected_ms
    assert frame[header.size:] == pcm

    print("  PASS: test_make_audio_frame_binary_round_trip")


def test_decode_play_audio_binary_valid():
    """A well-formed binary PLAY_AUDIO frame decodes to the JSON-equivalent
    payload shape _handle_play_audio already expects."""
    pcm = b"\x01\x02\x03"
    header = struct.pack(">BBIBII", PLAY_AUDIO_TAG, HEADER_VERSION, 42, 0x01, 250, 0)
    msg = decode_play_audio_binary(header + pcm)

    assert msg["type"] == "PLAY_AUDIO"
    payload = msg["payload"]
    assert payload["audio"] == pcm
    assert payload["sequence_number"] == 42
    assert payload["is_final"] is True
    assert payload["duration_ms"] == 250

    print("  PASS: test_decode_play_audio_binary_valid")


def test_decode_play_audio_binary_duration_unknown_sentinel():
    """0xFFFFFFFF duration means 'unknown', preserving int | None."""
    header = struct.pack(">BBIBII", PLAY_AUDIO_TAG, HEADER_VERSION, 1, 0x00, 0xFFFFFFFF, 0)
    msg = decode_play_audio_binary(header)

    assert msg["payload"]["duration_ms"] is None
    assert msg["payload"]["is_final"] is False
    assert msg["payload"]["audio"] == b""

    print("  PASS: test_decode_play_audio_binary_duration_unknown_sentinel")


def test_decode_play_audio_binary_malformed_frames_are_dropped():
    """Each malformed case returns None (logged) rather than raising -- a
    bad frame must never crash the session."""
    valid_header = struct.pack(">BBIBII", PLAY_AUDIO_TAG, HEADER_VERSION, 1, 0, 0, 0)

    too_short = b"\x02"
    wrong_tag = struct.pack(">BBIBII", 0x99, HEADER_VERSION, 1, 0, 0, 0)
    wrong_version = struct.pack(">BBIBII", PLAY_AUDIO_TAG, 99, 1, 0, 0, 0)
    truncated = valid_header[:5]

    for case in (too_short, wrong_tag, wrong_version, truncated):
        assert decode_play_audio_binary(case) is None

    print("  PASS: test_decode_play_audio_binary_malformed_frames_are_dropped")


def test_as_pcm_bytes_handles_both_representations():
    """as_pcm_bytes normalizes base64 str (JSON form), raw bytes (binary
    form), and None identically."""
    pcm = b"\x01\x02\x03\x04"
    assert as_pcm_bytes(base64.b64encode(pcm).decode("ascii")) == pcm
    assert as_pcm_bytes(pcm) == pcm
    assert as_pcm_bytes(bytearray(pcm)) == pcm
    assert as_pcm_bytes(None) == b""

    print("  PASS: test_as_pcm_bytes_handles_both_representations")


def main():
    tests = [
        test_hello_message,
        test_device_status_message,
        test_device_status_recording,
        test_pong_message,
        test_make_message_generic,
        test_parse_message,
        test_message_format_matches_protocol,
        test_hello_ack_parsing,
        test_command_parsing,
        test_make_audio_frame_json_default,
        test_make_audio_frame_binary_round_trip,
        test_decode_play_audio_binary_valid,
        test_decode_play_audio_binary_duration_unknown_sentinel,
        test_decode_play_audio_binary_malformed_frames_are_dropped,
        test_as_pcm_bytes_handles_both_representations,
    ]

    print(f"Running {len(tests)} tests...\n")
    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {test.__name__}: {e}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed, {len(tests)} total")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

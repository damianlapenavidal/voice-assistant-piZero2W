#!/usr/bin/env python3
"""
Tests for the Pi Zero 2W client's message creation and parsing.

Run with: python test_client.py
No external dependencies required beyond the standard library.
"""

import json
import sys
from pathlib import Path

# Add device/ to path so we can import zero2w_client
sys.path.insert(0, str(Path(__file__).parent))

from zero2w_client import (
    make_hello,
    make_device_status,
    make_pong,
    make_message,
    parse_message,
    get_device_id,
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

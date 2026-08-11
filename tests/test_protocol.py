"""Tests for elro_connects_k2_protocol.protocol — framing, builders."""

from __future__ import annotations

import json

import pytest

from elro_connects_k2_protocol.protocol import (
    build_ack,
    build_activation,
    build_app_send,
    build_discovery,
    decrypt_message,
    encrypt_message,
)


def test_encrypt_decrypt_roundtrip() -> None:
    original = '{"action":"IOT_KEY?","devID":"NULL"}'
    encrypted = encrypt_message(original)
    text, obj = decrypt_message(encrypted)
    assert text == original
    assert obj == {"action": "IOT_KEY?", "devID": "NULL"}


def test_different_seeds_are_used() -> None:
    msg = '{"action":"test"}'
    results = {encrypt_message(msg) for _ in range(50)}
    # Very unlikely to produce the same seed 50 times in a row
    assert len(results) > 1


def test_any_valid_seed_decrypts_correctly() -> None:
    msg = '{"action":"APP_SEND","devID":"X"}'
    # Manually encrypt with every possible seed and check decrypt
    for seed in range(256):
        key = seed ^ 0x23
        packet = bytes([seed]) + bytes(b ^ key for b in msg.encode())
        text, obj = decrypt_message(packet)
        assert text == msg, f"Failed for seed {seed}"
        assert obj is not None


def test_decrypt_empty_packet() -> None:
    text, obj = decrypt_message(b"")
    assert text == ""
    assert obj is None


def test_decrypt_garbage() -> None:
    text, _obj = decrypt_message(bytes(range(10)))
    # Should not raise; obj may be None
    assert isinstance(text, str)


def test_trim_json_stops_at_first_object() -> None:
    # The app trims on the first closing brace — anything after is noise
    msg = '{"action":"test"}garbage_after'
    encrypted = encrypt_message(msg)
    _text, obj = decrypt_message(encrypted)
    assert obj == {"action": "test"}


def test_trim_json_nested_object() -> None:
    msg = '{"action":"APP_SEND","msg":{"CMD_CODE":12}}'
    encrypted = encrypt_message(msg)
    _text, obj = decrypt_message(encrypted)
    assert obj is not None
    assert obj["msg"]["CMD_CODE"] == 12


@pytest.mark.parametrize("builder,args", [
    (build_discovery, ()),
    (build_discovery, ("MY_DEVICE",)),
    (build_activation, ("MY_DEVICE",)),
    (build_app_send, ("DEV", 1, 54, "00020000", "000200", "")),
    (build_ack, ("DEV", 42, 11)),
])
def test_builders_produce_valid_json(builder, args) -> None:  # type: ignore[no-untyped-def]
    result = builder(*args)
    parsed = json.loads(result)
    assert isinstance(parsed, dict)


def test_build_discovery_default_dev_id() -> None:
    obj = json.loads(build_discovery())
    assert obj["devID"] == "NULL"
    assert obj["action"] == "IOT_KEY?"


def test_build_app_send_shape() -> None:
    obj = json.loads(build_app_send("ST_123", 7, 54, "00020000", "000200", ""))
    assert obj["action"] == "APP_SEND"
    assert obj["devID"] == "ST_123"
    assert obj["msg"]["CMD_CODE"] == 54
    assert obj["msg"]["msg_ID"] == 7
    assert obj["msg"]["rev_str1"] == "00020000"


def test_build_ack_shape() -> None:
    obj = json.loads(build_ack("ST_123", 99, 55))
    assert obj["action"] == "APP_ACK"
    assert obj["msg"]["CMD_CODE"] == 11
    assert obj["msg"]["rev_str1"] == "55"
    assert obj["msg"]["rev_str2"] == "OK"

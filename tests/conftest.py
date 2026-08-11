"""Shared pytest fixtures and golden-test helpers."""

from __future__ import annotations

import json
import pathlib
from typing import Any

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES_DIR / name).read_text())
    return data


def all_parser_fixtures() -> list[tuple[str, dict[str, Any]]]:
    """Return (filename, fixture_dict) for every fixture that has a parser field."""
    result = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        data: dict[str, Any] = json.loads(path.read_text())
        if "parser" in data:
            result.append((path.name, data))
    return result


def assert_device_matches(device: Any, expected: dict[str, Any]) -> None:
    """Assert that a SubDevice's fields match the expected dict from a fixture.

    Enum fields are compared by .name so fixtures can use plain strings like "CLEAR".
    """
    from enum import Enum
    for field, value in expected.items():
        if field == "profile_name":
            actual: Any = device.profile.name
        else:
            actual = getattr(device, field)
        # Compare enums by name so fixtures don't need to import Python types
        if isinstance(actual, Enum):
            actual = actual.name
        assert actual == value, (
            f"Field {field!r}: expected {value!r}, got {actual!r}"
        )

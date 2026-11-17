"""Shared manifest-building helpers for the C2 manifest suites (not a test module).

``test_tec_manifest.py`` and ``test_tec_manifest_invalid.py`` both start from a valid manifest
(the design C2 initial content as a Python mapping), mutate one key, write it under ``tmp_path``
and load it. This directory has no ``__init__.py`` (design Risk R8); pytest's default ``prepend``
import mode puts it on ``sys.path``, so the test modules import this file by its unique name.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

_BASE: dict[str, Any] = {
    "slug": "inheco-tec",
    "title": "Inheco TEC",
    "hosting": "host-resident",
    "host": "192.0.2.10",
    "port": 8743,
    "protocol": {"api": "https-rest", "instrument": "usb-hid"},
    "auth": {"bearer_token_ref": "INHECO_TEC_API_TOKEN"},  # pragma: allowlist secret
    "tls": {
        "cert_file": "C:/ProgramData/instrument-apis/inheco-tec/tls/server.crt",
        "key_file": "C:/ProgramData/instrument-apis/inheco-tec/tls/server.key",
    },
    "service": {
        "bind": "0.0.0.0",
        "env_file": "C:/ProgramData/instrument-apis/inheco-tec/secrets/service.env",
        "state_file": "C:/ProgramData/instrument-apis/inheco-tec/state/state.json",
        "shutdown_timeout_s": 10,
    },
    "transport": {
        "kind": "hidapi",
        "pre_send_delay_ms": 200,
        "read_timeout_ms": 3000,
        "retry_delay_ms": 1000,
        "max_attempts": 3,
        "max_stale_reads": 3,
        "lock_wait_s": 16,
    },
    "article_numbers": {
        "7000179": {"class": "cpac", "variant": "CPAC Microplate"},
        "7000190": {"class": "cpac", "variant": "CPAC Ultraflat"},
        "7000193": {"class": "cpac", "variant": "CPAC Ultraflat"},
        "7000163": {"class": "cpac-2tec", "variant": "CPAC Microplate HT 2TEC"},
        "7000166": {"class": "cpac-2tec", "variant": "CPAC Ultraflat HT 2TEC"},
        "7000165": {"class": "cpac-2tec", "variant": "CPAC Ultraflat HT 2TEC"},
    },
    "controllers": [
        {
            "id": 0,
            "slots": [
                {"id": 1, "class": "cpac", "min_c": 4.0, "max_c": 70.0,
                 "at_target_tolerance_c": 1.0},
                {"id": 2, "class": "cpac", "min_c": 4.0, "max_c": 70.0,
                 "at_target_tolerance_c": 1.0},
            ],
        }
    ],
    "capabilities": ["status", "connect", "set-target", "start", "stop"],
    "refs": {"linear_issue": "<issue-id>", "vault": "<knowledge-base-ref>", "notion_hub": ""},
}  # fmt: skip

EXPECTED_ARTICLES: dict[str, tuple[str, str]] = {
    "7000179": ("cpac", "CPAC Microplate"),
    "7000190": ("cpac", "CPAC Ultraflat"),
    "7000193": ("cpac", "CPAC Ultraflat"),
    "7000163": ("cpac-2tec", "CPAC Microplate HT 2TEC"),
    "7000166": ("cpac-2tec", "CPAC Ultraflat HT 2TEC"),
    "7000165": ("cpac-2tec", "CPAC Ultraflat HT 2TEC"),
}
"""The exact FR-2.7 article-number map: key -> (limit class, variant)."""


def base_manifest() -> dict[str, Any]:
    """A fresh deep copy of a valid manifest mapping."""
    return copy.deepcopy(_BASE)


def slot(data: dict[str, Any], c_index: int = 0, s_index: int = 0) -> dict[str, Any]:
    """The slot mapping at ``controllers[c_index].slots[s_index]`` of ``data``."""
    result: dict[str, Any] = data["controllers"][c_index]["slots"][s_index]
    return result


def write_manifest(directory: Path, data: dict[str, Any], name: str = "instrument.yaml") -> Path:
    """Dump ``data`` as YAML under ``directory`` and return the file path.

    ``yaml.safe_dump`` quotes digit-only string keys, so article keys stay strings.
    """
    path = directory / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def write_text(directory: Path, text: str, name: str = "instrument.yaml") -> Path:
    """Write raw manifest ``text`` under ``directory`` and return the file path."""
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def mutated(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """A valid manifest with ``change`` applied in place."""
    data = base_manifest()
    change(data)
    return data

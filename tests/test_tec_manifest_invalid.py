"""Fail-closed tests for C2 ``load_manifest`` (Task 5; FR-2.5, FR-2.8, FR-5.2, FR-13.4, NFR-1).

Each invalid variant must raise ``ManifestError`` whose ``key_path`` names the offending key, and
no error text may carry the offending input value (design C2: "no secret value is ever in the
error"). Split from ``test_tec_manifest.py`` for the 500-line module limit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from inheco_tec_api.manifest import (
    MANIFEST_ENV_VAR,
    TRANSPORT_ENV_VAR,
    ManifestError,
    load_manifest,
)
from tec_manifest_helpers import base_manifest, mutated, slot, write_manifest, write_text

S0 = "controllers[0].slots[0]"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MANIFEST_ENV_VAR, raising=False)
    monkeypatch.delenv(TRANSPORT_ENV_VAR, raising=False)


def _load_error(path: Path) -> ManifestError:
    with pytest.raises(ManifestError) as info:
        load_manifest(path)
    return info.value


def _set_slot(**values: Any) -> dict[str, Any]:
    return mutated(lambda d: slot(d).update(values))


def _set_transport(**values: Any) -> dict[str, Any]:
    return mutated(lambda d: d["transport"].update(values))


def _add_article(key: str) -> dict[str, Any]:
    return mutated(lambda d: d["article_numbers"].update({key: {"class": "cpac", "variant": "x"}}))


def _set_ref(name: str) -> dict[str, Any]:
    return mutated(lambda d: d["auth"].update({"bearer_token_ref": name}))


def _base_text() -> str:
    return yaml.safe_dump(base_manifest(), sort_keys=False)


# --- Mapping variants (FR-2.5, FR-2.8, FR-5.2, FR-2.4) ---

_DUP_SLOT: dict[str, Any] = mutated(lambda d: d["controllers"][0]["slots"][1].update(id=1))
_DUP_CONTROLLER: dict[str, Any] = mutated(
    lambda d: d["controllers"].append({"id": 0, "slots": [{"id": 3, "class": "cpac"}]})
)

MAPPING_VARIANTS = [
    pytest.param(mutated(lambda d: d.update(colour="blue")), "colour", id="unknown-top-key"),
    pytest.param(_set_slot(colour="blue"), f"{S0}.colour", id="unknown-slot-key"),
    pytest.param(
        mutated(lambda d: d["transport"].update(speed=1)),
        "transport.speed",
        id="unknown-transport-key",
    ),
    pytest.param(
        mutated(lambda d: d["controllers"][0].update(id=8)),
        "controllers[0].id",
        id="controller-id-8",
    ),
    pytest.param(
        mutated(lambda d: d["controllers"][0].update(id=-1)),
        "controllers[0].id",
        id="controller-id-neg",
    ),
    pytest.param(_DUP_CONTROLLER, "controllers[1].id", id="duplicate-controller"),
    pytest.param(_set_slot(id=0), f"{S0}.id", id="slot-id-0"),
    pytest.param(_set_slot(id=7), f"{S0}.id", id="slot-id-7"),
    pytest.param(_DUP_SLOT, "controllers[0].slots[1].id", id="duplicate-slot"),
    pytest.param(_set_slot(**{"class": "cpac-3tec"}), f"{S0}.class", id="unknown-class"),
    pytest.param(_set_slot(min_c=50.0, max_c=40.0), f"{S0}.min_c", id="min-gt-max"),
    pytest.param(_set_slot(max_c=70.1), f"{S0}.max_c", id="cpac-max-70.1"),
    pytest.param(_set_slot(min_c=3.9), f"{S0}.min_c", id="cpac-min-3.9"),
    pytest.param(
        _set_slot(**{"class": "cpac-2tec", "max_c": 115.0}), f"{S0}.max_c", id="cpac-2tec-max-115"
    ),
    pytest.param(
        _set_slot(**{"class": "cpac-2tec", "max_c": 110.1}), f"{S0}.max_c", id="cpac-2tec-max-110.1"
    ),
    pytest.param(_set_slot(max_c=45.05), f"{S0}.max_c", id="not-tenths"),
    pytest.param(
        _set_slot(at_target_tolerance_c=0.05), f"{S0}.at_target_tolerance_c", id="tol-low"
    ),
    pytest.param(
        _set_slot(at_target_tolerance_c=5.1), f"{S0}.at_target_tolerance_c", id="tol-high"
    ),
    pytest.param(
        _add_article("700017"),
        "article_numbers.700017",
        id="article-6-digits",
    ),
    pytest.param(
        _add_article("70001790"),
        "article_numbers.70001790",
        id="article-8-digits",
    ),
    pytest.param(
        _add_article("700017a"),
        "article_numbers.700017a",
        id="article-non-digit",
    ),
    pytest.param(
        mutated(lambda d: d["article_numbers"]["7000179"].update({"class": "cpac-hd"})),
        "article_numbers.7000179.class",
        id="article-bad-class",
    ),
    pytest.param(
        _set_transport(pre_send_delay_ms=99), "transport.pre_send_delay_ms", id="pre-send-99-hidapi"
    ),
    pytest.param(
        _set_transport(pre_send_delay_ms=0, retry_delay_ms=0),
        "transport.pre_send_delay_ms",
        id="zero-delays-hidapi",
    ),
    pytest.param(
        _set_transport(retry_delay_ms=0), "transport.retry_delay_ms", id="zero-retry-hidapi"
    ),
    pytest.param(
        _set_transport(retry_delay_ms=999), "transport.retry_delay_ms", id="retry-999-hidapi"
    ),
    pytest.param(_set_transport(max_attempts=12), "transport.max_attempts", id="max-attempts-12"),
    pytest.param(_set_transport(max_attempts=0), "transport.max_attempts", id="max-attempts-0"),
    pytest.param(
        _set_transport(read_timeout_ms=499), "transport.read_timeout_ms", id="timeout-499"
    ),
    pytest.param(_set_transport(max_stale_reads=11), "transport.max_stale_reads", id="stale-11"),
    pytest.param(_set_transport(lock_wait_s=121), "transport.lock_wait_s", id="lock-wait-121"),
    pytest.param(_set_transport(kind="serial"), "transport.kind", id="transport-kind-unknown"),
    pytest.param(
        _set_ref("inheco-tec-token"),
        "auth.bearer_token_ref",
        id="token-ref-bad-shape",
    ),
    pytest.param(
        _set_ref("1TOKEN"),
        "auth.bearer_token_ref",
        id="token-ref-leading-digit",
    ),
    pytest.param(
        _set_ref(""),
        "auth.bearer_token_ref",
        id="token-ref-empty",
    ),
    pytest.param(
        mutated(lambda d: d["tls"].update(cert_file="")), "tls.cert_file", id="cert-empty"
    ),
    pytest.param(mutated(lambda d: d["tls"].update(key_file="")), "tls.key_file", id="key-empty"),
    pytest.param(mutated(lambda d: d.pop("auth")), "auth", id="missing-auth"),
]


@pytest.mark.parametrize(("data", "key_path"), MAPPING_VARIANTS)
def test_fr2_5_invalid_variant_raises_naming_key(
    tmp_path: Path, data: dict[str, Any], key_path: str
) -> None:
    error = _load_error(write_manifest(tmp_path, data))
    assert error.key_path == key_path
    assert str(error).startswith(f"{key_path}: ")
    assert error.reason


# --- Whole-file and raw-text variants (FR-2.5, FR-2.8) ---


def test_fr2_5_missing_file_raises_manifest_key(tmp_path: Path) -> None:
    error = _load_error(tmp_path / "absent.yaml")
    assert error.key_path == "manifest"
    assert "not found" in error.reason


def test_fr2_5_bad_yaml_raises_manifest_key(tmp_path: Path) -> None:
    error = _load_error(write_text(tmp_path, "slug: [inheco-tec\nport: 8743\n"))
    assert error.key_path == "manifest"
    assert "YAML" in error.reason


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string\n"])
def test_fr2_5_non_mapping_document_raises_manifest_key(tmp_path: Path, text: str) -> None:
    assert _load_error(write_text(tmp_path, text)).key_path == "manifest"


def test_fr2_5_unsafe_yaml_tag_is_rejected(tmp_path: Path) -> None:
    text = "!!python/object/apply:os.system ['echo unsafe']\n"
    assert _load_error(write_text(tmp_path, text)).key_path == "manifest"


def test_fr2_8_duplicate_article_key_raises(tmp_path: Path) -> None:
    text = _base_text()
    assert "'7000190':" in text
    error = _load_error(write_text(tmp_path, text.replace("'7000190':", "'7000179':")))
    assert error.key_path == "article_numbers.7000179"
    assert "duplicate" in error.reason


def test_fr2_5_duplicate_top_level_key_raises(tmp_path: Path) -> None:
    error = _load_error(write_text(tmp_path, _base_text() + "port: 9000\n"))
    assert error.key_path == "port"


def test_fr2_8_unquoted_article_key_says_quote(tmp_path: Path) -> None:
    text = _base_text().replace("'7000179':", "7000179:")
    error = _load_error(write_text(tmp_path, text))
    assert error.key_path == "article_numbers.7000179"
    assert "quote" in error.reason


def test_fr13_4_invalid_transport_env_value_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "serial-xq7")
    error = _load_error(write_manifest(tmp_path, base_manifest()))
    assert error.key_path == "transport.kind"
    assert "serial-xq7" not in str(error)


def test_fr2_5_strict_types_reject_quoted_port(tmp_path: Path) -> None:
    data = mutated(lambda d: d.update(port="8743"))
    assert _load_error(write_manifest(tmp_path, data)).key_path == "port"


# --- No input value in the error (NFR-1, FR-2.8) ---

SENTINEL = "LEAKCHECK_Q9Z"
"""A distinctive marker value; no error text may echo it back."""

LEAK_VARIANTS = [
    pytest.param(mutated(lambda d: d.update(extra_key=SENTINEL)), id="unknown-key-value"),
    pytest.param(
        mutated(lambda d: d["auth"].update(bearer_token_ref=f"{SENTINEL.lower()}-ref")),
        id="token-ref",
    ),
    pytest.param(_set_slot(**{"class": SENTINEL}), id="slot-class"),
    pytest.param(
        mutated(lambda d: d["article_numbers"]["7000179"].update({"class": SENTINEL})),
        id="article-class",
    ),
    pytest.param(_set_transport(kind=SENTINEL), id="transport-kind"),
    pytest.param(mutated(lambda d: d.update(hosting=SENTINEL)), id="hosting"),
    pytest.param(mutated(lambda d: d["protocol"].update(api=SENTINEL)), id="protocol"),
    pytest.param(mutated(lambda d: d.update(port=SENTINEL)), id="port-type"),
    pytest.param(_set_slot(max_c=SENTINEL), id="max-type"),
]


@pytest.mark.parametrize("data", LEAK_VARIANTS)
def test_nfr1_error_text_never_echoes_string_input(tmp_path: Path, data: dict[str, Any]) -> None:
    error = _load_error(write_manifest(tmp_path, data))
    for text in (str(error), error.reason, error.key_path, repr(error)):
        assert SENTINEL not in text
        assert SENTINEL.lower() not in text


@pytest.mark.parametrize(
    ("data", "value"),
    [
        pytest.param(_set_slot(min_c=61.3, max_c=52.7), ("61.3", "52.7"), id="min-gt-max"),
        pytest.param(_set_slot(max_c=73.9), ("73.9",), id="cpac-max"),
        pytest.param(
            _set_slot(**{"class": "cpac-2tec", "max_c": 117.3}), ("117.3",), id="2tec-max"
        ),
        pytest.param(_set_slot(max_c=45.05), ("45.05",), id="not-tenths"),
        pytest.param(_set_slot(at_target_tolerance_c=7.3), ("7.3",), id="tolerance"),
        pytest.param(_set_transport(pre_send_delay_ms=97), ("97",), id="pre-send"),
        pytest.param(_set_transport(retry_delay_ms=987), ("987",), id="retry"),
        pytest.param(_set_transport(max_attempts=37), ("37",), id="max-attempts"),
        pytest.param(_set_transport(read_timeout_ms=12345), ("12345",), id="read-timeout"),
    ],
)
def test_nfr1_error_text_never_echoes_numeric_input(
    tmp_path: Path, data: dict[str, Any], value: tuple[str, ...]
) -> None:
    error = _load_error(write_manifest(tmp_path, data))
    for needle in value:
        assert needle not in str(error)


def test_nfr1_bad_yaml_error_does_not_echo_document_text(tmp_path: Path) -> None:
    text = f"auth: {{bearer_token_ref: {SENTINEL}\nport: [\n"
    error = _load_error(write_text(tmp_path, text))
    assert SENTINEL not in str(error)


def test_nfr1_duplicate_key_error_does_not_echo_value(tmp_path: Path) -> None:
    text = _base_text() + f"title: {SENTINEL}\n"
    error = _load_error(write_text(tmp_path, text))
    assert error.key_path == "title"
    assert SENTINEL not in str(error)

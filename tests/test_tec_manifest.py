"""Tests for C2 ``manifest`` (valid paths) and the C8 class table in ``limits`` (Task 5).

Covers FR-2, FR-2.1, FR-2.2, FR-2.3, FR-2.4, FR-2.6, FR-2.7, FR-5.2, FR-7.3, FR-13.4 and DD-12.
The fail-closed variants (FR-2.5, FR-2.8, NFR-1) live in ``test_tec_manifest_invalid.py`` (500-line
module limit). Every test runs with ``INHECO_TEC_MANIFEST`` and ``INHECO_TEC_TRANSPORT`` removed
by the autouse fixture; tests that need them set them through ``monkeypatch`` only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api import limits
from inheco_tec_api.manifest import (
    DEFAULT_MANIFEST_PATH,
    MANIFEST_ENV_VAR,
    TRANSPORT_ENV_VAR,
    Manifest,
    ManifestError,
    TransportConfig,
    load_manifest,
)
from tec_manifest_helpers import (
    EXPECTED_ARTICLES,
    base_manifest,
    mutated,
    slot,
    write_manifest,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MANIFEST_ENV_VAR, raising=False)
    monkeypatch.delenv(TRANSPORT_ENV_VAR, raising=False)


@pytest.fixture
def shipped(duo_root: Path) -> Manifest:
    return load_manifest(duo_root / "instrument.yaml")


# --- Shipped manifest (FR-2, FR-2.1, FR-2.4, FR-2.6, FR-2.7) ---


def test_fr2_default_path_is_duo_instrument_yaml(duo_root: Path) -> None:
    assert DEFAULT_MANIFEST_PATH.resolve() == (duo_root / "instrument.yaml").resolve()


def test_fr2_load_without_path_or_env_reads_shipped_manifest(shipped: Manifest) -> None:
    assert load_manifest() == shipped


def test_fr2_7_shipped_manifest_holds_exactly_the_six_article_entries(shipped: Manifest) -> None:
    got = {k: (v.limit_class, v.variant) for k, v in shipped.article_numbers.items()}
    assert got == EXPECTED_ARTICLES


def test_fr2_6_shipped_manifest_declares_controller0_slots_1_2_cpac(shipped: Manifest) -> None:
    assert [c.id for c in shipped.controllers] == [0]
    slots = shipped.controllers[0].slots
    assert [s.id for s in slots] == [1, 2]
    for s in slots:
        assert s.limit_class == "cpac"
        assert (s.min_c, s.max_c) == (4.0, 70.0)
        assert (s.min_tenths, s.max_tenths) == (40, 700)
        assert s.at_target_tolerance_c == 1.0


def test_fr2_1_shipped_manifest_service_identity(shipped: Manifest) -> None:
    assert shipped.slug == "inheco-tec"
    assert shipped.hosting == "host-resident"
    assert shipped.port == 8743
    assert shipped.host
    assert (shipped.protocol.api, shipped.protocol.instrument) == ("https-rest", "usb-hid")
    assert shipped.refs.linear_issue == "<issue-id>"
    assert shipped.refs.vault == "<knowledge-base-ref>"
    for capability in ("status", "connect", "set-target", "stop-all", "emergency-off"):
        assert capability in shipped.capabilities


def test_fr2_4_shipped_manifest_references_secrets_by_name_and_path(shipped: Manifest) -> None:
    assert shipped.auth.bearer_token_ref == "INHECO_TEC_API_TOKEN"  # pragma: allowlist secret
    assert re.fullmatch(r"[A-Z_][A-Z0-9_]*", shipped.auth.bearer_token_ref)
    assert shipped.tls.cert_file.startswith("C:/ProgramData/instrument-apis/inheco-tec/")
    assert shipped.tls.key_file.startswith("C:/ProgramData/instrument-apis/inheco-tec/")
    assert shipped.service.state_file.startswith("C:/ProgramData/instrument-apis/inheco-tec/")


def test_fr2_4_shipped_manifest_has_no_secret_shaped_keys(duo_root: Path) -> None:
    text = (duo_root / "instrument.yaml").read_text(encoding="utf-8")
    for forbidden in ("token:", "password", "private_key", "BEGIN "):
        assert forbidden not in text


def test_fr2_3_shipped_transport_settings_meet_floors(shipped: Manifest) -> None:
    t = shipped.transport
    assert t.kind == "hidapi"
    assert t.pre_send_delay_ms >= 100
    assert t.retry_delay_ms >= 1000
    assert 500 <= t.read_timeout_ms <= 10000
    assert 1 <= t.max_attempts <= 11


# --- Path override (FR-2) ---


def test_fr2_env_path_override_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = base_manifest()
    data["slug"] = "override-marker"
    path = write_manifest(tmp_path, data, name="other.yaml")
    monkeypatch.setenv(MANIFEST_ENV_VAR, str(path))
    assert load_manifest().slug == "override-marker"


def test_fr2_env_path_override_missing_file_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MANIFEST_ENV_VAR, str(tmp_path / "absent.yaml"))
    with pytest.raises(ManifestError) as info:
        load_manifest()
    assert info.value.key_path == "manifest"


def test_fr2_explicit_path_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MANIFEST_ENV_VAR, str(tmp_path / "absent.yaml"))
    path = write_manifest(tmp_path, base_manifest())
    assert load_manifest(path).slug == "inheco-tec"


def test_fr2_empty_env_path_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, shipped: Manifest
) -> None:
    monkeypatch.setenv(MANIFEST_ENV_VAR, "")
    assert load_manifest() == shipped


# --- Valid boundaries (FR-2.2, FR-2.3, FR-5.2, FR-7.3) ---


def _set_slot(**values: Any) -> dict[str, Any]:
    return mutated(lambda d: slot(d).update(values))


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(mutated(lambda d: d["controllers"][0].update(id=7)), id="controller-id-7"),
        pytest.param(_set_slot(id=6), id="slot-id-6"),
        pytest.param(_set_slot(min_c=70.0, max_c=70.0), id="cpac-min-equals-max"),
        pytest.param(_set_slot(min_c=12.3, max_c=45.6), id="tenths-resolution"),
        pytest.param(_set_slot(**{"class": "cpac-2tec", "max_c": 110.0}), id="cpac-2tec-max-110"),
        pytest.param(_set_slot(at_target_tolerance_c=0.1), id="tolerance-0.1"),
        pytest.param(_set_slot(at_target_tolerance_c=5.0), id="tolerance-5.0"),
        pytest.param(
            mutated(lambda d: d["transport"].update(pre_send_delay_ms=100, retry_delay_ms=1000)),
            id="delay-floors",
        ),
        pytest.param(
            mutated(lambda d: d["transport"].update(max_attempts=11, max_stale_reads=10)),
            id="max-attempts-11",
        ),
        pytest.param(
            mutated(lambda d: d["transport"].update(read_timeout_ms=500, lock_wait_s=120)),
            id="timeout-bounds",
        ),
        pytest.param(
            mutated(
                lambda d: d["controllers"].append({"id": 1, "slots": [{"id": 1, "class": "cpac"}]})
            ),
            id="second-controller-same-slot-id",
        ),
    ],
)
def test_fr2_2_boundary_values_are_accepted(tmp_path: Path, data: dict[str, Any]) -> None:
    load_manifest(write_manifest(tmp_path, data))


# --- Defaults (FR-2.3, DD-12, FR-7.3) ---


def test_dd12_cpac_2tec_slot_without_max_gets_100(tmp_path: Path) -> None:
    def change(d: dict[str, Any]) -> None:
        s = slot(d)
        s["class"] = "cpac-2tec"
        del s["max_c"]

    m = load_manifest(write_manifest(tmp_path, mutated(change)))
    s = m.controllers[0].slots[0]
    assert s.max_c == 100.0
    assert s.max_tenths == 1000


def test_dd12_cpac_slot_without_limits_gets_4_and_70(tmp_path: Path) -> None:
    def change(d: dict[str, Any]) -> None:
        s = slot(d)
        del s["min_c"], s["max_c"], s["at_target_tolerance_c"]

    s = load_manifest(write_manifest(tmp_path, mutated(change))).controllers[0].slots[0]
    assert (s.min_c, s.max_c) == (4.0, 70.0)
    assert s.at_target_tolerance_c == 1.0


def test_fr2_3_omitted_transport_section_uses_defaults_meeting_floors(tmp_path: Path) -> None:
    m = load_manifest(write_manifest(tmp_path, mutated(lambda d: d.pop("transport"))))
    t = m.transport
    assert (t.kind, t.pre_send_delay_ms, t.retry_delay_ms) == ("hidapi", 200, 1000)
    assert (t.read_timeout_ms, t.max_attempts, t.max_stale_reads, t.lock_wait_s) == (
        3000,
        3,
        3,
        16,
    )


def test_fr5_2_transport_model_default_pre_send_is_200() -> None:
    assert TransportConfig().pre_send_delay_ms == 200


# --- Fake transport selection (FR-5.2, FR-13.4) ---


def _zero_delays(kind: str = "hidapi") -> dict[str, Any]:
    return mutated(
        lambda d: d["transport"].update(kind=kind, pre_send_delay_ms=0, retry_delay_ms=0)
    )


def test_fr5_2_zero_delays_accepted_with_env_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "fake")
    m = load_manifest(write_manifest(tmp_path, _zero_delays()))
    assert m.transport.kind == "fake"
    assert (m.transport.pre_send_delay_ms, m.transport.retry_delay_ms) == (0, 0)


def test_fr13_4_zero_delays_accepted_with_manifest_kind_fake(tmp_path: Path) -> None:
    m = load_manifest(write_manifest(tmp_path, _zero_delays(kind="fake")))
    assert m.transport.kind == "fake"


def test_fr13_4_env_hidapi_overrides_manifest_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "hidapi")
    with pytest.raises(ManifestError) as info:
        load_manifest(write_manifest(tmp_path, _zero_delays(kind="fake")))
    assert info.value.key_path == "transport.pre_send_delay_ms"


def test_fr13_4_env_fake_overrides_manifest_hidapi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "fake")
    assert load_manifest(write_manifest(tmp_path, base_manifest())).transport.kind == "fake"


def test_fr13_4_empty_env_keeps_manifest_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "")
    assert load_manifest(write_manifest(tmp_path, base_manifest())).transport.kind == "hidapi"


def test_fr5_2_fake_still_rejects_nonzero_delay_below_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "fake")
    data = mutated(lambda d: d["transport"].update(pre_send_delay_ms=50))
    with pytest.raises(ManifestError) as info:
        load_manifest(write_manifest(tmp_path, data))
    assert info.value.key_path == "transport.pre_send_delay_ms"


# --- C8 class table in limits.py (FR-7.3) ---


def test_fr7_3_class_table_cpac_row() -> None:
    row = limits.CLASS_TABLE["cpac"]
    assert (row.name, row.device_type) == ("cpac", 1)
    assert (row.min_tenths, row.max_tenths, row.default_max_tenths) == (40, 700, 700)


def test_fr7_3_class_table_cpac_2tec_row() -> None:
    row = limits.CLASS_TABLE["cpac-2tec"]
    assert (row.name, row.device_type) == ("cpac-2tec", 4)
    assert (row.min_tenths, row.max_tenths, row.default_max_tenths) == (40, 1100, 1000)


def test_fr7_3_class_table_has_exactly_two_classes_in_integer_tenths() -> None:
    assert set(limits.CLASS_TABLE) == {"cpac", "cpac-2tec"}
    for row in limits.CLASS_TABLE.values():
        for value in (row.min_tenths, row.max_tenths, row.default_max_tenths):
            assert type(value) is int
        assert row.min_tenths <= row.default_max_tenths <= row.max_tenths


@pytest.mark.parametrize(("rtd", "expected"), [(1, "cpac"), (4, "cpac-2tec")])
def test_fr7_3_class_of_device_type_known(rtd: int, expected: str) -> None:
    assert limits.class_of_device_type(rtd) == expected


@pytest.mark.parametrize("rtd", [0, 2, 3, 5, 128, -1, 255])
def test_fr7_3_class_of_device_type_unknown_is_none(rtd: int) -> None:
    assert limits.class_of_device_type(rtd) is None

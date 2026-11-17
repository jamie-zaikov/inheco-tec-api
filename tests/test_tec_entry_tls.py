"""Tests for the C13 TLS startup check of ``entry.main`` (Task 19; FR-3.3, NFR-1).

A missing or unreadable certificate or key, a mismatched pair, an encrypted key and a non-PEM file
each exit 4 with one ``tls_invalid`` line naming the key and file, never file content. The pairs are
throwaway ``openssl`` pairs in a tmp dir (``tec_entry_helpers.make_tls_pairs``); ``uvicorn.run`` is
monkeypatched, so no port is bound.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from inheco_tec_api import entry
from tec_api_helpers import sentinel
from tec_entry_helpers import (
    OPENSSL,
    Rig,
    make_tls_pairs,
    needs_openssl,
    one,
    restored_logging,
)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    with restored_logging():
        yield


@pytest.fixture(scope="session")
def tls_dir(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if OPENSSL is None:
        pytest.skip("openssl CLI not on PATH; cannot make a throwaway TLS pair")
    return make_tls_pairs(tmp_path_factory.mktemp("tls"))


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Rig:
    return Rig(tmp_path, monkeypatch, capsys)


# -- step 5: TLS ---------------------------------------------------------------------------------


@needs_openssl
@pytest.mark.parametrize("missing", ["cert", "key"])
def test_fr3_3_missing_cert_or_key_exits_4_naming_the_key(
    rig: Rig, tls_dir: dict[str, Path], missing: str
) -> None:
    rig.set_bearer()
    cert, key = tls_dir["cert_a"], tls_dir["key_a"]
    absent = rig.tmp_path / f"absent.{missing}"
    rig.use_tls(absent if missing == "cert" else cert, absent if missing == "key" else key)
    events = rig.main(expect_exit=entry.EXIT_TLS_INVALID)
    event = one(events, entry.EVENT_TLS_INVALID)
    assert event["level"] == "error"
    assert event["key"] == ("tls.cert_file" if missing == "cert" else "tls.key_file")
    assert event["file"] == str(absent)
    assert event["reason"].startswith("cannot open file")


def test_fr3_3_unreadable_cert_path_exits_4(rig: Rig) -> None:
    """A path that exists but cannot be opened as a file (a directory) exits 4."""
    rig.set_bearer()
    cert_dir = rig.tmp_path / "cert-dir"
    cert_dir.mkdir()
    rig.use_tls(cert_dir, rig.tmp_path / "missing.key")
    events = rig.main(expect_exit=entry.EXIT_TLS_INVALID)
    assert one(events, entry.EVENT_TLS_INVALID)["key"] == "tls.cert_file"


@needs_openssl
def test_fr3_3_mismatched_pair_exits_4(rig: Rig, tls_dir: dict[str, Path]) -> None:
    rig.set_bearer()
    rig.use_tls(tls_dir["cert_a"], tls_dir["key_b"])
    events = rig.main(expect_exit=entry.EXIT_TLS_INVALID)
    event = one(events, entry.EVENT_TLS_INVALID)
    assert event["key"] == "tls"
    assert event["reason"].startswith("invalid certificate/key pair")


@needs_openssl
def test_fr3_3_encrypted_key_exits_4_without_prompt(rig: Rig, tls_dir: dict[str, Path]) -> None:
    rig.set_bearer()
    rig.use_tls(tls_dir["cert_a"], tls_dir["key_a_encrypted"])
    events = rig.main(expect_exit=entry.EXIT_TLS_INVALID)
    event = one(events, entry.EVENT_TLS_INVALID)
    assert event["key"] == "tls.key_file"
    assert "password-protected" in event["reason"]


@needs_openssl
@pytest.mark.parametrize("bad", ["cert", "key"])
def test_fr3_3_non_pem_file_exits_4_without_content(
    rig: Rig, tls_dir: dict[str, Path], bad: str
) -> None:
    rig.set_bearer()
    marker = sentinel("not-pem")
    junk = rig.tmp_path / f"junk.{bad}"
    junk.write_text(f"not a PEM file {marker}\n", "utf-8")
    rig.markers.append(marker)
    rig.use_tls(
        junk if bad == "cert" else tls_dir["cert_a"], junk if bad == "key" else tls_dir["key_a"]
    )
    events = rig.main(expect_exit=entry.EXIT_TLS_INVALID)
    assert one(events, entry.EVENT_TLS_INVALID)["key"] == "tls"


@needs_openssl
def test_nfr1_tls_failure_logs_no_key_material(rig: Rig, tls_dir: dict[str, Path]) -> None:
    rig.set_bearer()
    rig.use_tls(tls_dir["cert_a"], tls_dir["key_b"])
    rig.main(expect_exit=entry.EXIT_TLS_INVALID)
    for name in ("key_a", "key_b", "cert_a"):
        body = "".join(tls_dir[name].read_text("ascii").splitlines()[1:-1])
        assert body[:40] not in rig.output.replace("\\n", "")
    assert "BEGIN" not in rig.output

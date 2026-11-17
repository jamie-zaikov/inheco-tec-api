"""Shared fixtures for the inheco-tec api suites (design C16).

This directory has NO ``__init__.py`` (design Risk R8): pytest and mypy then see unique top-level
module names (``conftest``, ``test_tec_*``) that cannot collide with the ``tests`` package of
``<vendored-core>``. Later tasks add the fake transport and app fixtures here.

The path fixtures below resolve the repository tree from this file's location, so they work from
any working directory and need no installed checkout layout beyond the repository itself. In this
standalone repository the API folder, the duo folder and the repository root are one folder: the
repository root holds ``pyproject.toml``, ``instrument.yaml`` and ``constraints.txt``.
"""

from pathlib import Path

import inheco_tec_api
import pytest

_TESTS_DIR: Path = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def api_root() -> Path:
    """The ``<api-repo-root>`` directory (holds ``pyproject.toml``)."""
    return _TESTS_DIR.parent


@pytest.fixture(scope="session")
def duo_root(api_root: Path) -> Path:
    """The folder that holds ``instrument.yaml``: the repository root."""
    return api_root


@pytest.fixture(scope="session")
def repo_root(duo_root: Path) -> Path:
    """The repository root (holds ``constraints.txt`` and ``.github/``)."""
    return duo_root


@pytest.fixture(scope="session")
def package_dir() -> Path:
    """The directory of the importable ``inheco_tec_api`` package."""
    return Path(inheco_tec_api.__file__).resolve().parent

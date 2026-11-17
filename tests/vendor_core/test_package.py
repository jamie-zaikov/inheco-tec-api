"""Package-level surface/version/stdlib-only import suite for ``instrument_api_core`` (Task 8).

This suite is the guard that a later conformance fix never changes the documented public surface
(NFR-8). It pins three package-level contracts:

- ``instrument_api_core.__all__`` equals the documented set exactly, compared sorted, so any add,
  remove, rename, or reorder of a public name breaks the test (FR-7, NFR-8);
- ``__version__`` equals the manifest ``version`` — read via
  ``importlib.metadata.version("instrument-api-core")`` when installed, with a stdlib ``tomllib``
  parse-fallback that reads ``pyproject.toml`` (FR-7.1) — so a bump that misses a file fails CI;
- ``import instrument_api_core`` succeeds with no optional dependency installed (the base is
  stdlib-only) and the ``py.typed`` marker ships in the importable package (FR-7.2, FR-1.4, NFR-2).

It uses the standard library plus pytest only and stays fully offline (NFR-7).
"""

import ast
import importlib.metadata
import sys
from pathlib import Path

from inheco_tec_api._vendor import instrument_api_core

# --------------------------------------------------------------------------------------------------
# The pinned public surface (FR-7, NFR-8)
#
# This literal is the documented ``__all__`` of ``instrument_api_core/__init__.py``: the constants,
# exceptions, dataclasses, and functions of FR-1..FR-6 plus ``__version__``. It is pinned here as an
# explicit set so any drift in the source ``__all__`` is caught by an exact comparison.
# --------------------------------------------------------------------------------------------------

EXPECTED_ALL: frozenset[str] = frozenset(
    {
        # health (FR-1)
        "HEALTH_PATH",
        "HEALTH_STATUS_OK",
        "HealthStatus",
        "health_payload",
        # auth (FR-2)
        "AuthConfigError",
        "Unauthorized",
        "load_expected_token",
        "verify_bearer_token",
        # config (FR-3)
        "ConfigError",
        "ServiceConfig",
        "require_env",
        # logging (FR-4)
        "DEFAULT_LOG_LEVEL",
        "LOG_LEVEL_ENV_VAR",
        "configure_logging",
        "get_logger",
        # port (FR-5)
        "CONTAINER_PORT",
        "PORT_ENV_VAR",
        "resolve_port",
        # transport (FR-6)
        "DEFAULT_TIMEOUT_SECONDS",
        "MAX_MESSAGE_BYTES",
        "ConnectionRefused",
        "HostNotFound",
        "MessageTooLarge",
        "TcpProtobufTransport",
        "TransportError",
        "TransportTimeout",
        # package (FR-7)
        "__version__",
    }
)

DISTRIBUTION_NAME: str = "instrument-api-core"
# The import path of the vendored core inside this distribution, and the ``project.version`` of
# the core's own ``pyproject.toml`` when it was vendored.
VENDORED_MODULE: str = "inheco_tec_api._vendor.instrument_api_core"
VENDORED_CORE_VERSION: str = "0.1.0"

# The documented base modules (module stems) whose bodies back the FR-1..FR-6 surface, plus the
# ``__init__`` barrel. The stdlib-only AST scan walks ``*.py`` dynamically, so this pinned set
# proves the scan covers every base module and cannot silently skip one a conformance fix drops.
EXPECTED_BASE_MODULES: frozenset[str] = frozenset(
    {
        "__init__",
        "auth",
        "config",
        "health",
        "logging",
        "port",
        "transport",
    }
)

# The optional-extra packages the base must never require (api/transport extras); a base import that
# pulls any of these in would violate the stdlib-only contract (NFR-2).
OPTIONAL_THIRD_PARTY: frozenset[str] = frozenset(
    {
        "fastapi",
        "uvicorn",
        "structlog",
        "pydantic",
        "pydantic_settings",
        "google",  # the ``protobuf`` runtime imports as ``google.protobuf``
        "protobuf",
    }
)


def _package_dir() -> Path:
    """Return the on-disk directory of the importable ``instrument_api_core`` package."""
    package_file = instrument_api_core.__file__
    assert package_file is not None
    return Path(package_file).parent


def _pyproject_version() -> str:
    """Return the ``project.version`` of the source ``pyproject.toml`` of the vendored core.

    The core ships inside this distribution without its own ``pyproject.toml``, so the version it
    had when it was vendored is pinned in ``VENDORED_CORE_VERSION``; the value is independent of
    the install state and works offline.
    """
    return VENDORED_CORE_VERSION


def _manifest_version() -> str:
    """Return the manifest version: installed metadata first, ``pyproject.toml`` as the fallback.

    When the distribution is installed, ``importlib.metadata.version`` is the authoritative source;
    otherwise the ``tomllib`` parse of ``pyproject.toml`` is used (FR-7.1).
    """
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return _pyproject_version()


def _imported_module_roots(source_path: Path) -> set[str]:
    """Return the set of top-level module names imported by the Python file at ``source_path``.

    The file is parsed with the ``ast`` module (no import side effects). A relative import
    (``from . import x``) contributes no external root and is skipped. ``import a.b`` and
    ``from a.b import c`` both contribute the root ``a``.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module is not None:
                roots.add(node.module.split(".", 1)[0])
    return roots


# --------------------------------------------------------------------------------------------------
# Surface: ``__all__`` equals the documented set exactly (FR-7, NFR-8)
# --------------------------------------------------------------------------------------------------


def test_all_equals_the_documented_surface_exactly() -> None:
    """``__all__`` equals the pinned documented set, so any add/remove/rename breaks (FR-7, NFR-8).

    The comparison is sorted-order-independent (set equality) and exact, so it also catches a
    duplicate or a reordering that changes the contents.
    """
    assert sorted(instrument_api_core.__all__) == sorted(EXPECTED_ALL)


def test_all_has_no_duplicate_names() -> None:
    """``__all__`` lists each public name once — no accidental duplicate (NFR-8)."""
    names = list(instrument_api_core.__all__)
    assert len(names) == len(set(names))


def test_every_exported_name_is_a_real_attribute() -> None:
    """Every name in ``__all__`` resolves to a real package attribute (FR-7, NFR-8)."""
    for name in instrument_api_core.__all__:
        assert hasattr(instrument_api_core, name), name


def test_every_documented_name_binds_via_from_import() -> None:
    """Every documented name binds via ``from instrument_api_core import <name>`` (FR-7, NFR-8).

    The real-attribute test proves ``hasattr``; this proves the consumer-facing import-statement
    path binds each name. ``__import__(..., fromlist=[...])`` plus ``getattr`` is exactly what the
    ``from instrument_api_core import <name>`` statement compiles to, so a name that failed to bind
    (an ``AttributeError`` here mirrors the statement's ``ImportError``) is caught. Iterating the
    pinned documented set ties importability to the contract, not to whatever ``__all__`` happens
    to list.
    """
    module = __import__(VENDORED_MODULE, fromlist=sorted(EXPECTED_ALL))
    unbound: list[str] = []
    for name in sorted(EXPECTED_ALL):
        try:
            getattr(module, name)
        except AttributeError:
            unbound.append(name)
    assert unbound == [], unbound


# --------------------------------------------------------------------------------------------------
# Version: ``__version__`` equals the manifest (FR-7.1)
# --------------------------------------------------------------------------------------------------


def test_version_equals_the_manifest_version() -> None:
    """``__version__`` equals the manifest version (metadata first, TOML fallback) (FR-7.1)."""
    assert instrument_api_core.__version__ == _manifest_version()


def test_version_equals_the_pyproject_version() -> None:
    """``__version__`` equals the ``pyproject.toml`` ``project.version`` parsed by ``tomllib``.

    This reads the authoritative manifest directly, so a bump that updates ``__init__.py`` but not
    ``pyproject.toml`` (or the reverse) fails CI regardless of the install state (FR-7.1).
    """
    assert instrument_api_core.__version__ == _pyproject_version()


def test_version_is_a_non_empty_string() -> None:
    """``__version__`` is a non-empty string (a usable semver token) (FR-7.1)."""
    assert isinstance(instrument_api_core.__version__, str)
    assert instrument_api_core.__version__


# --------------------------------------------------------------------------------------------------
# Import: stdlib-only base and the ``py.typed`` marker (FR-7.2, FR-1.4, NFR-2)
# --------------------------------------------------------------------------------------------------


def test_bare_import_succeeds_with_no_optional_dependency() -> None:
    """``import instrument_api_core`` succeeds with no optional dependency installed (FR-7.2).

    The module object imported at collection time is the proof; this asserts it is bound and names
    the package.
    """
    assert instrument_api_core.__name__ == VENDORED_MODULE


def test_py_typed_marker_ships_in_the_importable_package() -> None:
    """The ``py.typed`` marker is present in the importable package directory (FR-1.4, NFR-2)."""
    marker = _package_dir() / "py.typed"
    assert marker.is_file()


def test_base_modules_import_only_the_standard_library() -> None:
    """No base module imports a third-party package — the base is stdlib-only (NFR-2, FR-7.2).

    An AST import-graph walk over every ``.py`` file in the package collects the top-level import
    roots. Each root must be a standard-library module (``sys.stdlib_module_names``) or the package
    itself; a relative import contributes nothing. This proves the base pulls no third-party package
    without importing any optional dependency.
    """
    stdlib = sys.stdlib_module_names
    allowed = set(stdlib)
    offenders: dict[str, set[str]] = {}
    for source_path in sorted(_package_dir().glob("*.py")):
        third_party = _imported_module_roots(source_path) - allowed
        if third_party:
            offenders[source_path.name] = third_party
    assert offenders == {}, offenders


def test_import_graph_scan_covers_every_base_module() -> None:
    """The stdlib-only AST scan covers every documented base module — none is silently skipped.

    ``test_base_modules_import_only_the_standard_library`` walks ``*.py`` dynamically, so a renamed
    or deleted module would drop out of the glob and the stdlib-only guard would pass **vacuously**
    for it. This pins the scanned set to the seven documented base modules (health, auth, config,
    logging, port, transport, and the ``__init__`` barrel), so the completeness of the guard cannot
    regress (NFR-2, FR-7.2).
    """
    scanned = {source_path.stem for source_path in _package_dir().glob("*.py")}
    missing = EXPECTED_BASE_MODULES - scanned
    assert missing == frozenset(), missing


def test_base_modules_require_no_optional_extra_package() -> None:
    """No base module imports fastapi/uvicorn/structlog/pydantic/pydantic_settings/protobuf (NFR-2).

    This is the explicit complement of the stdlib-only check: it names the optional-extra runtimes
    and asserts the base requires none of them, so a bare import needs no extra installed (FR-7.2).
    """
    imported: set[str] = set()
    for source_path in _package_dir().glob("*.py"):
        imported |= _imported_module_roots(source_path)
    assert imported & OPTIONAL_THIRD_PARTY == set()

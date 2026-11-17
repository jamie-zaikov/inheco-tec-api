"""Structured-logging suite for ``instrument_api_core.logging`` (Task 5).

This suite pins four contracts of the stdlib logging base:

- the two constants (``LOG_LEVEL_ENV_VAR``, ``DEFAULT_LOG_LEVEL``) and the presence of the two
  callables (``configure_logging``, ``get_logger``);
- ``configure_logging`` installs exactly one ``StreamHandler`` on the root logger, writing to
  ``sys.stdout``, at the upper-cased level;
- ``get_logger(name)`` returns the stdlib ``logging.Logger`` for ``name``;
- the documented per-event field contract (``method`` / ``path`` / ``status_class`` only, never a
  token, host, port, or request body) and the module's stdlib-only import graph.

It uses the standard library plus pytest only and stays fully offline (NFR-7). Every test that
mutates the process-global root logger requests the ``restore_root_logger`` fixture (conftest) so a
``configure_logging`` call cannot leak into any other suite (design Risks -> global-state leakage).
"""

import ast
import logging
import sys

from inheco_tec_api._vendor.instrument_api_core import logging as core_logging
from inheco_tec_api._vendor.instrument_api_core.logging import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENV_VAR,
    configure_logging,
    get_logger,
)

# --------------------------------------------------------------------------------------------------
# Constants and surface (FR-4, FR-4.1)
# --------------------------------------------------------------------------------------------------


def test_log_level_env_var_names_the_override() -> None:
    """``LOG_LEVEL_ENV_VAR`` is exactly ``"LOG_LEVEL"`` (FR-4, FR-4.1)."""
    assert LOG_LEVEL_ENV_VAR == "LOG_LEVEL"


def test_default_log_level_is_info() -> None:
    """``DEFAULT_LOG_LEVEL`` is exactly ``"INFO"`` (FR-4, FR-4.1)."""
    assert DEFAULT_LOG_LEVEL == "INFO"


def test_module_exposes_configure_logging_and_get_logger() -> None:
    """The module exposes the two documented callables (FR-4, FR-4.1)."""
    assert callable(configure_logging)
    assert callable(get_logger)
    assert callable(core_logging.configure_logging)
    assert callable(core_logging.get_logger)


# --------------------------------------------------------------------------------------------------
# configure_logging handler/level installation (FR-4.2)
# --------------------------------------------------------------------------------------------------


def test_configure_logging_installs_one_stdout_stream_handler(
    restore_root_logger: None,
) -> None:
    """``configure_logging`` leaves the root logger one stdout ``StreamHandler`` (FR-4.2)."""
    configure_logging("debug")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stdout


def test_configure_logging_sets_the_upper_cased_level(
    restore_root_logger: None,
) -> None:
    """A lower-case ``"debug"`` is upper-cased to the ``DEBUG`` root level (FR-4.2)."""
    configure_logging("debug")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert logging.getLevelName(root.level) == "DEBUG"


def test_configure_logging_default_level_is_info(
    restore_root_logger: None,
) -> None:
    """With no argument the root level is the default ``INFO`` (FR-4.1, FR-4.2)."""
    configure_logging()
    root = logging.getLogger()
    assert root.level == logging.INFO


def test_configure_logging_replaces_prior_handlers(
    restore_root_logger: None,
) -> None:
    """A pre-existing handler is replaced, so exactly one remains after the call (FR-4.2)."""
    root = logging.getLogger()
    root.handlers = [logging.StreamHandler(sys.stderr)]
    configure_logging("debug")
    assert len(root.handlers) == 1
    assert root.handlers[0].stream is sys.stdout  # type: ignore[attr-defined]


def test_configure_logging_upper_cases_a_mixed_case_level(
    restore_root_logger: None,
) -> None:
    """A mixed-case level beyond ``"debug"`` is upper-cased (``"Warning"`` -> WARNING) (FR-4.2)."""
    configure_logging("Warning")
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert logging.getLevelName(root.level) == "WARNING"


def test_configure_logging_is_idempotent_across_two_calls(
    restore_root_logger: None,
) -> None:
    """Two consecutive calls do not accumulate handlers; exactly one remains (FR-4.2)."""
    configure_logging("info")
    configure_logging("debug")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stdout
    assert root.level == logging.DEBUG


# --------------------------------------------------------------------------------------------------
# get_logger (FR-4.3)
# --------------------------------------------------------------------------------------------------


def test_get_logger_returns_the_named_stdlib_logger() -> None:
    """``get_logger("x")`` returns the stdlib ``logging.Logger`` named ``"x"`` (FR-4.3)."""
    logger = get_logger("x")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "x"
    assert logger is logging.getLogger("x")


def test_get_logger_distinct_names_yield_distinct_loggers() -> None:
    """Distinct names yield distinct loggers; the same name yields the same instance (FR-4.3)."""
    logger_a = get_logger("pkg.a")
    logger_b = get_logger("pkg.b")
    assert logger_a is not logger_b
    assert logger_a.name == "pkg.a"
    assert logger_b.name == "pkg.b"
    assert get_logger("pkg.a") is logger_a


# --------------------------------------------------------------------------------------------------
# Documented field contract and stdlib-only import graph (FR-4.4, NFR-1)
# --------------------------------------------------------------------------------------------------


def test_module_documents_the_no_secret_no_host_no_port_exclusion() -> None:
    """The docstring documents that no token, host, port, or request body is logged (NFR-1)."""
    doc = core_logging.__doc__
    assert doc is not None
    assert "never a token" in doc
    assert "a host" in doc
    assert "a port" in doc
    assert "request body" in doc


def test_configure_logging_formatter_emits_message_only(
    restore_root_logger: None,
) -> None:
    """The installed handler formats a record to its bare message, adding no extra field (NFR-1).

    A behavioral complement to the docstring grep: the stdlib base uses ``"%(message)s"``, so it
    emits the event message unchanged and never prepends a timestamp, level, host, or port that
    could leak context beyond ``method`` / ``path`` / ``status_class`` (FR-4.4, NFR-1).
    """
    configure_logging("info")
    root = logging.getLogger()
    handler = root.handlers[0]
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="method=GET path=/healthz status_class=2xx",
        args=(),
        exc_info=None,
    )
    formatted = handler.format(record)
    assert formatted == "method=GET path=/healthz status_class=2xx"


def test_module_is_stdlib_only() -> None:
    """Every module the logging base imports resolves to the standard library (FR-4.4, NFR-1)."""
    source_path = core_logging.__file__
    assert source_path is not None
    with open(source_path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) stays inside the stdlib-only package.
            if node.level == 0 and node.module is not None:
                imported_roots.add(node.module.split(".")[0])

    assert imported_roots, "the module must import at least the standard library it documents"
    non_stdlib = imported_roots - sys.stdlib_module_names
    assert non_stdlib == set(), f"non-stdlib imports found: {sorted(non_stdlib)}"

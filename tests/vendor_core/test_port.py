"""Cloud Run port contract suite for ``instrument_api_core.port`` (Task 6).

This suite pins four contracts of the shared port base:

- the documented constants and surface (``CONTAINER_PORT == 8080``, ``PORT_ENV_VAR == "PORT"``,
  and the ``resolve_port`` callable) (FR-5, FR-5.1);
- the default-when-unset path (``resolve_port`` returns :data:`CONTAINER_PORT` when ``PORT`` is
  unset or empty in the injected env map) (FR-5.2);
- the in-range success path with the ``1`` and ``65535`` boundary cases (FR-5.3);
- the fail-fast path (a non-integer or an out-of-range value raises ``ValueError`` that names the
  offending value) (FR-5.4).

It uses the standard library plus pytest only and stays fully offline (NFR-7). The ``env_factory``
(conftest) injects a ``dict[str, str]`` so no test mutates ``os.environ`` except the single
``env=None`` process-env case, which uses ``monkeypatch.setenv``.
"""

import pytest
from inheco_tec_api._vendor.instrument_api_core import port
from inheco_tec_api._vendor.instrument_api_core.port import (
    CONTAINER_PORT,
    PORT_ENV_VAR,
    resolve_port,
)

from vendor_core.conftest import EnvFactory

# --------------------------------------------------------------------------------------------------
# Surface and constants (FR-5, FR-5.1)
# --------------------------------------------------------------------------------------------------


def test_module_exposes_the_documented_port_surface() -> None:
    """The module exposes the two constants and the ``resolve_port`` callable (FR-5.1)."""
    assert hasattr(port, "CONTAINER_PORT")
    assert hasattr(port, "PORT_ENV_VAR")
    assert callable(port.resolve_port)


def test_container_port_constant_is_8080() -> None:
    """The Cloud Run container port contract defaults to ``8080`` (FR-5.1)."""
    assert CONTAINER_PORT == 8080
    assert isinstance(CONTAINER_PORT, int)


def test_port_env_var_constant_is_port() -> None:
    """Cloud Run injects the listen port through the ``PORT`` env var (FR-5.1)."""
    assert PORT_ENV_VAR == "PORT"


# --------------------------------------------------------------------------------------------------
# Default-when-unset path (FR-5.2)
# --------------------------------------------------------------------------------------------------


def test_resolve_port_defaults_when_var_unset(env_factory: EnvFactory) -> None:
    """An injected env map without ``PORT`` falls back to :data:`CONTAINER_PORT` (FR-5.2)."""
    env = env_factory()  # PORT absent
    assert resolve_port(env) == CONTAINER_PORT


def test_resolve_port_defaults_when_value_empty(env_factory: EnvFactory) -> None:
    """An injected empty ``PORT`` value falls back to :data:`CONTAINER_PORT` (FR-5.2)."""
    env = env_factory(PORT="")
    assert resolve_port(env) == CONTAINER_PORT


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n", " \t\n "])
def test_resolve_port_defaults_when_value_whitespace_only(
    blank: str, env_factory: EnvFactory
) -> None:
    """An injected whitespace-only ``PORT`` value falls back to :data:`CONTAINER_PORT` (FR-5.2)."""
    env = env_factory(PORT=blank)
    assert resolve_port(env) == CONTAINER_PORT


def test_resolve_port_none_defaults_when_process_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``env=None`` and the process ``PORT`` unset, it defaults via ``os.environ`` (FR-5.2)."""
    monkeypatch.delenv(PORT_ENV_VAR, raising=False)
    assert resolve_port() == CONTAINER_PORT


# --------------------------------------------------------------------------------------------------
# In-range success path and boundaries (FR-5.3)
# --------------------------------------------------------------------------------------------------


def test_resolve_port_returns_in_range_integer(env_factory: EnvFactory) -> None:
    """A present, in-range integer ``PORT`` is parsed and returned (FR-5.3)."""
    env = env_factory(PORT="9090")
    result = resolve_port(env)
    assert result == 9090
    assert isinstance(result, int)


@pytest.mark.parametrize("boundary", [1, 65535])
def test_resolve_port_accepts_range_boundaries(boundary: int, env_factory: EnvFactory) -> None:
    """The lower (``1``) and upper (``65535``) in-range boundaries are accepted (FR-5.3)."""
    env = env_factory(PORT=str(boundary))
    assert resolve_port(env) == boundary


def test_resolve_port_injected_map_overrides_process_env(
    env_factory: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected ``Mapping`` takes precedence over the process environment (FR-5.3)."""
    monkeypatch.setenv(PORT_ENV_VAR, "1111")
    env = env_factory(PORT="2222")
    assert resolve_port(env) == 2222


def test_resolve_port_none_reads_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``env=None`` an in-range ``PORT`` is read from the process ``os.environ`` (FR-5.3)."""
    monkeypatch.setenv(PORT_ENV_VAR, "7070")
    assert resolve_port() == 7070


@pytest.mark.parametrize("padded", [" 8080 ", "\t8080\n", "  1  "])
def test_resolve_port_accepts_valid_integer_with_surrounding_whitespace(
    padded: str, env_factory: EnvFactory
) -> None:
    """A valid integer wrapped in whitespace is parsed, not rejected (FR-5.3).

    The empty/whitespace-only guard uses ``str.strip()`` for a truthiness check only; a value with
    real digits then passes to ``int()``, which itself trims the surrounding whitespace. This pins
    that the guard and the parse do not reject a padded-but-valid ``PORT``.
    """
    result = resolve_port(env_factory(PORT=padded))
    assert result == int(padded)
    assert isinstance(result, int)


def test_resolve_port_accepts_leading_plus_sign(env_factory: EnvFactory) -> None:
    """A signed positive integer (``"+80"``) is accepted and returned as ``int`` (FR-5.3)."""
    result = resolve_port(env_factory(PORT="+80"))
    assert result == 80
    assert isinstance(result, int)


# --------------------------------------------------------------------------------------------------
# Fail-fast on a bad port, naming the offending value (FR-5.4)
# --------------------------------------------------------------------------------------------------


def test_resolve_port_non_integer_raises_naming_value(env_factory: EnvFactory) -> None:
    """A non-integer ``PORT`` raises ``ValueError`` that names the offending value (FR-5.4)."""
    env = env_factory(PORT="abc")
    with pytest.raises(ValueError) as exc_info:
        resolve_port(env)
    assert "abc" in str(exc_info.value)


@pytest.mark.parametrize("out_of_range", [0, 65536])
def test_resolve_port_out_of_range_raises_naming_value(
    out_of_range: int, env_factory: EnvFactory
) -> None:
    """An out-of-range ``PORT`` (``0`` or ``65536``) raises ``ValueError`` naming it (FR-5.4)."""
    env = env_factory(PORT=str(out_of_range))
    with pytest.raises(ValueError) as exc_info:
        resolve_port(env)
    assert str(out_of_range) in str(exc_info.value)


def test_resolve_port_float_string_raises_naming_value(env_factory: EnvFactory) -> None:
    """A numeric-but-non-integer ``PORT`` (``"80.5"``) is rejected, not truncated (FR-5.4).

    ``int("80.5")`` raises, so the value takes the non-integer branch whose message names the raw
    string; the port is never silently truncated to ``80``.
    """
    env = env_factory(PORT="80.5")
    with pytest.raises(ValueError) as exc_info:
        resolve_port(env)
    assert "80.5" in str(exc_info.value)


def test_resolve_port_negative_value_raises_naming_value(env_factory: EnvFactory) -> None:
    """A negative ``PORT`` (``"-1"``) parses to an int but fails the range check (FR-5.4)."""
    env = env_factory(PORT="-1")
    with pytest.raises(ValueError) as exc_info:
        resolve_port(env)
    assert "-1" in str(exc_info.value)

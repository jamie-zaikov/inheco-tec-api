"""Runtime-configuration suite for ``instrument_api_core.config`` (Task 4).

This suite pins four contracts of the shared config base:

- the fail-closed matrix (``require_env`` refuses to run with the value unset, empty, or
  whitespace-only), asserting the raised message names the env var and never the value;
- the success path (a non-empty value returned unchanged, an injected ``Mapping`` overriding the
  process env, and the ``env=None`` process-env branch);
- the frozen ``ServiceConfig`` shape (``container_port: int`` only, no secret and no host field,
  assignment raises ``FrozenInstanceError``);
- ``ServiceConfig.from_env`` delegation to ``resolve_port`` (verified with an injected ``PORT`` map
  and a default, unset case), reading no secret.

It uses the standard library plus pytest only and stays fully offline (NFR-7). Every assertion
guards the no-value-in-message / no-secret-on-the-object rule (NFR-1): a rejected value never
appears in a ``ConfigError`` message, and ``ServiceConfig`` carries no secret. The ``env_factory``
(conftest) injects a ``dict[str, str]`` so no test mutates ``os.environ`` except the single
``env=None`` case, which uses ``monkeypatch.setenv``.
"""

import dataclasses
from collections.abc import Mapping

import pytest
from inheco_tec_api._vendor.instrument_api_core import config
from inheco_tec_api._vendor.instrument_api_core.config import (
    ConfigError,
    ServiceConfig,
    require_env,
)
from inheco_tec_api._vendor.instrument_api_core.port import CONTAINER_PORT, resolve_port

from vendor_core.conftest import EnvFactory

# A non-secret required setting name used across the require_env cases.
_SETTING_NAME: str = "SERVICE_REGION"


# --------------------------------------------------------------------------------------------------
# Surface (FR-3)
# --------------------------------------------------------------------------------------------------


def test_module_exposes_the_documented_config_surface() -> None:
    """The module exposes the three documented config names on import (FR-3)."""
    assert hasattr(config, "ConfigError")
    assert hasattr(config, "require_env")
    assert hasattr(config, "ServiceConfig")


def test_config_error_is_a_runtime_error() -> None:
    """``ConfigError`` is a ``RuntimeError`` subclass usable as a startup failure (FR-3)."""
    assert issubclass(ConfigError, RuntimeError)


# --------------------------------------------------------------------------------------------------
# Fail-closed matrix for require_env (FR-3.1)
# --------------------------------------------------------------------------------------------------


def test_require_env_unset_var_fails_closed(env_factory: EnvFactory) -> None:
    """An injected env map with the name absent raises ``ConfigError`` (FR-3.1)."""
    env = env_factory()  # the setting is not present at all
    with pytest.raises(ConfigError):
        require_env(_SETTING_NAME, env=env)


def test_require_env_empty_value_fails_closed(env_factory: EnvFactory) -> None:
    """An injected empty value raises ``ConfigError`` (fail closed) (FR-3.1)."""
    env = env_factory(SERVICE_REGION="")
    with pytest.raises(ConfigError):
        require_env(_SETTING_NAME, env=env)


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n", " \t\n "])
def test_require_env_whitespace_only_fails_closed(blank: str, env_factory: EnvFactory) -> None:
    """An injected whitespace-only value raises ``ConfigError`` (fail closed) (FR-3.1)."""
    env = env_factory(SERVICE_REGION=blank)
    with pytest.raises(ConfigError):
        require_env(_SETTING_NAME, env=env)


# --------------------------------------------------------------------------------------------------
# Success path for require_env (FR-3.1)
# --------------------------------------------------------------------------------------------------


def test_require_env_returns_value_unchanged(env_factory: EnvFactory) -> None:
    """A non-empty value is returned byte-for-byte unchanged (FR-3.1)."""
    env = env_factory(SERVICE_REGION="us-west1")
    assert require_env(_SETTING_NAME, env=env) == "us-west1"


def test_require_env_preserves_surrounding_content(env_factory: EnvFactory) -> None:
    """A value whose non-blank content has surrounding spacing is returned unchanged (FR-3.1).

    The value is non-empty after ``strip()``, so it is returned exactly as supplied (no trimming).
    """
    padded = "  us-west1  "
    env = env_factory(SERVICE_REGION=padded)
    assert require_env(_SETTING_NAME, env=env) == padded


def test_require_env_returns_value_with_inner_spaces(env_factory: EnvFactory) -> None:
    """A legitimate value with inner spaces is non-blank after ``strip()`` and returned (FR-3.1).

    The fail-closed check keys on emptiness after ``strip()``, never on the presence of a space,
    so a multi-token value is accepted (not rejected) and returned byte-for-byte unchanged.
    """
    value = "us west 1"
    env = env_factory(SERVICE_REGION=value)
    assert require_env(_SETTING_NAME, env=env) == value


def test_require_env_injected_mapping_overrides_process_env(
    env_factory: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected ``Mapping`` takes precedence over the process environment (FR-3.1)."""
    monkeypatch.setenv("SERVICE_REGION", "from-process-env")
    env = env_factory(SERVICE_REGION="from-injected-map")
    assert require_env(_SETTING_NAME, env=env) == "from-injected-map"


def test_require_env_none_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``env=None`` the reader consults the process ``os.environ`` (FR-3.1)."""
    monkeypatch.setenv("SERVICE_REGION", "us-west1")
    assert require_env(_SETTING_NAME) == "us-west1"


def test_require_env_none_fails_closed_when_process_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``env=None`` and the process var unset, startup fails closed (FR-3.1)."""
    monkeypatch.delenv("SERVICE_REGION", raising=False)
    with pytest.raises(ConfigError):
        require_env(_SETTING_NAME)


# --------------------------------------------------------------------------------------------------
# ConfigError message hygiene (FR-3.2, NFR-1)
# --------------------------------------------------------------------------------------------------


def test_config_error_message_names_the_env_var(env_factory: EnvFactory) -> None:
    """The ``ConfigError`` message names the referenced env var (FR-3.2)."""
    env = env_factory()
    with pytest.raises(ConfigError) as exc_info:
        require_env(_SETTING_NAME, env=env)
    assert _SETTING_NAME in str(exc_info.value)


def test_config_error_message_carries_no_value(env_factory: EnvFactory) -> None:
    """The rejected whitespace-only value never leaks into the error message (FR-3.2, NFR-1)."""
    rejected = " \t\n "  # whitespace-only: fails closed, and its raw bytes must not surface
    env = env_factory(SERVICE_REGION=rejected)
    with pytest.raises(ConfigError) as exc_info:
        require_env(_SETTING_NAME, env=env)
    message = str(exc_info.value)
    # The env var name is named, but the (blank) value itself never appears in the message.
    assert _SETTING_NAME in message
    assert rejected not in message


# --------------------------------------------------------------------------------------------------
# Frozen ServiceConfig shape (FR-3.3)
# --------------------------------------------------------------------------------------------------


def test_service_config_is_a_dataclass() -> None:
    """``ServiceConfig`` is a dataclass (FR-3.3)."""
    assert dataclasses.is_dataclass(ServiceConfig)


def test_service_config_carries_only_container_port() -> None:
    """``ServiceConfig`` declares exactly ``container_port: int`` — no secret, no host (FR-3.3)."""
    fields = dataclasses.fields(ServiceConfig)
    names = [field.name for field in fields]
    assert names == ["container_port"]
    (container_port_field,) = fields
    assert container_port_field.type in (int, "int")


def test_service_config_container_port_is_int() -> None:
    """A constructed ``ServiceConfig`` holds an ``int`` container port (FR-3.3)."""
    conf = ServiceConfig(container_port=8080)
    assert conf.container_port == 8080
    assert isinstance(conf.container_port, int)


def test_service_config_is_frozen() -> None:
    """Assigning to a ``ServiceConfig`` field raises ``FrozenInstanceError`` (FR-3.3)."""
    conf = ServiceConfig(container_port=8080)
    with pytest.raises(dataclasses.FrozenInstanceError):
        conf.container_port = 9090  # type: ignore[misc]


# --------------------------------------------------------------------------------------------------
# ServiceConfig.from_env delegation to resolve_port (FR-3.4)
# --------------------------------------------------------------------------------------------------


def test_from_env_resolves_container_port_from_injected_map(env_factory: EnvFactory) -> None:
    """``from_env`` resolves ``container_port`` from an injected ``PORT`` map (FR-3.4)."""
    env = env_factory(PORT="9090")
    conf = ServiceConfig.from_env(env=env)
    assert conf.container_port == 9090


def test_from_env_defaults_container_port_when_port_unset(env_factory: EnvFactory) -> None:
    """``from_env`` falls back to :data:`CONTAINER_PORT` when ``PORT`` is unset (FR-3.4)."""
    env = env_factory()  # PORT absent
    conf = ServiceConfig.from_env(env=env)
    assert conf.container_port == CONTAINER_PORT


def test_from_env_matches_resolve_port(env_factory: EnvFactory) -> None:
    """``from_env`` delegates to ``resolve_port``: the two agree for the same env (FR-3.4)."""
    env = env_factory(PORT="12345")
    assert ServiceConfig.from_env(env=env).container_port == resolve_port(env)


def test_from_env_propagates_resolve_port_failure(env_factory: EnvFactory) -> None:
    """A malformed ``PORT`` fails fast through ``from_env`` via ``resolve_port`` (FR-3.4)."""
    env = env_factory(PORT="not-a-port")
    with pytest.raises(ValueError):
        ServiceConfig.from_env(env=env)


def test_from_env_delegates_to_resolve_port(
    env_factory: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``from_env`` delegates port resolution to ``resolve_port``, not a reimplementation (FR-3.4).

    A spy replaces ``config.resolve_port`` and records the env it receives. ``from_env`` must call
    it, pass the same env object through, and adopt its return value verbatim as ``container_port``;
    a private reimplementation of the port logic would not route through the spy.
    """
    seen: list[Mapping[str, str] | None] = []
    sentinel_port = 54321

    def spy(env: Mapping[str, str] | None = None) -> int:
        seen.append(env)
        return sentinel_port

    monkeypatch.setattr(config, "resolve_port", spy)
    env = env_factory(PORT="9090")
    conf = ServiceConfig.from_env(env=env)
    assert conf.container_port == sentinel_port
    assert seen == [env]


def test_from_env_reads_no_secret(env_factory: EnvFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """``from_env`` reads no secret: an ``API_TOKEN`` in the env never lands on the object (FR-3.4).

    ``ServiceConfig`` carries only ``container_port``, so a bearer token present in the environment
    is neither read nor stored anywhere on the built instance (NFR-1).
    """
    token = "s3cr3t-token-value"  # pragma: allowlist secret
    monkeypatch.setenv("API_TOKEN", token)
    env = env_factory(PORT="8080", API_TOKEN=token)
    conf = ServiceConfig.from_env(env=env)
    assert dataclasses.asdict(conf) == {"container_port": 8080}
    assert token not in repr(conf)


def test_from_env_none_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``env=None`` ``from_env`` resolves the port from the process ``os.environ`` (FR-3.4)."""
    monkeypatch.setenv("PORT", "7070")
    conf = ServiceConfig.from_env()
    assert conf.container_port == 7070

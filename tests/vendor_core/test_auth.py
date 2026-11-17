"""Bearer-token authentication suite for ``instrument_api_core.auth`` (Task 3).

This is the security-critical suite. It pins four contracts of the shared auth base:

- the fail-closed startup matrix (``load_expected_token`` refuses to run with the token unset,
  empty, or whitespace-only), asserting the raised message names the env var and never a value;
- the success path (a non-empty value returned unchanged, an injected ``Mapping`` overriding the
  process env, and the ``env=None`` process-env branch);
- the header-rejection matrix and case-insensitive scheme match of ``verify_bearer_token``;
- the constant-time compare, including the non-ASCII presented token that must produce a clean
  ``Unauthorized`` and never a ``UnicodeError`` or any other escaping exception.

It uses the standard library plus pytest only and stays fully offline (NFR-7). Every assertion
guards the no-token-in-message / no-token-in-log rule the reference contract requires (NFR-1): a
token value never appears in an ``AuthConfigError`` or an ``Unauthorized`` message. The
``env_factory`` fixture (conftest) injects a ``dict[str, str]`` so no test mutates ``os.environ``
except the single ``env=None`` case, which uses ``monkeypatch.setenv``.
"""

import unicodedata

import pytest
from inheco_tec_api._vendor.instrument_api_core import auth
from inheco_tec_api._vendor.instrument_api_core.auth import (
    DEFAULT_TOKEN_ENV_VAR,
    AuthConfigError,
    Unauthorized,
    load_expected_token,
    verify_bearer_token,
)

from vendor_core.conftest import EnvFactory

# A well-formed opaque token used across the header and compare cases. Not a real secret.
_EXPECTED_TOKEN: str = "s3cr3t-token-value"  # pragma: allowlist secret
_UNAUTHORIZED_MESSAGE: str = "missing or invalid bearer token"


# --------------------------------------------------------------------------------------------------
# Surface (FR-2, FR-2.1)
# --------------------------------------------------------------------------------------------------


def test_module_exposes_the_documented_auth_surface() -> None:
    """The module exposes the five documented auth names on import (FR-2)."""
    assert hasattr(auth, "DEFAULT_TOKEN_ENV_VAR")
    assert hasattr(auth, "AuthConfigError")
    assert hasattr(auth, "Unauthorized")
    assert hasattr(auth, "load_expected_token")
    assert hasattr(auth, "verify_bearer_token")


def test_default_token_env_var_is_api_token() -> None:
    """``DEFAULT_TOKEN_ENV_VAR`` is exactly ``"API_TOKEN"`` (FR-2.1)."""
    assert DEFAULT_TOKEN_ENV_VAR == "API_TOKEN"


def test_auth_config_error_is_a_runtime_error() -> None:
    """``AuthConfigError`` is an ``Exception`` subclass usable as a startup failure (FR-2)."""
    assert issubclass(AuthConfigError, Exception)


def test_unauthorized_is_an_exception() -> None:
    """``Unauthorized`` is an ``Exception`` subclass the api maps to HTTP 401 (FR-2)."""
    assert issubclass(Unauthorized, Exception)


# --------------------------------------------------------------------------------------------------
# Fail-closed startup matrix for load_expected_token (FR-2.2, FR-2.4, NFR-1)
# --------------------------------------------------------------------------------------------------


def test_load_expected_token_unset_var_fails_closed(env_factory: EnvFactory) -> None:
    """An injected env map with the var absent raises ``AuthConfigError`` (FR-2.2)."""
    env = env_factory()  # the token var is not present at all
    with pytest.raises(AuthConfigError):
        load_expected_token(env=env)


def test_load_expected_token_empty_value_fails_closed(env_factory: EnvFactory) -> None:
    """An injected empty value raises ``AuthConfigError`` (fail closed) (FR-2.2)."""
    env = env_factory(API_TOKEN="")
    with pytest.raises(AuthConfigError):
        load_expected_token(env=env)


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n", " \t\n "])
def test_load_expected_token_whitespace_only_fails_closed(
    blank: str, env_factory: EnvFactory
) -> None:
    """An injected whitespace-only value raises ``AuthConfigError`` (fail closed) (FR-2.2)."""
    env = env_factory(API_TOKEN=blank)
    with pytest.raises(AuthConfigError):
        load_expected_token(env=env)


def test_fail_closed_message_names_the_env_var(env_factory: EnvFactory) -> None:
    """The ``AuthConfigError`` message names the referenced env var (FR-2.4)."""
    env = env_factory()
    with pytest.raises(AuthConfigError) as exc_info:
        load_expected_token(env=env)
    assert DEFAULT_TOKEN_ENV_VAR in str(exc_info.value)


def test_fail_closed_message_names_a_custom_env_var(env_factory: EnvFactory) -> None:
    """The message names a caller-supplied env var, not just the default (FR-2.4)."""
    env = env_factory()
    with pytest.raises(AuthConfigError) as exc_info:
        load_expected_token("PRINTER_TOKEN", env=env)  # pragma: allowlist secret
    assert "PRINTER_TOKEN" in str(exc_info.value)


def test_fail_closed_message_carries_no_token_value(env_factory: EnvFactory) -> None:
    """The rejected whitespace-only value never leaks into the error message (FR-2.4, NFR-1)."""
    rejected = " \t\n "  # whitespace-only: fails closed, and its raw bytes must not surface
    env = env_factory(API_TOKEN=rejected)  # pragma: allowlist secret
    with pytest.raises(AuthConfigError) as exc_info:
        load_expected_token(env=env)
    message = str(exc_info.value)
    # The env var name is named, but the (blank) value itself never appears in the message.
    assert DEFAULT_TOKEN_ENV_VAR in message
    assert rejected not in message


# --------------------------------------------------------------------------------------------------
# Success path for load_expected_token (FR-2.3)
# --------------------------------------------------------------------------------------------------


def test_load_expected_token_returns_value_unchanged(env_factory: EnvFactory) -> None:
    """A non-empty value is returned byte-for-byte unchanged (FR-2.3)."""
    env = env_factory(API_TOKEN=_EXPECTED_TOKEN)  # pragma: allowlist secret
    assert load_expected_token(env=env) == _EXPECTED_TOKEN


def test_load_expected_token_preserves_surrounding_content(env_factory: EnvFactory) -> None:
    """A value whose non-blank content has inner spacing is returned unchanged (FR-2.3)."""
    padded = "  token with spaces  "  # pragma: allowlist secret
    env = env_factory(API_TOKEN=padded)
    # The value is non-empty after strip(), so it is returned exactly as supplied (no trimming).
    assert load_expected_token(env=env) == padded


def test_injected_mapping_overrides_process_env(
    env_factory: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected ``Mapping`` takes precedence over the process environment (FR-2.3)."""
    monkeypatch.setenv("API_TOKEN", "from-process-env")  # pragma: allowlist secret
    env = env_factory(API_TOKEN="from-injected-map")  # pragma: allowlist secret
    assert load_expected_token(env=env) == "from-injected-map"


def test_env_none_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``env=None`` the loader reads the process ``os.environ`` (FR-2.3)."""
    monkeypatch.setenv("API_TOKEN", _EXPECTED_TOKEN)
    assert load_expected_token() == _EXPECTED_TOKEN


def test_env_none_fails_closed_when_process_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``env=None`` and the process var unset, startup fails closed (FR-2.2, FR-2.3)."""
    monkeypatch.delenv("API_TOKEN", raising=False)
    with pytest.raises(AuthConfigError):
        load_expected_token()


# --------------------------------------------------------------------------------------------------
# Header-rejection matrix for verify_bearer_token (FR-2.5)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Basic x",  # non-bearer scheme
        "bearer",  # scheme only, no token after the delimiter
        "Bearer",  # scheme only, different case
        "Bearer ",  # trailing space, empty token
        "Bearer   ",  # whitespace-only token
        "Bearer \t",  # whitespace-only token (tab)
    ],
)
def test_verify_rejects_malformed_headers(header: str | None) -> None:
    """Absent, empty, wrong-scheme, and scheme-only/blank-token headers raise ``Unauthorized``."""
    with pytest.raises(Unauthorized):
        verify_bearer_token(header, _EXPECTED_TOKEN)


@pytest.mark.parametrize("scheme", ["bearer", "Bearer", "BEARER", "BeArEr"])
def test_verify_scheme_match_is_case_insensitive(scheme: str) -> None:
    """The scheme match is case-insensitive per RFC 7235; a matching token passes (FR-2.5)."""
    # A correct token under any scheme casing returns None: it must not raise (the ``-> None``
    # return is proven by the type checker, so the assertion is the absence of an exception).
    verify_bearer_token(f"{scheme} {_EXPECTED_TOKEN}", _EXPECTED_TOKEN)


def test_verify_wrong_scheme_with_valid_token_is_rejected() -> None:
    """A valid token under a non-bearer scheme is still rejected (FR-2.5)."""
    with pytest.raises(Unauthorized):
        verify_bearer_token(f"Basic {_EXPECTED_TOKEN}", _EXPECTED_TOKEN)


# --------------------------------------------------------------------------------------------------
# Constant-time compare and non-ASCII clean reject (FR-2.6)
# --------------------------------------------------------------------------------------------------


def test_verify_matching_token_returns_none() -> None:
    """A well-formed matching bearer token returns ``None`` (no raise) (FR-2.6)."""
    # The ``-> None`` return is proven by the type checker; the assertion is that no exception
    # escapes for a matching token.
    verify_bearer_token(f"Bearer {_EXPECTED_TOKEN}", _EXPECTED_TOKEN)


def test_verify_mismatched_token_raises_unauthorized() -> None:
    """A well-formed but non-matching token raises ``Unauthorized`` (FR-2.6)."""
    with pytest.raises(Unauthorized):
        verify_bearer_token("Bearer wrong-token", _EXPECTED_TOKEN)


def test_verify_token_length_mismatch_raises_unauthorized() -> None:
    """A differing-length token compares False under ``compare_digest`` (FR-2.6)."""
    with pytest.raises(Unauthorized):
        verify_bearer_token(f"Bearer {_EXPECTED_TOKEN}extra", _EXPECTED_TOKEN)


def test_verify_non_ascii_token_is_a_clean_unauthorized() -> None:
    """A non-ASCII presented token raises ``Unauthorized``, never a ``UnicodeError`` (FR-2.6).

    The compare is over UTF-8 bytes, so ``"café"`` must produce a clean 401-style reject rather
    than an encoding error. The assertion below confirms only ``Unauthorized`` escapes.
    """
    with pytest.raises(Unauthorized):
        verify_bearer_token("Bearer café", _EXPECTED_TOKEN)


def test_verify_non_ascii_token_raises_no_other_exception() -> None:
    """Only ``Unauthorized`` escapes for a non-ASCII token: no ``UnicodeError`` or other type."""
    try:
        verify_bearer_token("Bearer café", _EXPECTED_TOKEN)
    except Unauthorized:
        pass  # the one and only permitted escape
    except BaseException as exc:  # pragma: no cover - fails loudly if any other type escapes
        pytest.fail(f"expected only Unauthorized, got {type(exc).__name__}: {exc}")
    else:  # pragma: no cover - a non-matching token must not pass silently
        pytest.fail("expected Unauthorized for a non-ASCII presented token")


def test_verify_non_ascii_expected_token_matches_itself() -> None:
    """A non-ASCII expected token still round-trips a byte-identical presented token (FR-2.6)."""
    non_ascii_token = "café-token-café"  # pragma: allowlist secret
    # A byte-identical non-ASCII token compares True under the UTF-8 ``compare_digest``: no raise.
    verify_bearer_token(f"Bearer {non_ascii_token}", non_ascii_token)


# --------------------------------------------------------------------------------------------------
# Partition and whitespace semantics of the header split (FR-2.5, FR-2.6)
#
# ``verify_bearer_token`` splits the header with ``partition(" ")`` (first space only) and gates the
# blank-token check on a stripped COPY while comparing the RAW ``presented`` slice. These cases pin
# that behaviour: a token may legitimately contain inner spaces, extra/edge whitespace is NOT
# silently trimmed before the constant-time compare, and a leading space voids the scheme.
# --------------------------------------------------------------------------------------------------


def test_verify_token_with_inner_spaces_matches() -> None:
    """A token with inner spaces round-trips: ``partition`` splits on the first space only (FR-2.6).

    Everything after the first delimiter space is the opaque token, inner spaces included, so a
    byte-identical presented token compares True and no exception escapes.
    """
    token_with_spaces = "tok en with spaces"  # pragma: allowlist secret
    verify_bearer_token(f"Bearer {token_with_spaces}", token_with_spaces)


def test_verify_extra_space_after_scheme_becomes_part_of_token() -> None:
    """A second space after the scheme is preserved in the token, so it no longer matches (FR-2.6).

    ``"Bearer  token"`` partitions to a presented value of ``" token"`` (leading space kept); that
    differs byte-for-byte from ``"token"`` and is rejected. The extra space is not collapsed.
    """
    with pytest.raises(Unauthorized):
        verify_bearer_token(f"Bearer  {_EXPECTED_TOKEN}", _EXPECTED_TOKEN)


def test_verify_trailing_whitespace_in_token_is_not_stripped_before_compare() -> None:
    """A trailing space on the token is compared literally, not trimmed away (FR-2.6).

    The blank-token guard strips a copy, but the compare uses the raw ``presented`` slice. So
    ``"Bearer token "`` (trailing space) must NOT match a stored ``"token"``; trimming here would
    silently widen the accepted-token set.
    """
    with pytest.raises(Unauthorized):
        verify_bearer_token(f"Bearer {_EXPECTED_TOKEN} ", _EXPECTED_TOKEN)


def test_verify_leading_whitespace_header_voids_the_scheme() -> None:
    """A header with a leading space partitions to an empty scheme and is rejected (FR-2.5).

    ``" Bearer token"`` splits to scheme ``""`` (before the first space), which fails the
    case-insensitive ``bearer`` scheme match, so a leading space alone denies the request.
    """
    with pytest.raises(Unauthorized):
        verify_bearer_token(f" Bearer {_EXPECTED_TOKEN}", _EXPECTED_TOKEN)


def test_verify_unicode_normalization_forms_do_not_match() -> None:
    """Canonically-equivalent but byte-distinct Unicode forms are rejected (FR-2.6, NFR-1).

    The compare is byte-exact over UTF-8 with no normalization step, so a composed (NFC) ``café``
    token does not match a decomposed (NFD) expected token even though they render identically. An
    attacker cannot substitute a normalization-equivalent token, and the fixed message leaks
    neither form.
    """
    nfc_token = unicodedata.normalize("NFC", "café-token")  # pragma: allowlist secret
    nfd_token = unicodedata.normalize("NFD", "café-token")  # pragma: allowlist secret
    assert nfc_token != nfd_token  # guard: the two forms really are byte-distinct
    with pytest.raises(Unauthorized) as exc_info:
        verify_bearer_token(f"Bearer {nfc_token}", nfd_token)
    message = str(exc_info.value)
    assert message == _UNAUTHORIZED_MESSAGE
    assert nfc_token not in message


# --------------------------------------------------------------------------------------------------
# Unauthorized message hygiene (FR-2.7, NFR-1)
# --------------------------------------------------------------------------------------------------


def test_unauthorized_message_is_the_fixed_safe_string() -> None:
    """``Unauthorized`` carries exactly the fixed safe message (FR-2.7)."""
    assert str(Unauthorized()) == _UNAUTHORIZED_MESSAGE


def test_unauthorized_from_verify_carries_no_token_or_request_detail() -> None:
    """An ``Unauthorized`` raised by ``verify`` names no token value or request detail (NFR-1)."""
    presented = "attacker-supplied-token"  # pragma: allowlist secret
    with pytest.raises(Unauthorized) as exc_info:
        verify_bearer_token(f"Bearer {presented}", _EXPECTED_TOKEN)
    message = str(exc_info.value)
    assert message == _UNAUTHORIZED_MESSAGE
    assert presented not in message
    assert _EXPECTED_TOKEN not in message


def test_unauthorized_message_stable_across_reject_branches() -> None:
    """Every rejection branch yields the identical fixed message: no branch leaks detail (NFR-1)."""
    headers: list[str | None] = [None, "", "Basic x", "bearer", "Bearer   ", "Bearer wrong"]
    messages: set[str] = set()
    for header in headers:
        with pytest.raises(Unauthorized) as exc_info:
            verify_bearer_token(header, _EXPECTED_TOKEN)
        messages.add(str(exc_info.value))
    assert messages == {_UNAUTHORIZED_MESSAGE}

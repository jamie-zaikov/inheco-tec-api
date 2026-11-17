"""The ``instrument.yaml`` manifest: models, loader and fail-closed validation (design C2).

:func:`load_manifest` reads the manifest from the path in ``INHECO_TEC_MANIFEST`` (default
``../instrument.yaml`` relative to the ``api/`` directory), parses it with a duplicate-key-rejecting
``SafeLoader`` (:mod:`inheco_tec_api.manifest_yaml`), and validates it with strict pydantic v2
models that forbid unknown keys (FR-2, FR-2.5, FR-2.8). Every failure raises
:class:`ManifestError` with the dotted key path of the offending entry, for example
``controllers[0].slots[1].max_c``; no error carries an input value, so no secret value can leak
(NFR-1). The manifest itself holds secrets only by reference: the bearer token by env-var name and
the TLS certificate and key by file path (FR-2.4).

The env var ``INHECO_TEC_TRANSPORT`` (``hidapi`` | ``fake``) overrides ``transport.kind``; no other
path selects the fake transport (FR-13.4).
"""

from __future__ import annotations

import math
import os
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import ErrorDetails

from inheco_tec_api.limits import CLASS_TABLE
from inheco_tec_api.manifest_yaml import DuplicateKeyError, load_unique

MANIFEST_ENV_VAR: Final = "INHECO_TEC_MANIFEST"
TRANSPORT_ENV_VAR: Final = "INHECO_TEC_TRANSPORT"

DEFAULT_MANIFEST_PATH: Final = Path(__file__).resolve().parents[2] / "instrument.yaml"
"""``../instrument.yaml`` relative to the ``api/`` directory of the checkout (design C2)."""

TRANSPORT_KINDS: Final = ("hidapi", "fake")

# Transport floors (FR-5.2, FR-5.6). Zero is the only value below a floor, and only for the fake.
PRE_SEND_DELAY_FLOOR_MS: Final = 100
RETRY_DELAY_FLOOR_MS: Final = 1000

DEFAULT_MIN_C: Final = 4.0
"""Configured minimum when a slot omits ``min_c`` (design C2, DD-12)."""

_ARTICLE_DIGITS: Final = 7
_ENV_NAME_PATTERN: Final = r"^[A-Z_][A-Z0-9_]*$"


class ManifestError(Exception):
    """The manifest is missing or invalid; the service must not start (FR-2.5, FR-2.8).

    ``key_path`` is the dotted path of the offending key (``manifest`` for a whole-file failure);
    ``reason`` says what is wrong. Neither ever holds a configured value.
    """

    def __init__(self, key_path: str, reason: str) -> None:
        super().__init__(f"{key_path}: {reason}")
        self.key_path = key_path
        self.reason = reason


def _to_tenths(value: float) -> int:
    """Return ``value`` °C as integer tenths, or raise if it is not finite or not a 0.1 multiple."""
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    tenths = Decimal(repr(float(value))) * 10
    if tenths != tenths.to_integral_value():
        raise ValueError("must be a multiple of 0.1")
    return int(tenths)


def _check_tenths(value: float) -> float:
    _to_tenths(value)
    return value


Celsius = Annotated[float, AfterValidator(_check_tenths)]
"""A temperature in °C with 0.1 °C resolution (FR-2.2)."""

LimitClassName = Literal["cpac", "cpac-2tec"]
NonEmptyStr = Annotated[str, Field(min_length=1)]


class _Model(BaseModel):
    # strict: no silent coercion (a quoted "8743" port or a bool temperature is an error);
    # extra="forbid": an unknown or misspelt key is an error (FR-2.5).
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ProtocolConfig(_Model):
    api: Literal["https-rest"]
    instrument: Literal["usb-hid"]


class AuthConfig(_Model):
    bearer_token_ref: Annotated[str, Field(pattern=_ENV_NAME_PATTERN)]  # pragma: allowlist secret
    """Env-var name that the service reads the API bearer credential from (FR-2.4).

    The manifest holds only this name, never the credential itself.
    """


class TlsConfig(_Model):
    cert_file: NonEmptyStr
    key_file: NonEmptyStr


class ServiceConfig(_Model):
    bind: NonEmptyStr
    env_file: NonEmptyStr | None = None
    state_file: NonEmptyStr
    shutdown_timeout_s: Annotated[int, Field(ge=1)] = 10


class TransportConfig(_Model):
    """Transport timing and retry settings; every default meets the FR-5 floors (FR-2.3).

    The delay floors depend on the effective kind, so :func:`load_manifest` checks them after the
    ``INHECO_TEC_TRANSPORT`` override; the model checks only the kind-independent ranges.
    """

    kind: Literal["hidapi", "fake"] = "hidapi"
    pre_send_delay_ms: Annotated[int, Field(ge=0)] = 200
    read_timeout_ms: Annotated[int, Field(ge=500, le=10000)] = 3000
    retry_delay_ms: Annotated[int, Field(ge=0)] = 1000
    max_attempts: Annotated[int, Field(ge=1, le=11)] = 3
    max_stale_reads: Annotated[int, Field(ge=1, le=10)] = 3
    lock_wait_s: Annotated[int, Field(ge=1, le=120)] = 16


class ArticleEntry(_Model):
    """One article-number map entry (FR-2.7): the limit class and the variant name."""

    limit_class: LimitClassName = Field(alias="class")
    variant: NonEmptyStr


class SlotConfig(_Model):
    """One configured slot (FR-2.2, FR-2.3). ``min_c``/``max_c`` default per DD-12."""

    id: Annotated[int, Field(ge=1, le=6)]
    limit_class: LimitClassName = Field(alias="class")
    min_c: Celsius
    max_c: Celsius
    at_target_tolerance_c: Annotated[float, Field(ge=0.1, le=5.0, allow_inf_nan=False)] = 1.0

    @model_validator(mode="before")
    @classmethod
    def _apply_limit_defaults(cls, data: Any) -> Any:
        """Fill an omitted ``min_c`` with 4.0 and ``max_c`` with the class default max (DD-12).

        With an unknown class the maximum stays omitted; the class error is reported first.
        """
        if not isinstance(data, dict):
            return data
        filled = dict(data)
        filled.setdefault("min_c", DEFAULT_MIN_C)
        class_name = filled.get("class")
        limit_class = CLASS_TABLE.get(class_name) if isinstance(class_name, str) else None
        if limit_class is not None:
            filled.setdefault("max_c", limit_class.default_max_tenths / 10)
        return filled

    @property
    def min_tenths(self) -> int:
        """Configured minimum in integer tenths of °C."""
        return _to_tenths(self.min_c)

    @property
    def max_tenths(self) -> int:
        """Configured maximum in integer tenths of °C."""
        return _to_tenths(self.max_c)


class ControllerConfig(_Model):
    id: Annotated[int, Field(ge=0, le=7)]
    slots: Annotated[list[SlotConfig], Field(min_length=1)]


class RefsConfig(_Model):
    linear_issue: NonEmptyStr
    vault: NonEmptyStr
    notion_hub: str = ""


class Manifest(_Model):
    """The whole ``instrument.yaml`` (design C2 schema)."""

    slug: NonEmptyStr
    title: NonEmptyStr
    hosting: Literal["host-resident"]
    host: NonEmptyStr
    port: Annotated[int, Field(ge=1, le=65535)]
    protocol: ProtocolConfig
    auth: AuthConfig
    tls: TlsConfig
    service: ServiceConfig
    transport: TransportConfig = TransportConfig()
    article_numbers: dict[str, ArticleEntry]
    controllers: Annotated[list[ControllerConfig], Field(min_length=1)]
    capabilities: list[NonEmptyStr]
    refs: RefsConfig


def _format_loc(loc: tuple[int | str, ...]) -> str:
    """Render a pydantic error location as a dotted key path: ``controllers[0].slots[1].max_c``."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif part == "[key]":
            continue
        else:
            path = f"{path}.{part}" if path else part
    return path or "manifest"


def _reason(error: ErrorDetails) -> str:
    """A reason for one pydantic error, built from its type and message only (never its input)."""
    kind = error["type"]
    if kind == "extra_forbidden":
        return "unknown key"
    if kind == "missing":
        return "required key is missing"
    message = str(error["msg"])
    return message.removeprefix("Value error, ")


def _check_article_keys(data: dict[str, Any]) -> None:
    """Article keys must be quoted strings of exactly 7 decimal digits (FR-2.8)."""
    articles = data.get("article_numbers")
    if not isinstance(articles, dict):
        return
    for key in articles:
        if not isinstance(key, str):
            raise ManifestError(
                f"article_numbers.{key}",
                "article number key must be a string of exactly 7 digits; quote the key",
            )
        if len(key) != _ARTICLE_DIGITS or not (key.isascii() and key.isdigit()):
            raise ManifestError(
                f"article_numbers.{key}", "article number key must be exactly 7 decimal digits"
            )


def _check_controllers(manifest: Manifest) -> None:
    """Unique ids and class bounds of the controller and slot map (FR-2.2, FR-2.5, FR-7.3)."""
    controller_ids: set[int] = set()
    for c_index, controller in enumerate(manifest.controllers):
        c_path = f"controllers[{c_index}]"
        if controller.id in controller_ids:
            raise ManifestError(f"{c_path}.id", f"duplicate controller id {controller.id}")
        controller_ids.add(controller.id)
        slot_ids: set[int] = set()
        for s_index, slot in enumerate(controller.slots):
            s_path = f"{c_path}.slots[{s_index}]"
            if slot.id in slot_ids:
                raise ManifestError(f"{s_path}.id", f"duplicate slot id {slot.id} in controller")
            slot_ids.add(slot.id)
            _check_slot_bounds(slot, s_path)


def _check_slot_bounds(slot: SlotConfig, s_path: str) -> None:
    """``min_c``/``max_c`` inside the class bounds of the C8 table, and ``min_c <= max_c``."""
    bounds = CLASS_TABLE[slot.limit_class]
    low, high = bounds.min_tenths / 10, bounds.max_tenths / 10
    window = f"class {slot.limit_class} bounds {low:.1f}-{high:.1f}"
    for field, tenths in (("min_c", slot.min_tenths), ("max_c", slot.max_tenths)):
        if not bounds.min_tenths <= tenths <= bounds.max_tenths:
            raise ManifestError(f"{s_path}.{field}", f"outside the {window}")
    if slot.min_tenths > slot.max_tenths:
        raise ManifestError(f"{s_path}.min_c", "min_c is greater than max_c")


def _effective_transport(transport: TransportConfig) -> TransportConfig:
    """Apply the ``INHECO_TEC_TRANSPORT`` override, then the kind-dependent floors (FR-5.2/5.6).

    An unset or empty env var leaves ``transport.kind`` as configured.
    """
    override = os.environ.get(TRANSPORT_ENV_VAR, "")
    if override:
        if override not in TRANSPORT_KINDS:
            raise ManifestError(
                "transport.kind", f"env {TRANSPORT_ENV_VAR} must be 'hidapi' or 'fake'"
            )
        transport = transport.model_copy(update={"kind": override})
    fake = transport.kind == "fake"
    for field, floor in (
        ("pre_send_delay_ms", PRE_SEND_DELAY_FLOOR_MS),
        ("retry_delay_ms", RETRY_DELAY_FLOOR_MS),
    ):
        value = getattr(transport, field)
        if value < floor and not (fake and value == 0):
            raise ManifestError(
                f"transport.{field}",
                f"must be >= {floor} (0 is allowed only with the fake transport)",
            )
    return transport


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ManifestError("manifest", f"file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError("manifest", f"cannot read {path}: {type(exc).__name__}") from None


def _parse(text: str) -> dict[str, Any]:
    try:
        data = load_unique(text)
    except DuplicateKeyError as exc:
        raise ManifestError(exc.key_path, f"duplicate key (line {exc.line})") from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1} column {mark.column + 1}" if mark is not None else ""
        raise ManifestError("manifest", f"unparsable YAML{where}") from None
    if not isinstance(data, dict):
        raise ManifestError("manifest", "top level must be a mapping")
    return data


def load_manifest(path: Path | None = None) -> Manifest:
    """Load and validate the manifest; raise :class:`ManifestError` on any problem (FR-2, FR-2.5).

    The path is ``path`` if given, else ``INHECO_TEC_MANIFEST`` if set, else
    :data:`DEFAULT_MANIFEST_PATH`. The returned manifest carries the effective transport kind.
    """
    if path is None:
        env_path = os.environ.get(MANIFEST_ENV_VAR, "")
        path = Path(env_path) if env_path else DEFAULT_MANIFEST_PATH
    data = _parse(_read_text(path))
    _check_article_keys(data)
    try:
        manifest = Manifest.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ManifestError(_format_loc(first["loc"]), _reason(first)) from None
    _check_controllers(manifest)
    transport = _effective_transport(manifest.transport)
    return manifest.model_copy(update={"transport": transport})

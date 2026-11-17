"""Shared helpers for the C11 ``TecService`` suites (not a test module; Task 15).

:class:`OpsRig` wires a :class:`~inheco_tec_api.operations.TecService` to a
:class:`tec_registry_helpers.RegRig` (fake devices, a recording store, virtual time). By default
controller 0 alone is configured, with CPAC slots 1 and 2 (effective limits 4.0-70.0 C, at-target
tolerance 1.0 C). The tests directory has no ``__init__.py`` (design Risk R8).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from inheco_tec_api.controller import ControllerManager
from inheco_tec_api.operations import TecService
from inheco_tec_api.transport.fake import FakeTecDevice
from inheco_tec_api.transport.fake_actions import DeviceLoss, Match
from tec_registry_helpers import RegRig

UNMAPPED_ARTICLE = "8123456"
"""An article number that is not in the manifest map: the slot is not permitted (FR-7.5)."""

RECONNECT_STOP = ["1ATE0", "0RDC1", "2ATE0", "0RDC1"]
"""The FR-11.4 / FR-17.8 ``ATE0`` sweep over the configured slots 1 and 2."""


class OpsRig:
    """A started registry over fake devices plus a :class:`TecService` on top of it."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        ids: Iterable[int] = (0,),
        start: bool = True,
        articles: Mapping[int, str] | None = None,
        **manifest_kw: Any,
    ) -> None:
        self.reg = RegRig(tmp_path, ids=ids, **manifest_kw)
        for sid, article in (articles or {}).items():
            self.reg.dev(0).slots[sid].article_number = article
        self.service = TecService(self.reg.registry)
        if start:
            self.reg.started()

    @property
    def manager(self) -> ControllerManager:
        return self.reg.manager(0)

    @property
    def dev(self) -> FakeTecDevice:
        return self.reg.dev(0)

    @property
    def commands(self) -> list[str]:
        return self.reg.commands(0)

    def mark(self) -> None:
        self.reg.mark()

    def release(self) -> None:
        """Release controller 0 through the service and forget the activity."""
        self.service.release(0)
        assert self.manager.state == "released"
        self.mark()

    def make_lost(self) -> None:
        """Lose controller 0 through a device loss during a stop, then forget the activity."""
        self.dev.script(match=Match(mnemonic="ATE"), action=DeviceLoss(), times=1)
        self.manager.stop_slots([1])
        assert self.manager.state == "lost"
        self.mark()


def flat_values(events: Iterable[Mapping[str, Any]]) -> list[str]:
    """Every key and value of every captured log event, as strings."""
    return [str(item) for event in events for pair in event.items() for item in pair]

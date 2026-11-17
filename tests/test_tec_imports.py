"""FR-1.4 banned-import scan over every module of ``inheco_tec_api`` (design C16).

The api must port the TEC domain knowledge as native code. It must not import ``pyhamilton``,
``pythonnet``/``clr``, ``win32com``/``comtypes``, or load the vendor ``InhecoMTCdll.dll``. This
suite parses (never imports) every ``.py`` file under the package and fails on:

- an ``import`` / ``from ... import`` whose top-level module is banned;
- a dynamic import (``importlib.import_module`` / ``__import__``) of a banned module name;
- any string constant that contains ``InhecoMTCdll`` (case-insensitive), which covers a
  ``ctypes`` / ``cffi`` load of the vendor DLL.

The scanner is also checked against synthetic sources, so a broken scanner cannot pass silently.
"""

import ast
from pathlib import Path

import pytest

BANNED_MODULES: frozenset[str] = frozenset(
    {"pyhamilton", "clr", "pythonnet", "win32com", "comtypes"}
)
BANNED_STRING: str = "inhecomtcdll"
_DYNAMIC_IMPORTERS: frozenset[str] = frozenset({"import_module", "__import__"})


def _top_level(module: str) -> str:
    return module.split(".", 1)[0]


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def find_violations(source: str, filename: str = "<string>") -> list[str]:
    """Return one ``file:line: reason`` entry for each banned import or DLL reference."""
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _top_level(alias.name) in BANNED_MODULES:
                    violations.append(f"{filename}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # Absolute imports only: ``from .clr import x`` names a local module, not pythonnet.
            if (
                node.level == 0
                and node.module is not None
                and _top_level(node.module) in BANNED_MODULES
            ):
                violations.append(f"{filename}:{node.lineno}: from {node.module} import")
        elif isinstance(node, ast.Call):
            if _call_name(node.func) in _DYNAMIC_IMPORTERS and node.args:
                first = node.args[0]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and _top_level(first.value) in BANNED_MODULES
                ):
                    violations.append(f"{filename}:{node.lineno}: dynamic import {first.value}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if BANNED_STRING in node.value.lower():
                violations.append(f"{filename}:{node.lineno}: references InhecoMTCdll")
    return violations


def _package_modules(package_dir: Path) -> list[Path]:
    return sorted(p for p in package_dir.rglob("*.py") if "__pycache__" not in p.parts)


def test_fr_1_4_package_has_modules_to_scan(package_dir: Path) -> None:
    modules = {p.relative_to(package_dir).as_posix() for p in _package_modules(package_dir)}
    # The scan must cover the scaffold, including the three sub-packages of Task 1.
    assert {
        "__init__.py",
        "protocol/__init__.py",
        "transport/__init__.py",
        "http/__init__.py",
    } <= modules


def test_fr_1_4_no_banned_import_in_any_module(package_dir: Path) -> None:
    violations: list[str] = []
    for path in _package_modules(package_dir):
        source = path.read_text(encoding="utf-8")
        violations.extend(find_violations(source, path.relative_to(package_dir).as_posix()))
    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        "import pyhamilton",
        "import clr",
        "import pythonnet",
        "import win32com.client",
        "import comtypes.client as cc",
        "import os, clr",
        "from pyhamilton import HamiltonInterface",
        "from win32com.client import Dispatch",
        "from comtypes import CoInitialize",
        "def f():\n    import clr\n",
        "import importlib\nimportlib.import_module('clr')",
        "from importlib import import_module\nimport_module('win32com.client')",
        "__import__('pythonnet')",
        "import ctypes\nctypes.WinDLL('InhecoMTCdll.dll')",
        "DLL = r'C:\\Program Files\\INHECO\\inhecomtcdll.dll'",
    ],
)
def test_fr_1_4_scanner_flags_banned_source(source: str) -> None:
    assert find_violations(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "import hid",
        "import ctypes",
        "import clrlib",
        "import comtypes_like",
        "from .clr import x",
        "from instrument_api_core import configure_logging",
        "NAME = 'InhecoTEC'",
        "importlib.import_module('hid')",
    ],
)
def test_fr_1_4_scanner_allows_clean_source(source: str) -> None:
    assert find_violations(source) == []

"""The environment template agrees with the code (FR-9.4, FR-9.5).

``service.env.template`` must list exactly the environment variables that the API reads. The read
set is built statically with ``ast`` over every module of ``src/inheco_tec_api`` (the vendored
service base included); nothing is imported or run:

- the first argument of ``os.environ.get(...)``, ``os.environ[...]``, ``os.getenv(...)`` and
  ``require_env(...)``;
- the first argument of ``load_expected_token(...)``, the bearer-token reader;
- an argument that is a name resolves through a module-level string constant whose name ends with
  ``_ENV_VAR`` or ``_ENV`` (in any scanned module, so an imported constant resolves too), else
  through the one assignment of that name in the same module (a local alias);
- an argument that reads ``<...>.bearer_token_ref`` resolves to ``auth.bearer_token_ref`` of the
  shipped ``instrument.yaml``.

An argument in any other form fails the test, so a new kind of read cannot slip past it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "inheco_tec_api"
TEMPLATE = REPO_ROOT / "service.env.template"
MANIFEST = REPO_ROOT / "instrument.yaml"

READ_CALLS = frozenset({"getenv", "require_env", "load_expected_token"})
CONSTANT_SUFFIXES = ("_ENV_VAR", "_ENV")


def _modules() -> list[tuple[Path, ast.Module]]:
    paths = sorted(PACKAGE_DIR.rglob("*.py"))
    assert paths, "the package holds no module"
    trees: list[tuple[Path, ast.Module]] = []
    for path in paths:
        trees.append((path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
    return trees


def _assignments(tree: ast.Module) -> dict[str, list[ast.expr]]:
    """Every ``name = value`` of one module, at any depth: name -> values."""
    found: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                found.setdefault(node.targets[0].id, []).append(node.value)
    return found


def _constants(trees: list[tuple[Path, ast.Module]]) -> dict[str, set[str]]:
    """Module-level ``*_ENV_VAR`` / ``*_ENV`` string constants: name -> values."""
    found: dict[str, set[str]] = {}
    for _path, tree in trees:
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target, value = stmt.target, stmt.value
            else:
                continue
            if (
                isinstance(target, ast.Name)
                and target.id.endswith(CONSTANT_SUFFIXES)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                found.setdefault(target.id, set()).add(value.value)
    return found


def _is_environ(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def _read_arguments(tree: ast.Module) -> list[ast.expr]:
    """The first argument of every environment read of one module."""
    args: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            args.append(node.slice)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            environ_get = (
                isinstance(func, ast.Attribute) and name == "get" and _is_environ(func.value)
            )
            if environ_get or name in READ_CALLS:
                args.append(node.args[0])
    return args


def _bearer_token_ref() -> str:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    value = manifest["auth"]["bearer_token_ref"]
    assert isinstance(value, str) and value
    return value


def _resolve(
    arg: ast.expr,
    constants: dict[str, set[str]],
    local: dict[str, list[ast.expr]],
    where: str,
    depth: int = 0,
) -> set[str]:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return {arg.value}
    if isinstance(arg, ast.Name) and arg.id in constants:
        return set(constants[arg.id])
    if isinstance(arg, ast.Attribute) and arg.attr == "bearer_token_ref":
        return {_bearer_token_ref()}
    if isinstance(arg, ast.Name) and len(local.get(arg.id, [])) == 1 and depth < 4:
        return _resolve(local[arg.id][0], constants, local, where, depth + 1)
    raise AssertionError(f"{where}: an environment read whose name the test cannot resolve")


def variables_read() -> set[str]:
    trees = _modules()
    constants = _constants(trees)
    names: set[str] = set()
    for path, tree in trees:
        local = _assignments(tree)
        for arg in _read_arguments(tree):
            where = f"{path.relative_to(REPO_ROOT)}:{getattr(arg, 'lineno', 0)}"
            names |= _resolve(arg, constants, local, where)
    return names


def template_variables() -> list[str]:
    names: list[str] = []
    for raw in TEMPLATE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, _value = line.partition("=")
        assert sep == "=", f"not a NAME=value line: {line!r}"
        names.append(name.strip())
    return names


def test_fr9_5_template_lists_exactly_the_variables_the_api_reads() -> None:
    # Rejects: a template that misses a variable the API reads, or lists one it never reads.
    listed = template_variables()
    assert len(listed) == len(set(listed)), "a variable is listed twice"
    assert set(listed) == variables_read()


def test_fr9_4_template_holds_no_value() -> None:
    # Rejects: a template line that carries a value, so no real secret can sit in it (NFR-3.1).
    for raw in TEMPLATE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            assert line.endswith("="), f"a template value must be empty: {line.partition('=')[0]}"


def test_fr9_5_read_set_holds_the_token_named_by_the_manifest() -> None:
    # Rejects: a read set that lost the bearer-token variable (the scan would then accept a
    # template without it).
    assert _bearer_token_ref() in variables_read()

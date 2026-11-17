r"""T24-C1 / FR-16.2: the section 3 lockdown blocks in ``api/README.md`` check before they change.

``icacls`` acts on the target of a junction and ``/T`` recurses through it, so a pre-planted
``C:\instrument-apis`` or ``C:\ProgramData\instrument-apis`` junction must stop the 3.1 / 3.2
blocks before any owner or ACL change. These tests read the README as text only. They check that
section 3.0 defines both guard functions, and that in each of the 3.1 and 3.2 PowerShell blocks:

- the block is one ``& { ... }`` statement with ``$ErrorActionPreference = 'Stop'`` first;
- every path is existence-checked and guarded before the first ``New-Item`` / ``icacls`` change;
- an existing target folder throws, and an existing parent is only checked, never changed;
- every ``icacls`` change targets a folder the block created and reparse-checked, uses ``/L``,
  never ``/T``, checks ``$LASTEXITCODE``, and is followed by an admin-only re-check;
- ``New-Item`` never uses ``-Force``;
- 3.2 checks ``C:\ProgramData\instrument-apis`` before it creates anything.
"""

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[1] / "README.md"

GUARD_FUNCTIONS = ("Assert-NotReparsePoint", "Assert-AdminOnlyFolder")
ICACLS_CHANGE = re.compile(r"/(setowner|inheritance|grant|deny|remove|reset)\b", re.IGNORECASE)
LOCK_BLOCKS = {
    "### 3.1": (("top", r"C:\instrument-apis"), ("root", r"C:\instrument-apis\inheco-tec")),
    "### 3.2": (
        ("parent", r"C:\ProgramData\instrument-apis"),
        ("data", r"C:\ProgramData\instrument-apis\inheco-tec"),
    ),
}


def _lines() -> list[str]:
    return README.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")


def _text() -> str:
    return "\n".join(_lines())


def _subsection(prefix: str) -> str:
    """Return the text from the H3 heading that starts with ``prefix`` to the next H2/H3."""
    lines = _lines()
    start = next(i for i, line in enumerate(lines) if line.startswith(prefix + " "))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith(("## ", "### ")):
            end = i
            break
    return "\n".join(lines[start:end])


def _powershell_block(prefix: str) -> list[str]:
    """Return the code lines (comments and blank lines removed) of the first PS block."""
    lines = _subsection(prefix).split("\n")
    start = lines.index("```powershell") + 1
    end = lines.index("```", start)
    code = [re.sub(r"\s+#\s.*$", "", line) for line in lines[start:end]]
    return [line for line in code if line.strip() and not line.strip().startswith("#")]


def _icacls_change_lines(block: list[str]) -> list[tuple[int, str]]:
    return [
        (i, line)
        for i, line in enumerate(block)
        if line.strip().startswith("icacls ") and ICACLS_CHANGE.search(line)
    ]


def _first(block: list[str], pattern: str) -> int:
    for i, line in enumerate(block):
        if re.search(pattern, line):
            return i
    pytest.fail(f"no line matches {pattern!r}")


def _guard_pattern(var: str) -> str:
    return rf"(Test-Path -LiteralPath|{'|'.join(GUARD_FUNCTIONS)}) \${var}\b"


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_block_runs_with_error_action_stop(prefix: str) -> None:
    block = _powershell_block(prefix)
    # One script block (one statement on paste), and EAP Stop is its first statement.
    assert block[0].strip() == "& {"
    assert block[1].strip() == "$ErrorActionPreference = 'Stop'"
    assert block[-1].strip() == "}"


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_block_assigns_the_documented_paths(prefix: str) -> None:
    joined = "\n".join(_powershell_block(prefix))
    for var, path in LOCK_BLOCKS[prefix]:
        assert re.search(rf"\${var}\s*=\s*'{re.escape(path)}'", joined), f"${var} is not {path}"


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_block_has_icacls_change_lines(prefix: str) -> None:
    # Guards the tests below against passing on an empty set: owner + ACL for both paths.
    changes = _icacls_change_lines(_powershell_block(prefix))
    assert len(changes) >= 4
    for var, _ in LOCK_BLOCKS[prefix]:
        assert any(re.search(rf"icacls \${var} ", line) for _, line in changes), var


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_every_path_checked_before_first_change(prefix: str) -> None:
    block = _powershell_block(prefix)
    first_change = _icacls_change_lines(block)[0][0]
    first_create = _first(block, r"\bNew-Item\b")
    for var, _ in LOCK_BLOCKS[prefix]:
        # Existence check of every path comes before the first New-Item and the first icacls change.
        test_path = _first(block, rf"Test-Path -LiteralPath \${var}\b")
        assert test_path < first_create, f"Test-Path ${var} must come before any New-Item"
        assert test_path < first_change, f"Test-Path ${var} must come before any icacls change"
        guard = _first(block, _guard_pattern(var))
        assert guard < first_change, f"guard for ${var} must come before the first icacls change"


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_existing_target_folder_throws(prefix: str) -> None:
    block = _powershell_block(prefix)
    leaf_var = LOCK_BLOCKS[prefix][1][0]
    line = block[_first(block, rf"Test-Path -LiteralPath \${leaf_var}\b")]
    assert re.search(rf"if \(Test-Path -LiteralPath \${leaf_var}\) \{{ throw ", line)


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_existing_parent_only_checked_admin_only(prefix: str) -> None:
    block = _powershell_block(prefix)
    parent_var = LOCK_BLOCKS[prefix][0][0]
    check = _first(block, rf"Assert-AdminOnlyFolder \${parent_var}\b")
    assert check < _first(block, r"\bNew-Item\b")
    assert check < _icacls_change_lines(block)[0][0]


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_each_change_target_created_and_reparse_checked_first(prefix: str) -> None:
    block = _powershell_block(prefix)
    for i, line in _icacls_change_lines(block):
        var = re.match(r"\s*icacls \$(\w+) ", line)
        assert var, f"icacls change on a non-variable path: {line.strip()}"
        name = var.group(1)
        created = _first(block, rf"New-Item -ItemType Directory -Path \${name}\b")
        reparse = _first(block, rf"({'|'.join(GUARD_FUNCTIONS)}) \${name}\b")
        assert created < i, f"icacls change on ${name} before the block created it"
        # The reparse check on the created folder sits between New-Item and the change.
        between = block[created + 1 : i]
        assert any(re.search(rf"Assert-NotReparsePoint \${name}\b", b) for b in between), name
        assert reparse < i


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_icacls_changes_never_recurse(prefix: str) -> None:
    for _, line in _icacls_change_lines(_powershell_block(prefix)):
        assert not re.search(r"\s/T\b", line, re.IGNORECASE), f"/T on a change: {line.strip()}"


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_icacls_changes_act_on_link_itself(prefix: str) -> None:
    for _, line in _icacls_change_lines(_powershell_block(prefix)):
        assert re.search(r"\s/L\b", line), f"icacls change without /L: {line.strip()}"


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_icacls_change_exit_code_checked(prefix: str) -> None:
    block = _powershell_block(prefix)
    for i, line in _icacls_change_lines(block):
        assert re.search(r"if \(\$LASTEXITCODE -ne 0\) \{ throw ", block[i + 1]), line.strip()


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_new_item_never_forced(prefix: str) -> None:
    block = _powershell_block(prefix)
    new_items = [line for line in block if re.search(r"\bNew-Item\b", line)]
    assert new_items
    for line in new_items:
        assert not re.search(r"-Force\b", line, re.IGNORECASE), f"New-Item -Force: {line.strip()}"


@pytest.mark.parametrize("prefix", sorted(LOCK_BLOCKS), ids=lambda p: p[4:])
def test_t24_c1_admin_only_recheck_after_last_lock(prefix: str) -> None:
    block = _powershell_block(prefix)
    changes = _icacls_change_lines(block)
    for var, _ in LOCK_BLOCKS[prefix]:
        last = max(i for i, line in changes if re.search(rf"icacls \${var} ", line))
        after = block[last + 1 :]
        assert any(re.search(rf"Assert-AdminOnlyFolder \${var}\b", b) for b in after), var
        assert any(re.search(rf"Get-ChildItem -LiteralPath \${var} -Force", b) for b in after), var


def test_t24_c1_section_3_0_defines_both_guards_before_3_1() -> None:
    text = _text()
    assert text.index("### 3.0 ") < text.index("### 3.1 ") < text.index("### 3.2 ")
    guards = "\n".join(_powershell_block("### 3.0"))
    for name in GUARD_FUNCTIONS:
        assert re.search(rf"^function global:{name}\(\[string\]\$Path\) \{{", guards, re.M), name


def test_t24_c1_guard_functions_throw_on_reparse_owner_and_ace() -> None:
    guards = _powershell_block("### 3.0")
    joined = "\n".join(guards)
    split = _first(guards, r"^function global:Assert-AdminOnlyFolder")
    reparse_fn = "\n".join(guards[:split])
    admin_fn = "\n".join(guards[split:])
    assert "[IO.FileAttributes]::ReparsePoint" in reparse_fn
    assert "throw " in reparse_fn
    assert re.search(r"^\s*Assert-NotReparsePoint \$Path", admin_fn, re.M)
    assert "GetOwner(" in admin_fn
    assert "S-1-5-32-544" in admin_fn and "S-1-5-18" in admin_fn
    assert admin_fn.count("throw ") >= 2  # owner check and ACE check
    assert "-ErrorAction Stop" in joined


def test_t24_c1_3_2_checks_programdata_data_root_before_creating_anything() -> None:
    block = _powershell_block("### 3.2")
    first_create = _first(block, r"\bNew-Item\b")
    assert _first(block, r"\$parent\s*=\s*'C:\\ProgramData\\instrument-apis'") < first_create
    assert _first(block, r"Test-Path -LiteralPath \$parent\b") < first_create
    assert _first(block, r"Assert-AdminOnlyFolder \$parent\b") < first_create
    # New-Item on the data root comes only after the parent exists and is checked or locked.
    create_parent = _first(block, r"New-Item .*-Path \$parent\b")
    assert create_parent < _first(block, r"New-Item .*-Path \$data\b")

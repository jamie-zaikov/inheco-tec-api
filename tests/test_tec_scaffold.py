"""Repository scaffold, packaging and CI-wiring checks (FR-1, FR-1.1, FR-1.2, FR-1.5, NFR-5).

Every check is static: it reads files from the repository and never imports ``hid`` or opens a
device, so it runs on Linux CI and on the instrument host alike. The repository is standalone:
the shared service base ships inside the package as ``inheco_tec_api._vendor``, and the paired
n8n node lives in its own repository and shares only the route contract
``contract/api-routes.json``.
"""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml
from inheco_tec_api._vendor import instrument_api_core
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_URL = "https://github.com/jamie-zaikov/inheco-tec-api"


def _pyproject(api_root: Path) -> dict[str, Any]:
    with (api_root / "pyproject.toml").open("rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)
    return data


def _requirements(api_root: Path, extra: str | None = None) -> dict[str, Requirement]:
    project = _pyproject(api_root)["project"]
    raw: list[str] = (
        project["dependencies"] if extra is None else project["optional-dependencies"][extra]
    )
    reqs = [Requirement(r) for r in raw]
    return {canonicalize_name(r.name): r for r in reqs}


def _constraints(repo_root: Path) -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for raw_line in (repo_root / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        assert sep == "==", f"constraints.txt line is not an exact pin: {line!r}"
        pins[canonicalize_name(name.strip())] = Version(version.strip())
    return pins


# --- FR-1: repository layout ---------------------------------------------------------------------


def test_fr_1_repo_has_package_manifest_service_and_readme(duo_root: Path) -> None:
    assert (duo_root / "pyproject.toml").is_file()
    assert (duo_root / "src" / "inheco_tec_api" / "__init__.py").is_file()
    assert (duo_root / "instrument.yaml").is_file()
    assert (duo_root / "service" / "inheco-tec-api.xml").is_file()
    assert (duo_root / "service.env.template").is_file()
    assert (duo_root / "README.md").is_file()
    assert (duo_root / "LICENSE").is_file()


def test_fr_1_sub_packages_exist(package_dir: Path) -> None:
    for sub in ("protocol", "transport", "http"):
        assert (package_dir / sub / "__init__.py").is_file(), sub
    assert (package_dir / "_vendor" / "__init__.py").is_file()
    # The vendored service base is a sub-package of _vendor inside the package.
    core_init = Path(instrument_api_core.__file__ or "").resolve()
    assert core_init.name == "__init__.py"
    assert core_init.parent.parent == package_dir / "_vendor"


def test_r8_tests_dir_has_no_init_and_unique_names(api_root: Path) -> None:
    tests_dir = api_root / "tests"
    assert not (tests_dir / "__init__.py").exists()
    for path in tests_dir.glob("test_*.py"):
        assert path.name.startswith("test_tec_"), path.name
    # The vendored core tests are a sub-package, so their module names cannot collide.
    assert (tests_dir / "vendor_core" / "__init__.py").is_file()


# --- FR-1.1: names, hosting and the paired repo contract ------------------------------------------


def test_fr_1_1_python_package_name_and_console_script(api_root: Path) -> None:
    project = _pyproject(api_root)["project"]
    assert project["name"] == "inheco-tec-api"
    assert project["scripts"] == {"inheco-tec-api": "inheco_tec_api.entry:main"}


def test_fr_1_1_public_license_and_urls(api_root: Path) -> None:
    project = _pyproject(api_root)["project"]
    assert project["license"] == {"text": "MIT"}
    assert project["urls"] == {"Homepage": REPO_URL, "Repository": REPO_URL}


def test_fr_1_1_paired_node_contract_snapshot(duo_root: Path) -> None:
    snapshot = json.loads((duo_root / "contract" / "api-routes.json").read_text(encoding="utf-8"))
    assert snapshot["schema"] == 1
    assert snapshot["routes"], "the route contract lists no route"


def test_fr_1_1_manifest_slug_and_host_resident_hosting(duo_root: Path) -> None:
    manifest = yaml.safe_load((duo_root / "instrument.yaml").read_text(encoding="utf-8"))
    assert manifest["slug"] == "inheco-tec"
    assert manifest["hosting"] == "host-resident"


# --- FR-1.2 / C1: declared dependencies ----------------------------------------------------------


def test_fr_1_2_no_workspace_core_dependency(api_root: Path) -> None:
    data = _pyproject(api_root)
    assert "instrument-api-core" not in _requirements(api_root)
    assert "uv" not in data.get("tool", {})
    assert data["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]


def test_fr_1_2_package_imports_the_vendored_core(package_dir: Path) -> None:
    sources = [p.read_text(encoding="utf-8") for p in package_dir.rglob("*.py")]
    vendored = re.compile(r"^\s*from\s+inheco_tec_api\._vendor\b", re.MULTILINE)
    workspace = re.compile(r"^\s*(from|import)\s+instrument_api_core\b", re.MULTILINE)
    assert any(vendored.search(s) for s in sources)
    assert not any(workspace.search(s) for s in sources)


def test_c1_runtime_dependencies_match_design(api_root: Path) -> None:
    reqs = _requirements(api_root)
    assert set(reqs) == {
        "fastapi",
        "uvicorn",
        "structlog",
        "pydantic",
        "pyyaml",
        "python-dotenv",
        "hidapi",
    }
    # Plain uvicorn, not uvicorn[standard] (design Dependencies table).
    assert reqs["uvicorn"].extras == set()


def test_c1_dev_extras_match_design(api_root: Path) -> None:
    assert set(_requirements(api_root, "dev")) == {
        "pytest",
        "pytest-cov",
        "httpx",
        "mypy",
        "ruff",
        "types-pyyaml",
    }


# --- NFR-5: platform and pins --------------------------------------------------------------------


def test_nfr_5_hidapi_is_the_only_native_hid_dependency(api_root: Path) -> None:
    names = set(_requirements(api_root))
    assert "hidapi" in names
    assert names.isdisjoint({"hid", "pywinusb", "pyusb", "libusb", "libusb1", "easyhid"})


def test_nfr_5_every_runtime_dependency_is_pinned_in_constraints(
    api_root: Path, repo_root: Path
) -> None:
    pins = _constraints(repo_root)
    for name, req in _requirements(api_root).items():
        assert name in pins, f"{name} is not pinned in constraints.txt"
        assert req.specifier.contains(pins[name]), f"{name}=={pins[name]} violates {req}"


def test_nfr_5_pins_meet_design_floors(repo_root: Path) -> None:
    floors = {
        "hidapi": "0.14.0.post4",
        "fastapi": "0.115",
        "uvicorn": "0.32",
        "structlog": "24.4",
        "pydantic": "2.9",
        "pyyaml": "6.0.2",
        "python-dotenv": "1.0.1",
    }
    pins = _constraints(repo_root)
    for name, floor in floors.items():
        assert pins[name] >= Version(floor), name


# --- CI: pinned install and the coverage gate -----------------------------------------------------


def _workflow_runs(repo_root: Path) -> list[str]:
    workflow_path = repo_root / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    return [
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]


def test_r9_ci_installs_the_package_with_the_pinned_constraints(repo_root: Path) -> None:
    runs = _workflow_runs(repo_root)
    assert any(
        re.search(r'pip install -c constraints\.txt -e "\.\[dev\]"', run) for run in runs
    ), "no step installs the package with its dev extras through constraints.txt"


def test_c16_ci_keeps_the_coverage_floor_on_framing_engine_and_limits(repo_root: Path) -> None:
    pytest_runs = [run for run in _workflow_runs(repo_root) if "--cov-fail-under=90" in run]
    assert len(pytest_runs) == 1, pytest_runs
    run = pytest_runs[0]
    for module in ("inheco_tec_api.protocol", "inheco_tec_api.engine", "inheco_tec_api.limits"):
        assert module in run, module


# --- FR-1.5: no Terraform in the repository -------------------------------------------------------


def test_fr_1_5_repo_adds_no_terraform(duo_root: Path) -> None:
    skipped = {"node_modules", ".git", ".venv", "venv"}
    for pattern in ("*.tf", "*.tfvars"):
        assert [p for p in duo_root.rglob(pattern) if skipped.isdisjoint(p.parts)] == []

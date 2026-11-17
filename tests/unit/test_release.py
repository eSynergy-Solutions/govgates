"""Release-prep checks (Q1, Q8): no workspace host, one lock file, version in one place.

Pure file inspection: no workspace, no network. Uses `git ls-files` for the tracked-file scans.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "requirements" / "jobs.txt"
VERSION_FILE = ROOT / "VERSION"
EXPECTED_DEPENDENCIES = ["-c ${workspace.file_path}/requirements/jobs.txt"]
REQUIRED_PINS = {"pyyaml", "rapidfuzz", "faker", "databricks-sdk", "mlflow"}

HOST_RE = re.compile(r"adb-\d+\.\d+\.azuredatabricks\.net", re.IGNORECASE)
NAME_RE = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
VERSION_RE = r"\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?"
PIN_RE = re.compile(rf"^(?P<name>{NAME_RE})==(?P<version>{VERSION_RE})$")
SPEC_RE = re.compile(rf"^\s*(?P<name>{NAME_RE})\s*(?P<spec>.*)$")


# ---------- helpers ----------

def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return [ROOT / p for p in out.split("\0") if p and not p.startswith(".claude/worktrees/")]


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _lock_lines() -> list[str]:
    assert LOCK.is_file(), "requirements/jobs.txt is missing"
    lines = []
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(raw)
    return lines


def _lock_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in _lock_lines():
        m = PIN_RE.match(line)
        assert m, f"requirements/jobs.txt: line is not exactly name==version: {line!r}"
        pins[_norm(m["name"])] = m["version"]
    return pins


def _release(v: str) -> tuple[int, ...]:
    """Numeric release segment of a version, e.g. '2.14.1rc1' -> (2, 14, 1)."""
    m = re.match(r"\d+(?:\.\d+)*", v.strip())
    assert m, f"unparseable version {v!r}"
    return tuple(int(p) for p in m.group(0).split("."))


def _at_least(pinned: str, minimum: str) -> bool:
    a, b = _release(pinned), _release(minimum)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) >= b + (0,) * (width - len(b))


def _pyproject_requirements() -> list[str]:
    project = _pyproject()["project"]
    reqs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        reqs.extend(group)
    return reqs


def _parse_min_specs(req: str) -> tuple[str, list[str]]:
    """Return (normalised name, [minimum versions]) for a pyproject requirement. Only '>=' is allowed."""
    m = SPEC_RE.match(req)
    assert m, f"unparseable pyproject requirement {req!r}"
    spec = m["spec"].strip()
    mins: list[str] = []
    if spec:
        for part in spec.split(","):
            part = part.strip()
            assert part.startswith(">=") and not re.match(r"^>=\s*$", part), (
                f"pyproject requirement uses an operator other than >=: {req!r}"
            )
            mins.append(part[2:].strip())
    return _norm(m["name"]), mins


def _job_environments() -> list[tuple[str, str, dict]]:
    envs = []
    for f in sorted((ROOT / "resources").glob("*.yml")):
        doc = _load_yaml(f) or {}
        jobs = (doc.get("resources") or {}).get("jobs") or {}
        for job_name, job in jobs.items():
            for env in (job or {}).get("environments") or []:
                envs.append((f.name, job_name, env))
    return envs


# ---------- 1. no workspace host ----------

def test_databricks_yml_has_no_top_level_workspace_host():
    doc = _load_yaml(ROOT / "databricks.yml")
    has_host = "host" in (doc.get("workspace") or {})
    assert not has_host, "databricks.yml: top-level workspace.host is set"


def test_databricks_yml_has_no_target_workspace_host():
    doc = _load_yaml(ROOT / "databricks.yml")
    for name, target in (doc.get("targets") or {}).items():
        # Bool first so pytest's introspection never prints the host value.
        has_host = "host" in ((target or {}).get("workspace") or {})
        assert not has_host, f"databricks.yml: targets.{name}.workspace.host is set"


def test_host_regex_allows_readme_placeholder_and_catches_real_host():
    assert not HOST_RE.search("https://<your-workspace>.azuredatabricks.net")
    # built from parts so this file never contains a matching literal (the tracked-file scan below reads it)
    assert HOST_RE.search("https://ADB-" + "1234567890" + ".12." + "azuredatabricks.net")


def test_no_tracked_file_contains_a_workspace_host():
    hits: list[str] = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue  # binary
        text = data.decode("utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), start=1):
            if HOST_RE.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{i}")
    # Paths and line numbers only; never the matched text.
    assert not hits, "workspace host found at: " + ", ".join(hits)


# ---------- 2. lock file ----------

def test_lock_file_lines_are_exact_pins():
    bad = [line for line in _lock_lines() if not PIN_RE.match(line)]
    assert not bad, f"requirements/jobs.txt lines not exactly name==version: {bad}"


@pytest.mark.parametrize(
    "line",
    ["pyyaml>=6", "pyyaml==6.*", "pyyaml[extra]==6.0", "pyyaml==6.0; python_version>'3'",
     "pyyaml @ https://x/y.whl", "pyyaml == 6.0", "pyyaml~=6.0", "pyyaml"],
)
def test_pin_regex_rejects_non_exact_forms(line):
    assert not PIN_RE.match(line)


@pytest.mark.parametrize("line", ["pyyaml==6.0.2", "databricks-sdk==0.30.0", "Foo_Bar.baz==1.2rc1"])
def test_pin_regex_accepts_exact_pins(line):
    assert PIN_RE.match(line)


def test_lock_file_pins_every_job_package():
    missing = REQUIRED_PINS - set(_lock_pins())
    assert not missing, f"requirements/jobs.txt does not pin: {sorted(missing)}"


def test_lock_file_has_no_duplicate_packages():
    names = [_norm(PIN_RE.match(line)["name"]) for line in _lock_lines() if PIN_RE.match(line)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"requirements/jobs.txt lists packages more than once: {dupes}"


# ---------- 3. job environments use the lock file ----------

def test_resources_define_job_environments():
    assert _job_environments(), "no jobs.<name>.environments found in resources/*.yml"


def test_every_job_environment_uses_the_lock_file():
    """Lead, 9 Nov: the lock file is a constraints file (-c) first, then only the bare names each job uses, so every
    version comes from requirements/jobs.txt and no job installs what it doesn't need."""
    pinned = {_norm(line.split("==")[0]) for line in _lock_lines()}
    bad = []
    for fname, job, env in _job_environments():
        deps = (env.get("spec") or {}).get("dependencies") or []
        where = f"{fname}:{job}:{env.get('environment_key')}"
        if not deps or deps[0] != EXPECTED_DEPENDENCIES[0]:
            bad.append(f"{where}: first dependency is not the lock file constraint")
            continue
        names = deps[1:]
        if not names:
            bad.append(f"{where}: installs nothing")
        for name in names:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(name)):
                bad.append(f"{where}: {name!r} is not a bare package name (versions live in the lock file)")
            elif _norm(name) not in pinned:
                bad.append(f"{where}: {name!r} is not pinned in requirements/jobs.txt")
    assert not bad, "\n".join(bad)


def test_every_job_environment_is_client_2():
    bad = [
        f"{fname}:{job}:{env.get('environment_key')}"
        for fname, job, env in _job_environments()
        if (env.get("spec") or {}).get("client") != "2"
    ]
    assert not bad, f"environments not on client \"2\": {bad}"


# ---------- 4. lock pins satisfy pyproject ranges ----------

def test_version_compare_helper():
    assert _at_least("6.0.2", "6")
    assert _at_least("0.30.0", "0.30")
    assert _at_least("2.14", "2.14.0")
    assert not _at_least("2.13.9", "2.14")
    assert not _at_least("0.29.1", "0.30")


def test_pyproject_uses_only_minimum_specifiers():
    for req in _pyproject_requirements():
        _parse_min_specs(req)  # asserts on any operator other than >=


def test_lock_pins_satisfy_pyproject_ranges():
    pins = _lock_pins()
    bad = []
    for req in _pyproject_requirements():
        name, mins = _parse_min_specs(req)
        if name not in pins:
            continue
        for minimum in mins:
            if not _at_least(pins[name], minimum):
                bad.append(f"{name}=={pins[name]} < {minimum}")
    assert not bad, f"lock pins below pyproject minimums: {bad}"


# ---------- 5. version in one place ----------

def _version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def test_version_file_is_a_release_version():
    # the value lives only in VERSION; this checks its shape, so a bump never needs a test change
    assert VERSION_FILE.is_file(), "VERSION file is missing at the repo root"
    assert re.fullmatch(r"\d+\.\d+\.\d+\n?", VERSION_FILE.read_text(encoding="utf-8"))


def test_pyproject_version_is_dynamic_from_version_file():
    data = _pyproject()
    assert "version" not in data["project"], "pyproject.toml still has a static [project].version"
    assert "version" in data["project"].get("dynamic", [])
    assert data.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("version") == {"file": "VERSION"}


def test_package_version_matches_version_file():
    import gates

    assert gates.__version__ == VERSION_FILE.read_text(encoding="utf-8").strip()


def test_no_hard_coded_version_literal_in_pyproject_or_src():
    current = _version()
    hits = []
    for path in _tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel != "pyproject.toml" and not rel.startswith("src/"):
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
            if current in line:
                hits.append(f"{rel}:{i}")
    assert not hits, f"hard-coded version {current} found at: {hits}"


# ---------- 6. dev target unchanged ----------

def test_dev_target_is_development_and_default():
    dev = (_load_yaml(ROOT / "databricks.yml").get("targets") or {}).get("dev") or {}
    assert dev.get("mode") == "development"
    assert dev.get("default") is True


# ---------- 7. public release package (Q1, D2) ----------

SHA_USES_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
# built from parts so this file itself never holds the old licence strings (the tracked-file scan below reads it)
OLD_LICENSE_MARKERS = ["Apache" + " License", "Apache" + " 2.0"]
# LOG.md / PLAN.md are internal (never exported by make_release.py) and record the licence history.
OLD_LICENSE_ALLOWED = {"CHANGELOG.md", "NOTICE", "LOG.md", "PLAN.md"}
POLYFORM_FIRST_LINE = "# PolyForm Internal Use License 1.0.0"
POLYFORM_MARKERS = [
    "<https://polyformproject.org/licenses/internal-use/1.0.0>",
    "## Internal Business Use",
    "you may not distribute the software",
]


def test_license_is_polyform_internal_use_1_0_0():
    path = ROOT / "LICENSE"
    assert path.is_file(), "LICENSE is missing at the repo root"
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == POLYFORM_FIRST_LINE, "LICENSE first line is not the PolyForm Internal Use heading"
    missing = [m for m in POLYFORM_MARKERS if m not in text]
    assert not missing, f"LICENSE is not the PolyForm Internal Use 1.0.0 text; missing: {missing}"


# sha256 of PolyForm-Internal-Use-1.0.0.md as published in github.com/polyformproject/polyform-licenses (LF endings)
POLYFORM_SHA256 = "3a4a1539d3e265d98a43af998a13e28d4f04fcc35c283bec1e9bf7a86b266ef7"


def test_license_is_the_verbatim_upstream_text():
    import hashlib

    data = (ROOT / "LICENSE").read_bytes().replace(b"\r\n", b"\n")  # a Windows checkout may hold CRLF
    assert hashlib.sha256(data).hexdigest() == POLYFORM_SHA256, "LICENSE differs from the published licence text"


def test_notice_names_polyform_licence():
    text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "PolyForm Internal Use License 1.0.0" in text


def test_notice_states_0_1_0_was_apache_2():
    text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "0.1.0" in text, "NOTICE does not mention release 0.1.0"
    assert OLD_LICENSE_MARKERS[0] + " 2.0" in text, "NOTICE does not say 0.1.0 was released under the old licence"


def test_no_tracked_file_outside_changelog_and_notice_mentions_old_licence():
    hits: list[str] = []
    for path in _tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in OLD_LICENSE_ALLOWED or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), start=1):
            if any(m in line for m in OLD_LICENSE_MARKERS):
                hits.append(f"{rel}:{i}")
    assert not hits, "old licence named outside CHANGELOG.md/NOTICE at: " + ", ".join(hits)


def test_pyproject_classifies_licence_as_proprietary():
    classifiers = _pyproject()["project"].get("classifiers", [])
    assert "License :: Other/Proprietary License" in classifiers
    assert "Private :: Do Not Upload" in classifiers  # PyPI refuses uploads carrying it: no accidental publishing


def test_notice_exists_and_is_non_empty():
    path = ROOT / "NOTICE"
    assert path.is_file(), "NOTICE is missing at the repo root"
    assert path.read_text(encoding="utf-8").strip(), "NOTICE is empty"


def test_changelog_has_heading_for_current_version():
    path = ROOT / "CHANGELOG.md"
    assert path.is_file(), "CHANGELOG.md is missing at the repo root"
    prefix = f"## [{_version()}]"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith(prefix) for line in lines), f"CHANGELOG.md has no heading starting {prefix!r}"


def test_security_md_exists_and_is_non_empty():
    path = ROOT / "SECURITY.md"
    assert path.is_file(), "SECURITY.md is missing at the repo root"
    assert path.read_text(encoding="utf-8").strip(), "SECURITY.md is empty"


def test_ci_sp_variable_has_no_default_or_lookup():
    var = ((_load_yaml(ROOT / "databricks.yml").get("variables") or {}).get("ci_sp")) or {}
    assert var, "databricks.yml: variables.ci_sp is missing"
    assert "default" not in var, "variables.ci_sp has a default; set it per target or via BUNDLE_VAR_ci_sp"
    assert "lookup" not in var, "variables.ci_sp has a lookup; set it per target or via BUNDLE_VAR_ci_sp"
    assert str(var.get("description") or "").strip(), "variables.ci_sp lost its description"


def _workflow_uses() -> list[tuple[str, str, str]]:
    found = []
    for f in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        doc = _load_yaml(f) or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                if "uses" in (step or {}):
                    found.append((f.name, job_name, str(step["uses"])))
    return found


@pytest.mark.parametrize(
    "uses",
    ["actions/checkout@v4", "databricks/setup-cli@main", "actions/checkout@" + "a" * 39,
     "actions/checkout@" + "A" * 40, "checkout@" + "a" * 40],
)
def test_sha_pin_regex_rejects_unpinned(uses):
    assert not SHA_USES_RE.match(uses)


def test_sha_pin_regex_accepts_full_sha():
    assert SHA_USES_RE.match("actions/checkout@" + "0123456789abcdef" * 2 + "01234567")


def test_workflow_actions_are_pinned_to_full_sha():
    uses = _workflow_uses()
    assert uses, "no `uses:` steps found in .github/workflows/*.yml"
    # yaml drops a trailing `# vX.Y.Z` comment, so the parsed value must be exactly owner/repo@<sha>
    bad = [f"{f}:{job}: {u}" for f, job, u in uses if not SHA_USES_RE.match(u.strip())]
    assert not bad, "actions not pinned to a 40-hex commit SHA:\n" + "\n".join(bad)


def test_ci_deploy_dev_sets_bundle_var_ci_sp_from_client_id():
    doc = _load_yaml(ROOT / ".github" / "workflows" / "ci.yml") or {}
    job = (doc.get("jobs") or {}).get("deploy-dev") or {}
    assert job, "ci.yml: job deploy-dev is missing"
    env = job.get("env") or {}
    assert env.get("BUNDLE_VAR_ci_sp") == "${{ secrets.DATABRICKS_CLIENT_ID }}"


def test_release_claude_md_has_ground_rules():
    # In this repo the public version is release/CLAUDE.md; in the exported release it has replaced CLAUDE.md.
    path = ROOT / "release" / "CLAUDE.md" if (ROOT / "release").is_dir() else ROOT / "CLAUDE.md"
    assert path.is_file(), "release/CLAUDE.md (public agent instructions) is missing"
    text = path.read_text(encoding="utf-8")
    for needle in ("Synthetic data only", "No raw values in LLM prompts"):
        assert needle in text, f"release/CLAUDE.md lacks ground rule {needle!r}"


def test_readme_has_no_todo():
    assert "TODO" not in (ROOT / "README.md").read_text(encoding="utf-8")


def test_wheel_package_data_has_prompts_and_abbreviations():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    data = cfg["package-data"].get("gates.hydration", [])
    assert "abbreviations.yaml" in data and "prompts/*.md" in data
    assert "jobs*" not in cfg["packages"]["find"].get("include", ["jobs*"])

"""scripts/make_release.py pure API (Q1, D2): exclusions, overlay, rewrites, export, checksums, tag name.

The script is not a package, so it is loaded by file path. If it is missing, every test fails (never skips).
Pure Python with tmp_path only: no git, no workspace, no network. Rewrite rules here are synthetic.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "make_release.py"

RULES = [("oldname-approve", "newname-approve"), ("oldname", "newname")]


@pytest.fixture(scope="module")
def mr():
    assert SCRIPT.is_file(), "scripts/make_release.py is missing"
    spec = importlib.util.spec_from_file_location("make_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------- is_excluded ----------

EXCLUDED = [
    "LOG.md",
    "PLAN.md",
    "spikes/abac/notes.md",
    "spikes/x.py",
    "dashboards/main.lvdash.json",
    "release/CLAUDE.md",
    "build/synthetic/claims.csv",
    "dist/govgates-0.1.0.tar.gz",
    ".claude/worktrees/side/README.md",
    "specs/dashboards.md",
    ".claude/commands/abac-spike.md",
    ".claude/commands/dash.md",
    ".claude/commands/dash-pull.md",
    ".claude/agents/dash-builder.md",
    ".claude/agents/dash-checker.md",
    ".claude/agents/dash-designer.md",
    ".claude/agents/view-writer.md",
    ".client-denylist",
    ".release-denylist",
    ".release-rewrites",
    ".env",
]

INCLUDED = [
    "CLAUDE.md",
    "README.md",
    "src/gates/hydration/approve.py",
    "tests/access/matrix.yaml",
    "config/access_model.example.yaml",
    ".claude/hooks/guard_bash.py",
    ".claude/settings.json",
    ".claude/agents/reviewer.md",
    ".claude/commands/slice.md",
    "specs/hydration.md",
    "sql/views/v_review_queue.sql",
    # exact matching, not substring
    "specs/dashboards_extra.md",
    "xLOG.md",
    "docs/LOG.md.example",
    "src/spikes_util.py",
    "builder/x.py",
]


@pytest.mark.parametrize("path", EXCLUDED)
def test_is_excluded_true(mr, path):
    assert mr.is_excluded(path) is True


@pytest.mark.parametrize("path", INCLUDED)
def test_is_excluded_false(mr, path):
    assert mr.is_excluded(path) is False


# ---------- OVERLAY ----------

def test_overlay(mr):
    assert mr.OVERLAY == {"CLAUDE.md": "release/CLAUDE.md"}


# ---------- parse_rewrites ----------

def test_parse_rewrites_basic_and_order(mr):
    text = "# comment\n\noldname-approve => newname-approve\n  oldname   =>   newname  \n"
    assert mr.parse_rewrites(text) == RULES


def test_parse_rewrites_strips_bom_and_crlf(mr):
    text = "﻿oldname-approve => newname-approve\r\noldname => newname\r\n"
    assert mr.parse_rewrites(text) == RULES


def test_parse_rewrites_empty(mr):
    assert mr.parse_rewrites("") == []
    assert mr.parse_rewrites("# only a comment\n\n   \n") == []


@pytest.mark.parametrize(
    "line",
    [
        "oldname -> newname",
        "oldname=>newname",
        "oldname",
        " => newname",
        "oldname => ",
        "oldname => newname-oldname",
        "abc => xabcx",
    ],
)
def test_parse_rewrites_rejects_bad_line(mr, line):
    with pytest.raises(ValueError):
        mr.parse_rewrites(f"oldname-approve => newname-approve\n{line}\n")


# ---------- apply_rewrites ----------

def test_apply_rewrites_in_order(mr):
    assert mr.apply_rewrites("job oldname-approve in oldname", RULES) == "job newname-approve in newname"


def test_apply_rewrites_order_matters(mr):
    rules = [("ab", "X"), ("abc", "Y")]
    assert mr.apply_rewrites("abc", rules) == "Xc"


def test_apply_rewrites_case_sensitive(mr):
    assert mr.apply_rewrites("OldName oldname OLDNAME", RULES) == "OldName newname OLDNAME"


def test_apply_rewrites_no_rules_and_idempotent(mr):
    assert mr.apply_rewrites("unchanged", []) == "unchanged"
    once = mr.apply_rewrites("oldname-approve", RULES)
    assert mr.apply_rewrites(once, RULES) == once


# ---------- is_text ----------

def test_is_text(mr):
    assert mr.is_text(b"") is True
    assert mr.is_text(b"hello\n") is True
    assert mr.is_text("café".encode()) is True
    assert mr.is_text(b"ab\0cd") is False
    assert mr.is_text(b"\xff\xfe\x00") is False
    assert mr.is_text(b"\xff\xfeabc") is False  # invalid utf-8, no NUL


def test_is_text_nul_within_first_8192(mr):
    assert mr.is_text(b"a" * 8191 + b"\0") is False


# ---------- export_tree ----------

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00oldname-approve\x00\xff"


def _make_src(root: Path) -> list[str]:
    files: dict[str, bytes] = {
        "README.md": b"Run the oldname-approve job.\n",
        "CLAUDE.md": b"internal instructions for oldname\n",
        "release/CLAUDE.md": b"Public instructions. Synthetic data only. Job: oldname-approve\n",
        "src/gates/hydration/approve.py": b"JOB = 'oldname-approve'\n",
        "LOG.md": b"private log\n",
        "PLAN.md": b"private plan\n",
        "spikes/abac/notes.md": b"spike\n",
        ".client-denylist": b"secret-name\n",
        ".release-rewrites": b"oldname => newname\n",
        "specs/dashboards.md": b"dash spec\n",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return sorted(files)


def test_export_tree(mr, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    paths = _make_src(src)
    written = mr.export_tree(src, dst, paths, RULES)

    expected = sorted(["README.md", "CLAUDE.md", "src/gates/hydration/approve.py"])
    assert written == expected

    for rel in ["LOG.md", "PLAN.md", "spikes", ".client-denylist", ".release-rewrites", "specs/dashboards.md",
                "release"]:
        assert not (dst / rel).exists(), f"{rel} must not be exported"

    assert (dst / "README.md").read_text(encoding="utf-8") == "Run the newname-approve job.\n"
    assert (dst / "src/gates/hydration/approve.py").read_text(encoding="utf-8") == "JOB = 'newname-approve'\n"
    # overlay replaces CLAUDE.md and is rewritten too
    assert (dst / "CLAUDE.md").read_text(encoding="utf-8") == (
        "Public instructions. Synthetic data only. Job: newname-approve\n"
    )


def test_export_tree_never_copies_denylist_even_if_listed(mr, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _make_src(src)
    for name in [".release-denylist", ".env"]:
        (src / name).write_text("x\n", encoding="utf-8")
    written = mr.export_tree(src, dst, [".client-denylist", ".release-denylist", ".env", "README.md"], RULES)
    assert ".client-denylist" not in written
    assert not (dst / ".client-denylist").exists()
    assert not (dst / ".release-denylist").exists()
    assert not (dst / ".env").exists()


def test_export_tree_is_idempotent(mr, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    paths = _make_src(src)
    first = mr.export_tree(src, dst, paths, RULES)
    snapshot = {p: (dst / p).read_bytes() for p in first}
    second = mr.export_tree(src, dst, paths, RULES)
    assert second == first
    assert {p: (dst / p).read_bytes() for p in second} == snapshot


def test_export_tree_refuses_non_text_files(mr, tmp_path):
    # binaries and non-UTF-8 text can't be rewritten or scanned (git grep -I skips them), so none may ship
    src = tmp_path / "src"
    paths = _make_src(src)
    (src / "docs").mkdir()
    (src / "docs" / "logo.png").write_bytes(PNG_BYTES)
    (src / "docs" / "notes.txt").write_bytes("oldname".encode("utf-16"))
    for i, extra in enumerate(["docs/logo.png", "docs/notes.txt"]):
        with pytest.raises(ValueError, match=extra):
            mr.export_tree(src, tmp_path / f"dst{i}", paths + [extra], RULES)
    assert mr.export_tree(src, tmp_path / "dst", paths, RULES)  # without them it exports


def test_export_tree_missing_overlay_source_raises(mr, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    paths = _make_src(src)
    (src / "release" / "CLAUDE.md").unlink()
    with pytest.raises(FileNotFoundError):
        mr.export_tree(src, dst, paths, RULES)


# ---------- leftover ----------

def test_leftover_lists_files_still_holding_old_strings(mr, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "z.md").write_text("still oldname here\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("clean newname\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("x = 'oldname-approve'\n", encoding="utf-8")
    assert mr.leftover(tmp_path, RULES) == ["a/z.md", "c.py"]


def test_leftover_searches_bytes_and_paths(mr, tmp_path):
    (tmp_path / "logo.bin").write_bytes(PNG_BYTES)
    (tmp_path / "oldname_notes.md").write_text("clean\n", encoding="utf-8")
    (tmp_path / "clean.md").write_text("clean\n", encoding="utf-8")
    assert mr.leftover(tmp_path, RULES) == ["logo.bin", "oldname_notes.md"]


def test_leftover_empty_after_export(mr, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    paths = _make_src(src)
    mr.export_tree(src, dst, paths, RULES)
    assert mr.leftover(dst, RULES) == []


# ---------- checksum_lines ----------

def test_checksum_lines(mr, tmp_path):
    b = tmp_path / "b.tar.gz"
    a = tmp_path / "sub" / "a.whl"
    a.parent.mkdir()

    a_bytes, b_bytes = b"\x00aaa", b"bbb"
    a.write_bytes(a_bytes)
    b.write_bytes(b_bytes)
    expected = (
        f"{hashlib.sha256(a_bytes).hexdigest()}  a.whl\n"
        f"{hashlib.sha256(b_bytes).hexdigest()}  b.tar.gz\n"
    )
    assert mr.checksum_lines([b, a]) == expected
    assert mr.checksum_lines([a, b]) == expected


# ---------- tag_name ----------

def test_tag_name(mr):
    assert mr.tag_name("0.1.0") == "v0.1.0"
    assert mr.tag_name("12.3.45") == "v12.3.45"


@pytest.mark.parametrize("bad", ["0.1", "v0.1.0", "0.1.0rc1", "", "0.1.0 ", "1.2.3.4"])
def test_tag_name_rejects(mr, bad):
    with pytest.raises(ValueError):
        mr.tag_name(bad)


# ---------- check_wheel ----------

GOOD_WHEEL = [
    "gates/__init__.py",
    "gates/hydration/abbreviations.yaml",
    "gates/hydration/prompts/propose_v3.md",
    "synthetic/glossary.yaml",
]


def test_check_wheel_accepts_complete_wheel(mr):
    assert mr.check_wheel(GOOD_WHEEL) == []


@pytest.mark.parametrize("drop", GOOD_WHEEL[1:])
def test_check_wheel_needs_runtime_data(mr, drop):
    assert mr.check_wheel([n for n in GOOD_WHEEL if n != drop])


def test_check_wheel_refuses_top_level_jobs(mr):
    assert mr.check_wheel(GOOD_WHEEL + ["jobs/hydrate.py"])


# ---------- check_identity (real git, local config only) ----------

def _git(cwd, *args, env=None):
    import os
    import subprocess

    subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, **(env or {})})


def test_check_identity(mr, tmp_path):
    who = {"GIT_AUTHOR_NAME": "Rel Ease", "GIT_AUTHOR_EMAIL": "rel@example.com",
           "GIT_COMMITTER_NAME": "Rel Ease", "GIT_COMMITTER_EMAIL": "rel@example.com"}
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "--local", "user.name", "Rel Ease")
    _git(tmp_path, "config", "--local", "user.email", "rel@example.com")
    _git(tmp_path, "config", "--local", "tag.gpgsign", "false")
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "one", env=who)
    _git(tmp_path, "tag", "-a", "v0.0.1", "-m", "t", env=who)
    mr.check_identity(tmp_path, "Rel Ease", "rel@example.com", "v0.0.1")
    with pytest.raises(mr.ReleaseError):
        mr.check_identity(tmp_path, "Rel Ease", "other@example.com")
    # the tagger comes from the committer identity: a tag made as someone else is refused
    _git(tmp_path, "tag", "-a", "v0.0.2", "-m", "t", env={**who, "GIT_COMMITTER_EMAIL": "personal@example.org"})
    with pytest.raises(mr.ReleaseError):
        mr.check_identity(tmp_path, "Rel Ease", "rel@example.com", "v0.0.2")
    other ={**who, "GIT_COMMITTER_EMAIL": "personal@example.org"}
    (tmp_path / "f.txt").write_text("y\n", encoding="utf-8")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-q", "-a", "--no-verify", "-m", "two", env=other)
    with pytest.raises(mr.ReleaseError):
        mr.check_identity(tmp_path, "Rel Ease", "rel@example.com")

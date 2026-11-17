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


# ---------- clear_worktree / start_repo / commit_release (real git, local config only) ----------

REL_NAME, REL_EMAIL = "Rel Ease", "rel@example.com"
STAMP = "2026-11-18T09:00:00+0000"


def _who(name: str = REL_NAME, email: str = REL_EMAIL) -> dict[str, str]:
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email}


def _out(cwd, *args) -> str:
    import subprocess

    return subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))


def _make_base(tmp_path: Path, email: str = REL_EMAIL) -> tuple[Path, str]:
    """A published-main stand-in: one commit on main by the given identity, annotated tag v0.0.1."""
    base = tmp_path / "base"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    for k, v in (("user.name", REL_NAME), ("user.email", email), ("commit.gpgsign", "false"),
                 ("tag.gpgsign", "false"), ("core.autocrlf", "false")):
        _git(base, "config", "--local", k, v)
    _write_files(base, {"README.md": "old readme\n", "gone.txt": "dropped in the next release\n",
                        "src/a.py": "A = 1\n"})
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "--no-verify", "-m", "v0.0.1", env=_who(REL_NAME, email))
    _git(base, "tag", "-a", "v0.0.1", "-m", "v0.0.1", env=_who(REL_NAME, email))
    return base, _out(base, "rev-parse", "HEAD").strip()


def _template(tmp_path: Path) -> Path:
    t = tmp_path / "empty-template"
    t.mkdir()
    return t


def _tree(repo: Path) -> list[str]:
    return sorted(p for p in _out(repo, "ls-tree", "-r", "-z", "--name-only", "HEAD").split("\0") if p)


NEW_FILES = {"README.md": "new readme\n", "src/a.py": "A = 2\n", "src/b/c.py": "C = 3\n", ".gitignore": "build/\n"}


def test_clear_worktree_keeps_only_git(mr, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    git_before = sorted(p.relative_to(repo).as_posix() for p in (repo / ".git").rglob("*"))
    _write_files(repo, {"a.txt": "a\n", ".hidden": "h\n", ".github/workflows/ci.yml": "x\n",
                        "deep/er/still/f.md": "f\n", "deep/.dot/g": "g\n"})
    (repo / "emptydir").mkdir()
    mr.clear_worktree(repo)
    assert [p.name for p in repo.iterdir()] == [".git"]
    assert sorted(p.relative_to(repo).as_posix() for p in (repo / ".git").rglob("*")) == git_before
    mr.clear_worktree(repo)  # idempotent
    assert [p.name for p in repo.iterdir()] == [".git"]


def test_based_release_is_one_commit_on_top_of_published_main(mr, tmp_path):
    base, base_head = _make_base(tmp_path)
    template = _template(tmp_path)
    repo = tmp_path / "work" / "repo"
    assert mr.start_repo(repo, str(base), template) == base_head
    assert _out(repo, "rev-parse", "HEAD").strip() == base_head
    assert _out(repo, "tag", "-l", "v0.0.1").strip() == "v0.0.1"  # tags fetched

    mr.clear_worktree(repo)
    assert [p.name for p in repo.iterdir()] == [".git"]
    _write_files(repo, NEW_FILES)
    written = sorted(NEW_FILES)
    head = mr.commit_release(repo, written, REL_NAME, REL_EMAIL, "v0.1.1", base_head, STAMP, template)

    assert head == _out(repo, "rev-parse", "HEAD").strip()
    assert head != base_head
    assert _out(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [base_head]
    assert _out(repo, "rev-list", "--count", "HEAD").strip() == "2"
    assert _tree(repo) == written
    assert "gone.txt" not in _tree(repo)
    assert _out(repo, "log", "-1", "--format=%an|%ae|%cn|%ce").strip() == (
        f"{REL_NAME}|{REL_EMAIL}|{REL_NAME}|{REL_EMAIL}"
    )
    assert _out(repo, "log", "-1", "--format=%ad", "--date=format:%Y-%m-%dT%H:%M:%S%z").strip() == STAMP
    assert _out(repo, "log", "-1", "--format=%cd", "--date=format:%Y-%m-%dT%H:%M:%S%z").strip() == STAMP
    assert _out(repo, "tag", "-l", "v0.0.1").strip() == "v0.0.1"
    assert _out(repo, "tag", "-l", "v0.1.1").strip() == ""  # commit_release never creates the tag
    assert _out(repo, "config", "--local", "user.name").strip() == REL_NAME
    assert _out(repo, "config", "--local", "user.email").strip() == REL_EMAIL


def test_commit_release_refuses_existing_tag(mr, tmp_path):
    base, base_head = _make_base(tmp_path)
    template = _template(tmp_path)
    repo = tmp_path / "repo"
    mr.start_repo(repo, str(base), template)
    mr.clear_worktree(repo)
    _write_files(repo, NEW_FILES)
    with pytest.raises(mr.ReleaseError):
        mr.commit_release(repo, sorted(NEW_FILES), REL_NAME, REL_EMAIL, "v0.0.1", base_head, STAMP, template)
    assert _out(repo, "rev-parse", "HEAD").strip() == base_head  # nothing committed


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_commit_release_refuses_written_mismatch(mr, tmp_path, change):
    base, base_head = _make_base(tmp_path)
    template = _template(tmp_path)
    repo = tmp_path / "repo"
    mr.start_repo(repo, str(base), template)
    mr.clear_worktree(repo)
    _write_files(repo, NEW_FILES)
    written = sorted(NEW_FILES)
    written = written[1:] if change == "missing" else sorted([*written, "not/staged.txt"])
    with pytest.raises(mr.ReleaseError):
        mr.commit_release(repo, written, REL_NAME, REL_EMAIL, "v0.1.1", base_head, STAMP, template)
    assert _out(repo, "rev-parse", "HEAD").strip() == base_head


def test_commit_release_refuses_base_with_other_identity(mr, tmp_path):
    base, base_head = _make_base(tmp_path, email="someone-else@example.org")
    template = _template(tmp_path)
    repo = tmp_path / "repo"
    mr.start_repo(repo, str(base), template)
    mr.clear_worktree(repo)
    _write_files(repo, NEW_FILES)
    with pytest.raises(mr.ReleaseError):
        mr.commit_release(repo, sorted(NEW_FILES), REL_NAME, REL_EMAIL, "v0.1.1", base_head, STAMP, template)


def test_fresh_release_is_a_root_commit(mr, tmp_path):
    template = _template(tmp_path)
    repo = tmp_path / "work" / "repo"
    assert mr.start_repo(repo, None, template) is None
    assert (repo / ".git").is_dir()
    assert _out(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    _write_files(repo, NEW_FILES)
    head = mr.commit_release(repo, sorted(NEW_FILES), REL_NAME, REL_EMAIL, "v0.1.1", None, STAMP, template)
    assert head == _out(repo, "rev-parse", "HEAD").strip()
    assert _out(repo, "rev-list", "--count", "HEAD").strip() == "1"
    assert _out(repo, "rev-list", "--parents", "-n", "1", "HEAD").split() == [head]
    assert _tree(repo) == sorted(NEW_FILES)
    assert _out(repo, "log", "-1", "--format=%ad", "--date=format:%Y-%m-%dT%H:%M:%S%z").strip() == STAMP
    assert _out(repo, "tag", "-l").strip() == ""


def test_start_repo_nonexistent_base_raises(mr, tmp_path):
    with pytest.raises(mr.ReleaseError):
        mr.start_repo(tmp_path / "repo", str(tmp_path / "no-such-base"), _template(tmp_path))


# ---------- base URL, changelog date, required --base, fast-forward guard ----------

@pytest.mark.parametrize("url", ["https://github.com/org/repo.git", "https://example.com/a/b"])
def test_check_base_url_accepts_https(mr, url):
    mr.check_base_url(url)


@pytest.mark.parametrize("url", ["", "U:/some/repo", "../repo", "file:///tmp/repo", "http://github.com/o/r",
                                 "git@github.com:o/r.git", "https://"])
def test_check_base_url_refuses_local_and_non_https(mr, url):
    with pytest.raises(mr.ReleaseError):
        mr.check_base_url(url)


def test_changelog_date(mr):
    text = "# Changelog\n\n## [0.1.1] - 2026-11-17\n\nx\n\n## [0.1.0] - 2026-11-16\n"
    assert mr.changelog_date(text, "0.1.1") == "2026-11-17"
    assert mr.changelog_date(text, "0.1.0") == "2026-11-16"
    assert mr.changelog_date(text, "0.2.0") is None


def test_main_requires_base(mr, tmp_path):
    with pytest.raises(SystemExit):
        mr.main(["--out", str(tmp_path / "o"), "--author-name", "Rel Ease", "--author-email", "rel@example.com"])


def test_commit_release_refuses_non_fast_forward(mr, tmp_path):
    template = tmp_path / "tpl"
    template.mkdir()
    repo = tmp_path / "r"
    mr.start_repo(repo, None, template)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    first = mr.commit_release(repo, ["a.txt"], "Rel Ease", "rel@example.com", "v0.0.1", None,
                              "2026-11-18T09:00:00+0000", template)
    (repo / "a.txt").write_text("b\n", encoding="utf-8")
    with pytest.raises(mr.ReleaseError):  # claims a base head that is not the commit's parent
        mr.commit_release(repo, ["a.txt"], "Rel Ease", "rel@example.com", "v0.0.2", "0" * 40,
                          "2026-11-18T09:00:00+0000", template)
    assert first

"""Build a public release as a git repo outside this repo, one commit per release. Never pushes.

    python scripts/make_release.py --out <empty dir outside the repo> \
        --author-name "<name>" --author-email "<email>" --base https://github.com/<org>/<repo>.git

v0.1.0 was the fresh-history first release. Every release since builds on the published main (--base, required),
so the release is one new commit and the push is a fast-forward. The CHANGELOG heading for VERSION must carry
the UTC run date.

Steps (stops at the first failure, leaving the output folder for inspection):
 1. This repo's working tree must be clean, so the export is exactly HEAD.
 2. Clone the published main (--base) or git init, clear its tree, copy every tracked, non-excluded file in,
    apply the rewrite rules from the gitignored
    .release-rewrites (`old => new` per line) to text files, then put the public overlay files in place
    (release/CLAUDE.md becomes CLAUDE.md). Fail if any old string is left.
 3. Set the given author as the export repo's LOCAL identity, one commit (its parent must be the base main).
 4. Check every author / committer field is exactly the given identity.
 5. Run release_scan.sh inside the export; it reads the denylists in place (GG_DENYLIST_DIR), never copied.
 6. Extract the commit with git archive (LF, exactly what is committed) and run ruff there; run the unit tests
    in the export repo (some read tracked files through git ls-files).
 7. Build the wheel and sdist from that archive into dist/, check the wheel holds its data files, write SHA256SUMS.
 8. Annotated tag v<VERSION>; check the tagger identity too.

Prints what the reviewer checks before pushing: file count, commit, tag, checksums.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

# Never shipped: internal logs and plans, spikes, the dashboard work, build output, local secrets/denylists.
EXCLUDED_FILES = frozenset({
    "LOG.md",
    "PLAN.md",
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
})
EXCLUDED_DIRS = ("spikes/", "dashboards/", "release/", "build/", "dist/", ".claude/worktrees/")

# Export path -> source path in this repo. Applied after the copy, so the public version replaces the internal one.
OVERLAY: dict[str, str] = {"CLAUDE.md": "release/CLAUDE.md"}

DENYLISTS = (".client-denylist", ".release-denylist")
_VERSION = re.compile(r"\d+\.\d+\.\d+")


def is_excluded(path: str) -> bool:
    return path in EXCLUDED_FILES or path.startswith(EXCLUDED_DIRS)


def parse_rewrites(text: str) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " => " not in line:
            raise ValueError("rewrite rule must be 'old => new'")
        old, new = (part.strip() for part in line.split(" => ", 1))
        if not old or not new:
            raise ValueError("rewrite rule has an empty side")
        if old in new:
            raise ValueError("rewrite rule's new value contains the old value")
        rules.append((old, new))
    return rules


def apply_rewrites(text: str, rules: list[tuple[str, str]]) -> str:
    for old, new in rules:
        text = text.replace(old, new)
    return text


def is_text(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _write(dst: Path, rel: str, data: bytes, rules: list[tuple[str, str]]) -> None:
    # Binaries and non-UTF-8 text can't be rewritten, and the scan's `git grep -I` skips them: refuse to ship any.
    if not is_text(data):
        raise ValueError(f"non-text file can't be checked for release: {rel}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(apply_rewrites(data.decode("utf-8"), rules).encode("utf-8"))


def export_tree(src_root: Path, dst_root: Path, paths: list[str], rules: list[tuple[str, str]]) -> list[str]:
    for src in OVERLAY.values():
        if not (src_root / src).is_file():
            raise FileNotFoundError(f"overlay source missing: {src}")
    written: set[str] = set()
    for rel in paths:
        if is_excluded(rel):
            continue
        _write(dst_root / rel, rel, (src_root / rel).read_bytes(), rules)
        written.add(rel)
    for dst, src in OVERLAY.items():
        _write(dst_root / dst, src, (src_root / src).read_bytes(), rules)
        written.add(dst)
    return sorted(written)


def leftover(dst_root: Path, rules: list[tuple[str, str]]) -> list[str]:
    """Files whose bytes or path still contain any rule's old string (byte search, so binaries count too)."""
    hits = []
    for p in dst_root.rglob("*"):
        rel = p.relative_to(dst_root)
        if not p.is_file() or ".git" in rel.parts:
            continue
        data = p.read_bytes()
        if any(old.encode("utf-8") in data or old in rel.as_posix() for old, _ in rules):
            hits.append(rel.as_posix())
    return sorted(hits)


def checksum_lines(files: list[Path]) -> str:
    return "".join(
        f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n" for f in sorted(files, key=lambda f: f.name)
    )


def tag_name(version: str) -> str:
    if not _VERSION.fullmatch(version):
        raise ValueError("version must be X.Y.Z")
    return f"v{version}"


# ----------------------------------------------------------------------------------------------- side effects


class ReleaseError(Exception):
    pass


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise ReleaseError(f"{' '.join(cmd[:3])} failed (exit {r.returncode}):\n{r.stdout[-3000:]}{r.stderr[-3000:]}")
    return r.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "safe.directory=*", *args], cwd)


def _bash() -> str:
    git_bash = r"C:\Program Files\Git"
    candidates = [os.environ.get("GG_BASH"), rf"{git_bash}\bin\bash.exe", rf"{git_bash}\usr\bin\bash.exe"]
    found = shutil.which("bash")
    if found and "system32" not in found.lower():  # never the WSL launcher: it can't see this Windows setup
        candidates.append(found)
    for c in candidates:
        if c and Path(c).is_file():
            return c
    raise ReleaseError("Git Bash not found (set GG_BASH)")


def check_identity(repo: Path, name: str, email: str, tag: str | None = None) -> None:
    for line in _git(repo, "log", "--all", "--format=%an|%ae|%cn|%ce").splitlines():
        if line != f"{name}|{email}|{name}|{email}":
            raise ReleaseError("a commit's author or committer is not the release identity")
    if tag:
        tagger = _git(repo, "for-each-ref", f"refs/tags/{tag}", "--format=%(taggername)|%(taggeremail)").strip()
        if tagger != f"{name}|<{email}>":
            raise ReleaseError("the tag's tagger is not the release identity")


WHEEL_MUST_HAVE = ("gates/hydration/abbreviations.yaml", "gates/hydration/prompts/propose_v", "synthetic/glossary.yaml")


def check_wheel(names: list[str]) -> list[str]:
    """Problems with a wheel's file list: missing runtime data files, or a generic top-level `jobs` package."""
    problems = [f"missing {m}" for m in WHEEL_MUST_HAVE if not any(n.startswith(m) for n in names)]
    if any(n.startswith("jobs/") for n in names):
        problems.append("ships the top-level jobs package")
    return problems


def _extract(archive: bytes, dst: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
        try:
            tf.extractall(dst, filter="data")
        except TypeError:  # Python < 3.11.4 has no filter; the archive is our own git archive
            tf.extractall(dst)


def _pinned(name: str, email: str, stamp: str, template: Path) -> tuple[list[str], dict[str, str]]:
    """git argv prefix with hooks disabled, and an env with the identity and date pinned."""
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": stamp,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": stamp}
    return ["git", "-c", "safe.directory=*", "-c", f"core.hooksPath={template.as_posix()}"], env


def clear_worktree(repo: Path) -> None:
    """Delete everything in repo except .git, so the export replaces the tree (files dropped upstream go too)."""
    for p in repo.iterdir():
        if p.name == ".git":
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()


def start_repo(repo: Path, base: str | None, template: Path) -> str | None:
    """Fresh repo (base None), or a clone of the published main; returns the base main HEAD sha (or None)."""
    pin = ["git", "-c", "safe.directory=*", "-c", f"core.hooksPath={template.as_posix()}"]
    if base is None:
        repo.mkdir(parents=True, exist_ok=True)
        _run([*pin, "init", "-q", "-b", "main", f"--template={template.as_posix()}"], repo)
        return None
    repo.parent.mkdir(parents=True, exist_ok=True)
    _run([*pin, "clone", "-q", "--branch", "main", f"--template={template.as_posix()}", base, str(repo)], repo.parent)
    return _git(repo, "rev-parse", "HEAD").strip()


def commit_release(repo: Path, written: list[str], name: str, email: str, tag: str, base_head: str | None,
                   stamp: str, template: Path) -> str:
    """One commit of the exported tree (on top of base_head when given). Never creates the tag."""
    for k, v in (("user.name", name), ("user.email", email), ("commit.gpgsign", "false"),
                 ("tag.gpgsign", "false"), ("core.autocrlf", "false")):
        _git(repo, "config", "--local", k, v)
    if _git(repo, "tag", "-l", tag).strip():
        raise ReleaseError(f"{tag} already exists in the published repo: bump VERSION, never move a tag")
    _git(repo, "add", "-A")
    committed = sorted(p for p in _git(repo, "ls-files", "-z").split("\0") if p)
    if committed != sorted(written):  # a .gitignore or global excludesFile must not silently drop a file
        raise ReleaseError(f"git add did not stage exactly the exported files: {sorted(set(written) ^ set(committed))}")
    pin, env = _pinned(name, email, stamp, template)
    _run([*pin, "commit", "-q", "--no-verify", "-m", f"govgates {tag}"], repo, env)
    if base_head is not None:
        parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
        if parents != [base_head]:
            raise ReleaseError("the release commit is not a fast-forward of the published main")
    check_identity(repo, name, email)
    return _git(repo, "rev-parse", "HEAD").strip()


def check_base_url(base: str) -> None:
    """The base must be the published remote: an https URL, never a local path or file:// (internal history)."""
    if not re.fullmatch(r"https://[^\s/]+/\S+", base):
        raise ReleaseError("--base must be the published repo's https:// URL")


_CHANGELOG_HEADING = re.compile(r"^## \[(?P<v>[^\]]+)\] - (?P<d>\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


def changelog_date(text: str, version: str) -> str | None:
    """The date on the CHANGELOG heading for `version`, or None if there is no such heading."""
    for m in _CHANGELOG_HEADING.finditer(text):
        if m.group("v") == version:
            return m.group("d")
    return None


def build_release(src: Path, work: Path, name: str, email: str, base: str | None = None,
                  state: dict | None = None) -> dict[str, str]:
    """Steps 2-8 inside `work`. Raises ReleaseError on any failure; the caller decides what to keep.

    `state["tagged"]` is set once this run has created the tag, so a failure handler deletes only its own tag."""
    state = {} if state is None else state
    version = (src / "VERSION").read_text(encoding="utf-8").strip()
    tag = tag_name(version)
    rules = parse_rewrites((src / ".release-rewrites").read_text(encoding="utf-8"))
    head = _git(src, "rev-parse", "--short", "HEAD").strip()

    # 2-4. repo: the published main (base) or a fresh one; tree replaced by the export; one commit on top
    empty = work / "empty-template"
    empty.mkdir()
    repo = work / "repo"
    base_head = start_repo(repo, base, empty)
    if base is not None:
        # the clone must be the published release line: an earlier v* tag reachable from main
        if not _git(repo, "tag", "--merged", "HEAD", "-l", "v*").strip():
            raise ReleaseError("--base has no earlier v* release tag on main: is it the published repo?")
        if _git(repo, "ls-remote", "--tags", "origin", f"refs/tags/{tag}").strip():
            raise ReleaseError(f"{tag} already exists on the published repo: bump VERSION, never move a tag")
    clear_worktree(repo)
    paths = [p for p in _git(src, "ls-files", "-z").split("\0") if p]
    try:
        written = export_tree(src, repo, paths, rules)
    except ValueError as e:
        raise ReleaseError(str(e)) from e
    left = leftover(repo, rules)
    if left:
        raise ReleaseError(f"rewrite sources still present in {len(left)} file(s): {', '.join(left)}")
    print(f"exported {len(written)} files from {head}" + (f" on top of {base_head[:7]}" if base_head else ""))
    # the actual run time, in UTC so the local timezone never reaches the public metadata
    stamp = f"{dt.datetime.now(dt.UTC):%Y-%m-%dT%H:%M:%S}+0000"
    commit = commit_release(repo, written, name, email, tag, base_head, stamp, empty)
    pin, genv = _pinned(name, email, stamp, empty)

    # 5. scan: denylists read in place from the source repo, never copied into the export
    r = subprocess.run([_bash(), "scripts/release_scan.sh"], cwd=repo, capture_output=True, text=True,
                       env={**os.environ, "GG_RELEASE_EMAIL": email, "GG_DENYLIST_DIR": src.as_posix()})
    print(r.stdout[-4000:])
    if r.returncode != 0:
        raise ReleaseError("release_scan.sh: NOT READY")

    # 6. ruff on exactly what is committed (LF); tests in the export repo (some use git ls-files)
    tree = work / "check"
    tree.mkdir()
    ar = subprocess.run(["git", "-c", "safe.directory=*", "archive", "--format=tar", "HEAD"], cwd=repo,
                        capture_output=True)
    if ar.returncode != 0:
        raise ReleaseError("git archive failed")
    _extract(ar.stdout, tree)
    py = sys.executable
    _run([py, "-m", "ruff", "check", "--no-cache", "src", "tests"], tree)
    env = {**os.environ, "PYTHONPATH": str(repo / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    tests = _run([py, "-m", "pytest", "tests/unit", "-q", "-p", "no:cacheprovider"], repo, env)
    if _git(repo, "status", "--porcelain"):
        raise ReleaseError("the test run changed the export repo")
    print(tests.strip().splitlines()[-1])

    # 7. wheel + sdist from the archive, wheel contents checked, checksums
    dist = work / "dist"
    _run([py, "-m", "build", "--outdir", str(dist)], tree)
    wheels = list(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseError(f"expected one wheel, got {len(wheels)}")
    with zipfile.ZipFile(wheels[0]) as z:
        problems = check_wheel(z.namelist())
    if problems:
        raise ReleaseError("wheel: " + "; ".join(problems))
    sums = checksum_lines(sorted(p for p in dist.iterdir() if p.suffix in (".whl", ".gz")))
    (dist / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")

    # 8. tag (same pinned identity and date), then check the tagger
    _run([*pin, "tag", "-a", tag, "-m", f"govgates {tag}"], repo, genv)
    state["tagged"] = True
    check_identity(repo, name, email, tag)
    return {"commit": commit, "tag": tag, "sums": sums, "head": head, "parent": base_head or ""}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path, help="empty or new folder outside this repo")
    ap.add_argument("--author-name", required=True)
    ap.add_argument("--author-email", required=True)
    # Required: v0.1.0 is published, so a release without a base would be a new root commit needing a force push.
    ap.add_argument("--base", required=True,
                    help="published repo https URL: the release is one commit on top of its main (a fast-forward)")
    a = ap.parse_args(argv)
    check_base_url(a.base)

    src = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip()).resolve()
    out = a.out.resolve()
    if out == src or src in out.parents:
        raise ReleaseError("--out must be outside the repo")
    if out.exists() and any(out.iterdir()):
        raise ReleaseError("--out must be empty or not exist")
    if "@" not in a.author_email or not a.author_name.strip():
        raise ReleaseError("give a real --author-name and --author-email")
    # find_spec on the __main__ modules: a bare "build" would match the gitignored build/ folder as a namespace package
    missing = [m for m in ("build", "ruff", "pytest") if importlib.util.find_spec(f"{m}.__main__") is None]
    if missing:
        raise ReleaseError(f"{sys.executable} lacks {', '.join(missing)}: run with the repo's .venv python")
    if _git(src, "status", "--porcelain"):
        raise ReleaseError("working tree is not clean: commit first, the export must be exactly HEAD")
    if not (src / ".release-rewrites").is_file():
        raise ReleaseError(".release-rewrites missing (gitignored, one 'old => new' per line)")
    for d in DENYLISTS:
        if not (src / d).is_file():
            raise ReleaseError(f"{d} missing: the scan can't run without it")
    version = (src / "VERSION").read_text(encoding="utf-8").strip()
    tag = tag_name(version)
    today = f"{dt.datetime.now(dt.UTC):%Y-%m-%d}"
    dated = changelog_date((src / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    if dated != today:  # the commit and tag carry the UTC run date; the public changelog must agree
        raise ReleaseError(f"CHANGELOG.md heading for {version} is dated {dated}, but the UTC run date is {today}")

    # Everything is built in .incomplete and only renamed to release/ (RELEASE_OK written last) when every step
    # passed. A failed run leaves .incomplete, with this run's tag (never a published one) deleted.
    work = out / ".incomplete"
    work.mkdir(parents=True)
    state: dict = {}
    try:
        r = build_release(src, work, a.author_name, a.author_email, a.base, state)
        final = out / "release"
        os.replace(work, final)
    except BaseException:
        if state.get("tagged") and (work / "repo" / ".git").is_dir():
            subprocess.run(["git", "-c", "safe.directory=*", "tag", "-d", tag], cwd=work / "repo", capture_output=True)
        print(f"NOT RELEASED: partial output kept in {work} for inspection; do not push from it.", file=sys.stderr)
        raise
    summary = (f"commit {r['commit']}\nparent {r['parent']}\nbase   {a.base}\ntag    {r['tag']}\n"
               f"author {a.author_name} <{a.author_email}>\nfrom   {r['head']}\n\n{r['sums']}")
    (final / "RELEASE_OK").write_text(summary, encoding="utf-8", newline="\n")
    print(f"READY TO REVIEW: {final / 'repo'}  (RELEASE_OK written)\n{summary}"
          f"dist: {final / 'dist'}\n"
          f"origin = {a.base}: push main + {r['tag']} as a fast-forward, never --force. Nothing was pushed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReleaseError as e:
        print(f"RELEASE FAILED: {e}", file=sys.stderr)
        sys.exit(1)

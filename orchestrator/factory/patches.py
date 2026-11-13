"""Diff extraction, validation and repo helpers. Model output is untrusted until it passes here."""
from __future__ import annotations

import io
import os
import pathlib
import re
import stat
import subprocess
import tarfile

from factory.breaker import PatchRejected

FORBIDDEN = re.compile(
    r"^(\+\+\+|---) [ab]/(tests/|\.github/|addons/gut/|.*(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|"
    r"uv\.lock|poetry\.lock|requirements[^/]*\.txt|pyproject\.toml|package\.json|Cargo\.toml|Cargo\.lock|"
    r"pom\.xml|build\.gradle(\.kts)?|settings\.gradle(\.kts)?|\.godot-version)$)", re.M)
FENCE = re.compile(r"```(?:diff|patch)?[ \t]*\n(.*?)```", re.S)
SAFE_PATH = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9_.\-/]{1,200}$")


def validate_repo_path(p: str) -> str:
    if not SAFE_PATH.fullmatch(p) or p.startswith(".git/") or "/.git/" in p:
        raise PatchRejected(f"unsafe path from model: {p!r}")
    return p


def extract_diff(raw: str) -> str:
    m = FENCE.search(raw)
    diff = (m.group(1) if m else raw).strip("\n") + "\n"
    if "--- " not in diff or "+++ " not in diff:
        raise PatchRejected("model output contains no unified diff")
    return diff


def validate_diff(diff: str, repo: pathlib.Path) -> str:
    if FORBIDDEN.search(diff):
        raise PatchRejected("patch touches frozen tests, CI config, manifests or lockfiles")
    for line in diff.splitlines():
        if line.startswith(("+++ b/", "--- a/")):
            validate_repo_path(line[6:])
    r = git(repo, "apply", "--check", "--recount", "-", check=False, input=diff.encode())
    if r.returncode != 0:
        raise PatchRejected("git apply --check failed: " + r.stderr.decode(errors="replace")[:2000])
    return diff


def changed_files(diff: str) -> list[str]:
    return sorted({line[6:].strip() for line in diff.splitlines()
                   if line.startswith("+++ b/") and line[6:].strip() != "/dev/null"})


def extract_and_validate(raw: str, repo: pathlib.Path) -> str:
    return validate_diff(extract_diff(raw), repo)


# Every git command the factory runs ignores repo-local hooks and fsmonitor, and reads no system or
# global config. The runner never sees the clone (it gets an archive with no .git), so this is a second
# layer: if a mount mistake ever exposes .git again, planted hooks still do not run here.
GIT_HARDEN = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
              "-c", "core.untrackedCache=false", "-c", "protocol.ext.allow=never")


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", **(extra or {})}


def git(repo: pathlib.Path, *args: str, check: bool = True, env: dict[str, str] | None = None,
        **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *GIT_HARDEN, "-C", str(repo), *args], check=check, capture_output=True,
                          env=git_env(env), **kw)


# ------------------------------------------------------------------ archives for the sealed runner
MAX_SOURCE_FILE = 5 * 1024 * 1024
MAX_SOURCE_TOTAL = 60 * 1024 * 1024


def _add_file(t: tarfile.TarFile, name: str, path: pathlib.Path) -> int:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SOURCE_FILE:
        return 0                                    # symlinks, devices and oversized files stay behind
    info = tarfile.TarInfo(name)
    info.size, info.mode = st.st_size, (0o755 if st.st_mode & 0o111 else 0o644)
    with path.open("rb") as fh:
        t.addfile(info, fh)
    return st.st_size


def source_archive(repo: pathlib.Path) -> bytes:
    """The patched working tree as a tar: tracked and new files only, no .git, no tests/ (the frozen
    tests go in separately), regular files only. This is all the runner ever sees of the repo."""
    names = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.decode().split("\0")
    out, total = io.BytesIO(), 0
    with tarfile.open(fileobj=out, mode="w") as t:
        for n in sorted(set(filter(None, names))):
            if n == ".git" or n.startswith((".git/", "tests/", ".factory/")) or "/.git/" in f"/{n}":
                continue
            p = repo / n
            if not p.exists() and not p.is_symlink():
                continue                            # deleted by the patch
            total += _add_file(t, n, p)
            if total > MAX_SOURCE_TOTAL:
                raise PatchRejected("repository too large for the sandbox (over 60 MB of source)")
    return out.getvalue()


def dir_archive(root: pathlib.Path) -> bytes:
    """A folder (the frozen tests) as a tar of its regular files."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as t:
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                _add_file(t, p.relative_to(root).as_posix(), p)
    return out.getvalue()


def reset_to_base(repo: pathlib.Path, base_sha: str) -> None:
    git(repo, "reset", "--hard", base_sha)
    git(repo, "clean", "-fdq")


def apply_patch(repo: pathlib.Path, diff: str) -> None:
    git(repo, "apply", "--recount", "--whitespace=nowarn", "-", input=diff.encode())


def read_files(repo: pathlib.Path, paths: list[str], limit: int = 60_000) -> dict[str, str]:
    out, total = {}, 0
    for p in paths:
        f = repo / validate_repo_path(p)
        text = f.read_text(errors="replace") if f.is_file() else "(new file)"
        total += len(text)
        if total > limit:
            out[p] = "(omitted: context budget)"
            continue
        out[p] = text
    return out


def repo_map(repo: pathlib.Path, max_files: int = 400) -> str:
    files = git(repo, "ls-files").stdout.decode().splitlines()
    head = "\n".join(files[:max_files])
    more = f"\n... {len(files) - max_files} more files" if len(files) > max_files else ""
    readme = ""
    for name in ("README.md", "README.rst", "README"):
        if (repo / name).is_file():
            readme = (repo / name).read_text(errors="replace")[:3000]
            break
    return f"<files>\n{head}{more}\n</files>\n<readme>\n{readme}\n</readme>"

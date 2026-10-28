"""Diff extraction, validation and repo helpers. Model output is untrusted until it passes here."""
from __future__ import annotations

import pathlib
import re
import subprocess

from factory.breaker import PatchRejected

FORBIDDEN = re.compile(
    r"^(\+\+\+|---) [ab]/(tests/|\.github/|.*(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|uv\.lock|"
    r"poetry\.lock|requirements[^/]*\.txt|pyproject\.toml|package\.json)$)", re.M)
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
    r = subprocess.run(["git", "-C", str(repo), "apply", "--check", "--recount", "-"],
                       input=diff.encode(), capture_output=True)
    if r.returncode != 0:
        raise PatchRejected("git apply --check failed: " + r.stderr.decode(errors="replace")[:2000])
    return diff


def changed_files(diff: str) -> list[str]:
    return sorted({line[6:].strip() for line in diff.splitlines()
                   if line.startswith("+++ b/") and line[6:].strip() != "/dev/null"})


def extract_and_validate(raw: str, repo: pathlib.Path) -> str:
    return validate_diff(extract_diff(raw), repo)


def git(repo: pathlib.Path, *args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, **kw)


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

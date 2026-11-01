"""Which dependency files a runner image is built from, and their hash. Standard library only.

The hash names the runner image (factory-runner-<project>:<hash>) and lets the factory notice, at the
start of every run, that a repo's dependencies changed since its image was built.
"""
from __future__ import annotations

import hashlib
import pathlib
import re

NODE_FILES = ("package.json", "package-lock.json")
PY_FILE = re.compile(r"^requirements[^/]*\.txt$")


def deps_files(repo: pathlib.Path, language: str) -> list[str]:
    """Top-level dependency files the runner image installs, sorted. Symlinks are ignored."""
    if language in ("javascript", "typescript"):
        names = [n for n in NODE_FILES if (repo / n).is_file() and not (repo / n).is_symlink()]
    else:
        names = [p.name for p in repo.iterdir() if PY_FILE.match(p.name) and p.is_file() and not p.is_symlink()]
    return sorted(names)


def deps_hash(repo: pathlib.Path, language: str) -> str:
    """sha256 over the language and each dependency file's name and bytes. 64 hex characters."""
    h = hashlib.sha256(f"lang:{'node' if language != 'python' else 'python'}\0".encode())
    for name in deps_files(repo, language):
        h.update(name.encode() + b"\0" + (repo / name).read_bytes() + b"\0")
    return h.hexdigest()

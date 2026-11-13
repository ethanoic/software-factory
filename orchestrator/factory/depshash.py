"""Which dependency files a runner image is built from, and their hash. Standard library only.

The hash names the runner image (factory-runner-<project>:<hash>) and lets the factory notice, at the
start of every run, that a repo's dependencies changed since its image was built.
"""
from __future__ import annotations

import hashlib
import pathlib
import re

# Every language a project can be registered with, and the runner family that tests it. `node` is the
# name for JavaScript and TypeScript projects; `javascript` and `typescript` stay valid for older records.
LANGUAGES = ("python", "node", "javascript", "typescript", "rust", "java", "godot")
FAMILY = {"python": "python", "node": "node", "javascript": "node", "typescript": "node",
          "rust": "rust", "java": "java", "godot": "godot"}

NODE_FILES = ("package.json", "package-lock.json")
RUST_FILES = ("Cargo.toml", "Cargo.lock")
JAVA_FILES = ("pom.xml",)
GODOT_FILES = (".godot-version",)               # optional: pins the Godot release the runner downloads
PY_FILE = re.compile(r"^requirements[^/]*\.txt$")


def family(language: str) -> str:
    """The runner family for a language. Unknown languages raise, so a typo never picks a runner."""
    try:
        return FAMILY[language]
    except KeyError:
        raise ValueError(f"unknown language {language!r}; use one of {', '.join(LANGUAGES)}") from None


def deps_files(repo: pathlib.Path, language: str) -> list[str]:
    """Top-level dependency files the runner image installs, sorted. Symlinks are ignored."""
    fam = family(language)
    if fam == "python":
        names = [p.name for p in repo.iterdir() if PY_FILE.match(p.name) and p.is_file() and not p.is_symlink()]
    else:
        fixed = {"node": NODE_FILES, "rust": RUST_FILES, "java": JAVA_FILES, "godot": GODOT_FILES}[fam]
        names = [n for n in fixed if (repo / n).is_file() and not (repo / n).is_symlink()]
    return sorted(names)


def deps_hash(repo: pathlib.Path, language: str) -> str:
    """sha256 over the runner family and each dependency file's name and bytes. 64 hex characters.
    javascript, typescript and node share a family, so renaming a project's language keeps its image."""
    h = hashlib.sha256(f"lang:{family(language)}\0".encode())
    for name in deps_files(repo, language):
        h.update(name.encode() + b"\0" + (repo / name).read_bytes() + b"\0")
    return h.hexdigest()

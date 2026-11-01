"""Archive checks for the sealed runner. Standard library only, so the factory's tests always run them.

Incoming archives (source, frozen tests) are rebuilt from scratch with only directories and regular
files. Outgoing results are reduced to known file names, regular files, under a size limit.
"""
from __future__ import annotations

import io
import posixpath
import re
import tarfile

MAX_ARCHIVE = 64 * 1024 * 1024            # each incoming archive, decoded
MAX_MEMBERS = 20_000
RESULT_FILES = {"lint.json", "qa.json", "junit.xml", "jest.json", "test.log", "result.json"}
MAX_RESULT_FILE = 1024 * 1024             # per result file; larger ones are dropped
MAX_RESULTS = 8 * 1024 * 1024             # the whole results archive as streamed from Docker
CHANGED_NAME = re.compile(r"^[A-Za-z0-9_.\-/]{1,200}$")


class Rejected(ValueError):
    pass


def safe_member_name(name: str) -> str:
    n = name
    while n.startswith("./"):
        n = n[2:]
    n = posixpath.normpath(n or ".")
    if name.startswith("/") or n == "." or n.startswith("..") or "/../" in f"/{n}/" or "\x00" in n:
        raise Rejected(f"unsafe path in archive: {name!r}")
    if n == ".git" or n.startswith(".git/") or "/.git/" in f"/{n}/":
        raise Rejected(f"archive contains git metadata: {name!r}")
    return n


def rebuild_archive(raw: bytes, *, skip_prefixes: tuple[str, ...] = ()) -> bytes:
    """Re-pack an incoming archive with only directories and regular files, owned by root, mode 644/755.
    Anything else (symlinks, hard links, devices, FIFOs, .git, absolute or parent paths) is refused."""
    if len(raw) > MAX_ARCHIVE:
        raise Rejected("archive too large")
    out = io.BytesIO()
    try:
        src = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
    except tarfile.TarError as e:
        raise Rejected(f"not a tar archive: {e}") from e
    with src, tarfile.open(fileobj=out, mode="w") as dst:
        for i, m in enumerate(src):
            if i >= MAX_MEMBERS:
                raise Rejected("archive has too many entries")
            name = safe_member_name(m.name)
            if name.startswith(skip_prefixes):
                continue
            if m.isdir():
                info = tarfile.TarInfo(name)
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                dst.addfile(info)
            elif m.isreg():
                info = tarfile.TarInfo(name)
                info.size, info.mode = m.size, (0o755 if m.mode & 0o111 else 0o644)
                dst.addfile(info, src.extractfile(m))
            else:
                raise Rejected(f"archive entry is not a regular file or directory: {m.name!r}")
    return out.getvalue()


def changed_files_archive(paths: list[str]) -> bytes:
    for p in paths:
        if not CHANGED_NAME.fullmatch(p) or ".." in p or p.startswith("/"):
            raise Rejected(f"unsafe changed file path: {p!r}")
    data = "\n".join(paths).encode()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as t:
        info = tarfile.TarInfo("changed_files.txt")
        info.size, info.mode = len(data), 0o644
        t.addfile(info, io.BytesIO(data))
    return out.getvalue()


def read_results(chunks) -> dict[str, str]:
    """Results archive streamed from Docker -> {name: text}. Only known names, only regular files,
    only directly inside the results folder, each under MAX_RESULT_FILE. Everything else is dropped."""
    buf, total = io.BytesIO(), 0
    for chunk in chunks:
        total += len(chunk)
        if total > MAX_RESULTS:
            break                                   # truncated archive: read what parses, drop the rest
        buf.write(chunk)
    buf.seek(0)
    files: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=buf, mode="r|") as t:
            for m in t:
                parts = m.name.split("/")
                if len(parts) != 2 or parts[1] not in RESULT_FILES:
                    continue
                if not m.isreg() or m.size > MAX_RESULT_FILE:
                    continue
                f = t.extractfile(m)
                if f is not None:
                    files[parts[1]] = f.read().decode("utf-8", errors="replace")
    except tarfile.TarError:
        pass
    return files

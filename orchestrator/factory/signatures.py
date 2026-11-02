"""Public signatures of source files, without their bodies: what the test writer is allowed to see.

The test writer works from the ticket and these signatures only, so its tests describe the behaviour the
ticket asks for, not whatever the current code happens to do.
"""
from __future__ import annotations

import ast
import pathlib
import re

MAX_CHARS = 12_000


def python_signatures(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "(could not parse)"
    out: list[str] = []

    def doc(node) -> str:
        d = ast.get_docstring(node)
        return f'  """{d.strip().splitlines()[0][:160]}"""' if d else ""

    def func(node, indent=""):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        decos = "".join(f"{indent}@{ast.unparse(d)}\n" for d in node.decorator_list)
        line = f"{decos}{indent}{prefix} {node.name}({ast.unparse(node.args)}){ret}: ..."
        d = doc(node)
        return line + (f"\n{indent}{d}" if d else "")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            out.append(func(node))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            bases = f"({', '.join(ast.unparse(b) for b in node.bases)})" if node.bases else ""
            out.append(f"class {node.name}{bases}:" + (f"\n{doc(node)}" if doc(node) else ""))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                        (not item.name.startswith("_") or item.name == "__init__"):
                    out.append(func(item, "    "))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [ast.unparse(t) for t in targets]
            if any(n.isupper() or n == "__all__" for n in names):
                out.append(ast.unparse(node)[:200])          # public constants and __all__
    return "\n".join(out) or "(no public functions or classes)"


JS_PATTERNS = [
    re.compile(r"^\s*(export\s+(default\s+)?)?(async\s+)?function\s*\*?\s*[A-Za-z_$][\w$]*\s*(<[^>]*>)?\s*\([^)]*\)[^{]*"),
    re.compile(r"^\s*(export\s+)?(const|let|var)\s+[A-Za-z_$][\w$]*\s*(:[^=]+)?=\s*(async\s+)?(\([^)]*\)|[A-Za-z_$][\w$]*)\s*(:[^=]+)?=>"),
    re.compile(r"^\s*(export\s+(default\s+)?)?(abstract\s+)?class\s+[A-Za-z_$][\w$]*[^{]*"),
    re.compile(r"^\s*(export\s+)?(interface|type|enum)\s+[A-Za-z_$][\w$]*[^{=]*"),
    re.compile(r"^\s+(static\s+|async\s+|public\s+|get\s+|set\s+)*[A-Za-z_$][\w$]*\s*\([^)]*\)\s*(:[^{]+)?\{"),
    re.compile(r"^\s*(module\.exports|exports\.[A-Za-z_$][\w$]*)\s*="),
    re.compile(r"^\s*export\s*\{[^}]*\}"),
]
JS_SKIP = re.compile(r"^\s*(if|for|while|switch|catch|return|else)\b")


def js_signatures(source: str) -> str:
    out = []
    for line in source.splitlines():
        if JS_SKIP.match(line) or line.strip().startswith(("//", "*", "/*")):
            continue
        for pat in JS_PATTERNS:
            m = pat.match(line)
            if m:
                sig = m.group(0).rstrip().rstrip("{").rstrip()
                if pat.pattern.startswith(r"^\s*(module"):
                    sig = line.strip()[:200]               # the export list itself is the interface
                out.append(sig + (" { ... }" if not sig.endswith((";", "}", "=")) else ""))
                break
    return "\n".join(out) or "(no functions, classes or exports found)"


def signatures(repo: pathlib.Path, paths: list[str], language: str) -> dict[str, str]:
    """{path: signatures} for the target files. A file that does not exist yet is marked as new."""
    out, total = {}, 0
    for p in paths:
        f = repo / p
        if not f.is_file() or f.is_symlink():
            out[p] = "(new file: does not exist yet)"
            continue
        text = f.read_text(errors="replace")
        sig = python_signatures(text) if p.endswith(".py") else js_signatures(text)
        total += len(sig)
        out[p] = sig if total <= MAX_CHARS else "(omitted: context budget)"
    return out


def test_style(repo: pathlib.Path, language: str) -> str:
    """How existing tests import the code (first lines of one existing test), so new tests match.
    Test code only: it shows module style, not the implementation."""
    tests = repo / "tests"
    if not tests.is_dir():
        return "(no existing tests)"
    pattern = "test_*.py" if language == "python" else "*.test.*"
    for f in sorted(tests.rglob(pattern)):
        if f.is_file() and not f.is_symlink():
            head = "\n".join(f.read_text(errors="replace").splitlines()[:15])
            return f"{f.relative_to(repo).as_posix()}:\n{head}"
    return "(no existing tests)"

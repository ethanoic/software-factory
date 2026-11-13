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


def _line_signatures(source: str, patterns: list[re.Pattern], skip: re.Pattern, comment: tuple[str, ...]) -> str:
    """Declarations only, one per line, bodies cut at the first `{` or `:`. Shared by the Rust, Java and
    GDScript readers, which, like the JavaScript one, read line by line rather than parse."""
    out = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(comment) or skip.match(line):
            continue
        for pat in patterns:
            m = pat.match(line)
            if m:
                sig = m.group(0).rstrip().rstrip("{").rstrip()
                out.append(sig[:240])
                break
    return "\n".join(out) or "(no public declarations found)"


RUST_PATTERNS = [
    re.compile(r"^\s*pub(\([^)]*\))?\s+(const\s+|async\s+|unsafe\s+|extern\s+\"[^\"]*\"\s+)*fn\s+\w+[^{;]*"),
    re.compile(r"^\s*pub(\([^)]*\))?\s+(struct|enum|trait|type|mod|const|static|use)\s+[^{;]*;?"),
    re.compile(r"^\s*impl(<[^>]*>)?\s+[^{]*"),
    re.compile(r"^\s*#\[derive\([^)]*\)\]"),
]
RUST_SKIP = re.compile(r"^\s*(let|if|for|while|match|return|loop)\b")

JAVA_PATTERNS = [
    re.compile(r"^\s*package\s+[\w.]+;"),
    re.compile(r"^\s*(public|protected)\s+([\w<>\[\],?\s]+\s+)?(class|interface|enum|record|@interface)\s+\w+[^{]*"),
    re.compile(r"^\s*(public|protected)\s+(static\s+|final\s+|abstract\s+|synchronized\s+|default\s+|<[^>]+>\s+)*"
               r"[\w<>\[\],.?\s]+\s+\w+\s*\([^)]*\)[^{;]*"),
    re.compile(r"^\s*(public|protected)\s+\w+\s*\([^)]*\)[^{;]*"),                       # constructors
    re.compile(r"^\s*(public|protected)\s+static\s+final\s+[^=;]+"),                    # constants, no values
]
JAVA_SKIP = re.compile(r"^\s*(if|for|while|switch|catch|return|else|new|throw)\b")

GD_PATTERNS = [
    re.compile(r"^(class_name|extends)\s+\S+.*"),
    re.compile(r"^(static\s+)?func\s+[A-Za-z]\w*\s*\([^)]*\)(\s*->\s*[\w.\[\]]+)?"),   # public: no leading _
    re.compile(r"^signal\s+\w+.*"),
    re.compile(r"^(@export[\w(),\s\"]*\s+)?(const|var|enum)\s+[A-Za-z]\w*[^=:]*(:\s*[\w.\[\]]+)?"),
    re.compile(r"^class\s+\w+.*"),
]
GD_SKIP = re.compile(r"^\s")                    # indented lines are bodies; top-level only


def rust_signatures(source: str) -> str:
    return _line_signatures(source, RUST_PATTERNS, RUST_SKIP, ("//", "/*", "*"))


def java_signatures(source: str) -> str:
    return _line_signatures(source, JAVA_PATTERNS, JAVA_SKIP, ("//", "/*", "*"))


def gdscript_signatures(source: str) -> str:
    return _line_signatures(source, GD_PATTERNS, GD_SKIP, ("#",))


READERS = {".py": python_signatures, ".rs": rust_signatures, ".java": java_signatures, ".gd": gdscript_signatures}


def signatures(repo: pathlib.Path, paths: list[str], language: str) -> dict[str, str]:
    """{path: signatures} for the target files. A file that does not exist yet is marked as new."""
    out, total = {}, 0
    for p in paths:
        f = repo / p
        if not f.is_file() or f.is_symlink():
            out[p] = "(new file: does not exist yet)"
            continue
        text = f.read_text(errors="replace")
        sig = READERS.get(pathlib.PurePosixPath(p).suffix, js_signatures)(text)
        total += len(sig)
        out[p] = sig if total <= MAX_CHARS else "(omitted: context budget)"
    return out


def test_style(repo: pathlib.Path, language: str) -> str:
    """How existing tests import the code (first lines of one existing test), so new tests match.
    Test code only: it shows module style, not the implementation."""
    from factory.depshash import family
    fam = family(language)
    pattern = {"python": "test_*.py", "node": "*.test.*", "rust": "*.rs", "java": "*Test.java",
               "godot": "test_*.gd"}[fam]
    # Java projects keep their tests under src/test/java; the factory's new tests still go to tests/.
    roots = [repo / "tests"] + ([repo / "src" / "test" / "java"] if fam == "java" else [])
    for tests in roots:
        if not tests.is_dir() or tests.is_symlink():
            continue
        for f in sorted(tests.rglob(pattern)):
            if f.is_file() and not f.is_symlink():
                head = "\n".join(f.read_text(errors="replace").splitlines()[:15])
                return f"{f.relative_to(repo).as_posix()}:\n{head}"
    return "(no existing tests)"

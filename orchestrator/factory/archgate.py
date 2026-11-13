"""Gate 3: changes that alter the architecture need a person who has thought about it. Standard library only.

A deterministic check, not a model, reads the paths the final patch touches. If any falls in one of the
classes below, the run cannot be pushed with the ordinary approval: a person must add
`factory:arch-approve` (or run `make arch-ok`) after reading why. Everything else passes untouched.

Manifests, lockfiles, CI workflows and frozen tests are not here: `patches.FORBIDDEN` refuses those
outright, and the reviewer blocks build and tool configuration. This gate is the deterministic backstop
for what the reviewer might miss, and for what is allowed but still changes how the system is built,
deployed or trusted.
"""
from __future__ import annotations

import re

RULES: list[tuple[str, re.Pattern]] = [
    ("container image or compose file",
     re.compile(r"(^|/)(Dockerfile|Containerfile)(\.[^/]*)?$|(^|/)(docker-)?compose[^/]*\.ya?ml$|(^|/)\.dockerignore$")),
    ("infrastructure as code",
     re.compile(r"\.(tf|tfvars|bicep|hcl)$|(^|/)(k8s|kubernetes|helm|charts|terraform|infra|deploy|deployment)/"
                r"|(^|/)(cloudformation|serverless)[^/]*\.(ya?ml|json)$", re.I)),
    ("test harness configuration (can change what the frozen tests prove)",
     re.compile(r"(^|/)(conftest\.py|pytest\.ini|tox\.ini|jest\.config\.[cm]?[jt]s|jest\.setup\.[cm]?[jt]s"
                r"|babel\.config\.[cm]?js|\.babelrc|\.gutconfig\.json|nextest\.toml)$|(^|/)\.config/nextest\.toml$")),
    ("code that runs at build or install time",
     re.compile(r"(^|/)build\.rs$|(^|/)\.cargo/config(\.toml)?$|(^|/)\.mvn/|(^|/)settings\.xml$|(^|/)Makefile$"
                r"|(^|/)(setup\.py|setup\.cfg)$|\.(sh|bash|ps1|bat|cmd)$")),
    ("environment or registry configuration",
     re.compile(r"(^|/)\.env(\.[^/]*)?$|(^|/)\.(npmrc|yarnrc(\.yml)?|pypirc|pip\.conf|netrc)$")),
    ("ownership, permissions or platform entitlements",
     re.compile(r"(^|/)CODEOWNERS$|\.entitlements$|(^|/)AndroidManifest\.xml$|(^|/)Info\.plist$"
                r"|(^|/)\.gitattributes$|(^|/)\.gitmodules$")),
    ("engine or export configuration",
     re.compile(r"(^|/)project\.godot$|(^|/)export_presets\.cfg$|(^|/)override\.cfg$|(^|/)addons/[^/]+/plugin\.cfg$")),
]

ADDED = re.compile(r"^--- /dev/null\n\+\+\+ b/(\S+)", re.M)


def touched(diff: str) -> list[str]:
    """Every path a unified diff touches, old and new names, in order, without duplicates."""
    out = []
    for m in re.finditer(r"^(?:\+\+\+|---) [ab]/(\S+)", diff or "", re.M):
        out.append(m.group(1))
    return list(dict.fromkeys(out))


def triggers(diff: str, top_level: set[str]) -> list[str]:
    """Why a person must approve this change's architecture, one line per reason; empty when nothing
    triggers. `top_level`: the names at the repo root before the change."""
    reasons = []
    for path in touched(diff):
        for why, rule in RULES:
            if rule.search(path):
                reasons.append(f"{path}: {why}")
                break
    for path in ADDED.findall(diff or ""):
        head = path.split("/", 1)[0]
        if "/" in path and head not in top_level:
            reasons.append(f"{head}/: new top-level folder (a new module, package or service)")
    return list(dict.fromkeys(reasons))


CHECKLIST = ("Before approving, check: a threat note for what the change lets the code do that it could not "
             "before; no new network path, service, data flow or permission you did not ask for; the factory's "
             "principles still hold (runner without network, nothing pushed without you); any new egress or "
             "dependency is one you would add yourself.")

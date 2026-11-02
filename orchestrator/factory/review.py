"""Independent code review: categories, what blocks, and reading the reviewer's answer.

The reviewer only reports findings by category. Whether a finding blocks the run is decided here, from
pool.yaml (`review`), never by the model. A blocked run goes back to repair; the reviewer can never
approve anything, only the human can.
"""
from __future__ import annotations

import json
import re

from factory.config import policy
from factory.state import Finding, Review

CATEGORIES = {
    "scope": "changes the ticket did not ask for (other features, files or behaviour)",
    "secrets": "reads credentials, tokens, environment variables or files outside the repository",
    "network": "new network calls, sockets, downloads, or running shell commands or subprocesses",
    "disabled_checks": "removes or weakens tests, validation, error handling, auth, lint or security checks",
    "test_or_tool_config": "changes test, lint, build, package or CI configuration, or test helpers",
    "obfuscation": "encoded or generated code, eval, or behaviour that differs when tests run",
    "unmet_criterion": "an acceptance criterion the change does not actually implement",
    "correctness": "a bug the tests do not catch",
    "quality": "readability, naming, duplication",
}
DEFAULT_BLOCK = ("scope", "secrets", "network", "disabled_checks", "test_or_tool_config", "obfuscation")


def settings() -> dict:
    cfg = policy().get("review") or {}
    return {"mode": cfg.get("mode", "block"),
            "block_categories": set(cfg.get("block_categories", DEFAULT_BLOCK)),
            "independence": cfg.get("independence", "prefer")}


def parse(raw: str, *, model: str, independent: bool) -> Review:
    """Reviewer output -> Review. Unknown categories become 'quality'. Unreadable output gives a review
    marked not completed: the run still reaches you, flagged, rather than passing silently."""
    s = settings()
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else None
        items = data.get("findings", []) if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError
    except (ValueError, AttributeError):
        return Review(model=model, independent=independent, completed=False)
    findings = []
    for f in items[:30]:
        if not isinstance(f, dict):
            continue
        cat = str(f.get("category", "")).strip().lower()
        cat = cat if cat in CATEGORIES else "quality"
        findings.append(Finding(category=cat, file=str(f.get("file", ""))[:200], detail=str(f.get("detail", ""))[:600],
                                blocking=s["mode"] == "block" and cat in s["block_categories"]))
    return Review(model=model, independent=independent, findings=findings)


def summary(r: Review | None) -> str:
    """One paragraph for the approval comment, the PR body and `make diff`."""
    if r is None:
        return "Independent review: not run."
    who = "a different model from the code author" if r.independent else \
        "the SAME model that wrote the code (no other model is qualified)"
    if not r.completed:
        return f"Independent review by {r.model} ({who}): DID NOT COMPLETE (unreadable answer). Review the diff yourself."
    if not r.findings:
        return f"Independent review by {r.model} ({who}): no findings."
    lines = [f"Independent review by {r.model} ({who}): {len(r.findings)} finding(s)."]
    for f in r.findings:
        lines.append(f"- [{'BLOCKING' if f.blocking else 'advisory'}] {f.category}"
                     f"{' in ' + f.file if f.file else ''}: {f.detail}")
    return "\n".join(lines)

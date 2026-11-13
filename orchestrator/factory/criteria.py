"""Human-owned acceptance criteria (Gate 1). Standard library only.

A person writes the criteria in the issue, under an "Acceptance criteria" heading, one bullet each:

    ## Acceptance criteria
    - Given an empty name, when greet() is called, then it returns "Hello, World!"
    - Given "Ada", when greet() is called, then it returns "Hello, Ada!"
    - feel: the greeting reads as friendly, not formal

Bullets become tests. Bullets that start with `feel:` (or `manual:`) cannot be tested by a machine: they
are never sent to the test writer, and are listed in the PR as checks for a person. Indented lines under
a bullet continue it. The section ends at the next heading.

When an issue has no such section, the planner proposes criteria and the run waits until a person
approves them (the `factory:criteria-ok` label, or `make criteria-ok`). No model ever decides what
"correct" means for a run on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING = re.compile(r"^\s{0,3}#{1,6}\s*acceptance\s+criteria\s*:?\s*#*\s*$", re.I)
ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
BULLET = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?(.*)$")
CONTINUATION = re.compile(r"^\s{2,}(\S.*)$")
MANUAL = re.compile(r"^(?:feel|manual)\s*:\s*", re.I)
MAX_ITEMS, MAX_CHARS = 20, 400


@dataclass(frozen=True)
class Criteria:
    testable: list[str] = field(default_factory=list)      # each becomes at least one test
    manual: list[str] = field(default_factory=list)        # a person checks these on the PR

    def __bool__(self) -> bool:
        return bool(self.testable or self.manual)


def parse(body: str) -> Criteria:
    """The criteria in an issue body, or an empty Criteria when there is no section with bullets."""
    items: list[str] = []
    inside = False
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if HEADING.match(line):
            inside, items = True, []                        # the last such section wins
            continue
        if not inside:
            continue
        if ANY_HEADING.match(line):
            inside = False
            continue
        m = BULLET.match(line)
        if m:
            items.append(m.group(1).strip())
            continue
        c = CONTINUATION.match(line)
        if c and items:
            items[-1] = f"{items[-1]} {c.group(1).strip()}"
    testable, manual = [], []
    for raw in items:
        text = " ".join(raw.split())[:MAX_CHARS]
        if not text:
            continue
        if MANUAL.match(text):
            rest = MANUAL.sub("", text, count=1).strip()
            if rest:
                manual.append(rest)
        else:
            testable.append(text)
    return Criteria(testable=_dedupe(testable)[:MAX_ITEMS], manual=_dedupe(manual)[:MAX_ITEMS])


def _dedupe(xs: list[str]) -> list[str]:
    return list(dict.fromkeys(xs))


def as_markdown(testable: list[str], manual: list[str] | None = None) -> str:
    """The section a person can paste into the issue, edit, and approve."""
    lines = ["## Acceptance criteria", *[f"- {c}" for c in testable], *[f"- feel: {c}" for c in manual or []]]
    return "\n".join(lines)

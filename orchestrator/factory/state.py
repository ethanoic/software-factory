"""Typed state carried through the FSM (spec §1.2)."""
from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


class Tier(StrEnum):
    T1_DETERMINISTIC = "t1"
    T2_LOCAL = "t2"
    T3_REMOTE = "t3"


class TicketSpec(BaseModel):
    ticket_id: str                      # e.g. "JIRA-1423"
    title: str
    body: str                           # UNTRUSTED. Never interpolated into shell commands.
    repo: str                           # "owner/repo"
    base_branch: str = "main"
    language: Literal["python", "node", "javascript", "typescript", "rust", "java", "godot"]   # see depshash.FAMILY
    issue_number: int | None = None     # set when the ticket came from GitHub intake (spec §5.6)
    criteria: list[str] = Field(default_factory=list)       # written by a person in the issue (criteria.py)
    manual_checks: list[str] = Field(default_factory=list)  # `feel:` items: a person checks them on the PR
    rework: str = Field(default="", max_length=4_000)       # a person's reason for sending a change back


class History(BaseModel):
    """What happened on this ticket's previous run, so a retry or a rework does not start blind."""
    previous_run: str
    status: str                                             # halted | pr_open | awaiting_approval | ...
    reason: str = Field(default="", max_length=1_000)       # why it halted, when it did
    attempts: list[dict] = Field(default_factory=list)      # its last repair attempts (failure class, diff stat)
    previous_diff: str = Field(default="", max_length=12_000)   # the change it made, for a rework


class Plan(BaseModel):
    summary: str
    target_files: list[str]             # relative to the repo root, validated by validate_repo_path()
    acceptance_criteria: list[str]
    test_files: list[str] = Field(default_factory=list)   # paths under tests/, written by WRITE_TESTS
    test_sources: dict[str, str] = Field(default_factory=dict)
    criteria_tests: dict[str, list[str]] = Field(default_factory=dict)   # criterion -> test names (WRITE_TESTS)
    complexity: Literal["trivial", "routine", "complex"]


class Patch(BaseModel):
    unified_diff: str
    produced_by: Tier
    model: str


class FailureClass(StrEnum):
    SYNTAX = "syntax"            # parse/lint error
    ASSERTION = "assertion"      # logic wrong -> escalate
    ENVIRONMENT = "environment"  # missing dep, import path -> halt, not a model problem
    TIMEOUT = "timeout"          # unbounded loop / slow -> escalate once, then halt
    TAMPER = "tamper"            # frozen tests modified -> halt immediately
    NONE = "none"


class SandboxResult(BaseModel):
    exit_code: int
    passed: int
    failed: int
    lint_errors: int
    failure_class: FailureClass
    stack_excerpt: str = Field(default="", max_length=8_000)
    duration_s: float = 0.0
    tests_hash_ok: bool = True


class RedCheck(BaseModel):
    """The new tests run against the unchanged code (no model involved). At least one must fail."""
    new_tests: int
    new_failed: int
    detail: str = Field(default="", max_length=2_000)


class Finding(BaseModel):
    category: str                       # see review.BLOCK_CATEGORIES and ADVISE_CATEGORIES
    file: str = ""
    detail: str = Field(default="", max_length=600)
    blocking: bool = False              # decided by the factory from the category, never by the model


class Review(BaseModel):
    """Independent code review. The reviewer can block a run; it can never approve one."""
    model: str
    independent: bool                   # False when no model other than the code author was qualified
    completed: bool = True              # False when the reviewer's answer could not be read
    findings: list[Finding] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.blocking for f in self.findings)


class QAResult(BaseModel):
    high_findings: int
    medium_findings: int
    report_excerpt: str = Field(default="", max_length=4_000)


class FactoryState(TypedDict, total=False):
    run_id: str
    ticket: TicketSpec
    base_sha: str
    accepted: bool
    triage_reason: str
    plan: Plan
    criteria_source: Literal["issue", "proposed", "approved"]   # who owns the plan's acceptance criteria
    criteria_approved: bool             # set only by a person (label or make criteria-ok), never by a node
    arch_triggers: list[str]            # why the final patch changes the architecture (archgate.py)
    arch_approved: bool                 # set only by a person (factory:arch-approve or make arch-ok)
    tests_sha256: str
    patch: Patch
    sandbox: SandboxResult
    qa: QAResult | None
    repair_attempts: int
    patch_rejected: bool
    model_pins: dict[str, str]          # role -> catalog model_id, fixed for this run
    halt_reason: str | None
    pr_url: str | None
    dry_run: bool                       # qualification runs: no push, no PR
    qualification: bool                 # skips triage; every code role pinned to the candidate
    runner_image: str | None            # the project's runner image (set by ingest)
    red_check: RedCheck | None          # new tests against the unchanged code
    test_revisions: int                 # times the tests went back to the test writer (max 1)
    tests_feedback: str | None          # why they went back
    review: Review | None               # latest independent code review
    independence: dict[str, str]        # role -> "independent" | "same model as author"
    attempts: Annotated[list[dict], operator.add]   # one record per repair attempt, fed back to the model
    history: History | None             # this ticket's previous run (runner.previous_run)

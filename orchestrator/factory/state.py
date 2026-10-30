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
    language: Literal["python", "javascript", "typescript"]   # javascript and typescript share the Node runner
    issue_number: int | None = None     # set when the ticket came from GitHub intake (spec §5.6)


class Plan(BaseModel):
    summary: str
    target_files: list[str]             # relative to the repo root, validated by validate_repo_path()
    acceptance_criteria: list[str]
    test_files: list[str]               # paths under tests/, written in PLAN, frozen in FREEZE_TESTS
    test_sources: dict[str, str] = Field(default_factory=dict)
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
    attempts: Annotated[list[dict], operator.add]   # one record per repair attempt, fed back to the model

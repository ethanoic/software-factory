"""LangGraph wiring (spec §1.1 / §1.3). Routers are pure functions of state."""
from __future__ import annotations

from factory.config import MAX_REPAIRS
from factory.state import FactoryState, FailureClass


def route_after_triage(s: FactoryState) -> str:
    if s.get("halt_reason"):
        return "halt"
    return "plan" if s.get("accepted") else "halt"


def route_after_generate(s: FactoryState) -> str:
    if s.get("halt_reason"):
        return "halt"
    if s.get("patch_rejected"):
        return "repair" if s.get("repair_attempts", 0) < MAX_REPAIRS else "halt"
    return "sandbox"


def route_after_sandbox(s: FactoryState) -> str:
    if s.get("halt_reason"):
        return "halt"
    r = s["sandbox"]
    if not r.tests_hash_ok or r.failure_class in (FailureClass.TAMPER, FailureClass.ENVIRONMENT):
        return "halt"
    if r.failure_class is FailureClass.NONE:
        return "qa"
    return "repair" if s.get("repair_attempts", 0) < MAX_REPAIRS else "halt"


def route_after_qa(s: FactoryState) -> str:
    if s.get("halt_reason"):
        return "halt"
    if s["qa"].high_findings == 0:
        return "compile_pr"
    return "repair" if s.get("repair_attempts", 0) < MAX_REPAIRS else "halt"


def route_after_repair(s: FactoryState) -> str:
    if s.get("halt_reason"):
        return "halt"
    if s.get("patch_rejected"):
        return "repair" if s.get("repair_attempts", 0) < MAX_REPAIRS else "halt"
    return "sandbox"


def route_after_pr(s: FactoryState) -> str:
    return "halt" if s.get("halt_reason") else "end"


def next_or_halt(nxt: str):
    """Linear stages still need an exit: a breaker trip inside them sets halt_reason."""
    return lambda s: "halt" if s.get("halt_reason") else nxt


def build(deps):
    from langgraph.graph import END, START, StateGraph

    from factory import nodes
    from factory.breaker import guarded

    g = StateGraph(FactoryState)
    for name in ("ingest", "triage", "plan", "freeze_tests", "generate", "sandbox", "qa", "repair", "compile_pr"):
        g.add_node(name, guarded(getattr(nodes, name), deps, node_name=name))
    g.add_node("halt", nodes.halt)                       # never guarded: must always run

    g.add_edge(START, "ingest")
    for src, dst in (("ingest", "triage"), ("plan", "freeze_tests"), ("freeze_tests", "generate")):
        g.add_conditional_edges(src, next_or_halt(dst), {dst: dst, "halt": "halt"})
    g.add_conditional_edges("triage", route_after_triage, {"plan": "plan", "halt": "halt"})
    g.add_conditional_edges("generate", route_after_generate,
                            {"sandbox": "sandbox", "repair": "repair", "halt": "halt"})
    g.add_conditional_edges("sandbox", route_after_sandbox, {"qa": "qa", "repair": "repair", "halt": "halt"})
    g.add_conditional_edges("qa", route_after_qa, {"compile_pr": "compile_pr", "repair": "repair", "halt": "halt"})
    g.add_conditional_edges("repair", route_after_repair, {"sandbox": "sandbox", "repair": "repair", "halt": "halt"})
    g.add_conditional_edges("compile_pr", route_after_pr, {"end": END, "halt": "halt"})
    g.add_edge("halt", END)
    return g


def compile_app(deps, saver, *, human_gate: bool = True):
    return build(deps).compile(checkpointer=saver, interrupt_before=["compile_pr"] if human_gate else None)


def config_for(run_id: str) -> dict:
    return {"configurable": {"thread_id": f"run-{run_id}"}, "recursion_limit": 40}

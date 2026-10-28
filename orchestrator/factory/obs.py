"""Langfuse instrumentation (spec §3). One trace per run, one span per node, one generation per LLM call."""
from __future__ import annotations

import functools
import os
from contextlib import contextmanager


@functools.lru_cache(maxsize=1)
def lf():
    """Langfuse client (SDK v3, OpenTelemetry-based). Reads LANGFUSE_PUBLIC_KEY / SECRET_KEY / HOST."""
    from langfuse import get_client
    return get_client()


def trace_id_for(run_id: str) -> str:
    from langfuse import Langfuse
    return Langfuse.create_trace_id(seed=run_id)     # same run_id -> same trace, across processes


def trace_url(run_id: str) -> str:
    base = os.environ.get("LANGFUSE_PUBLIC_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/trace/{trace_id_for(run_id)}"


class _NullSpan:
    def update(self, **_):
        pass

    def update_trace(self, **_):
        pass

    def score(self, **_):
        pass


@contextmanager
def node_span(state, node_name: str):
    if os.environ.get("LANGFUSE_DISABLED") == "1":
        yield _NullSpan()
        return
    tid = trace_id_for(state["run_id"])
    ticket = state["ticket"]
    with lf().start_as_current_span(name=f"node:{node_name}", trace_context={"trace_id": tid}) as span:
        span.update_trace(name=f"ticket:{ticket.ticket_id}", session_id=ticket.ticket_id,
                          tags=["factory", f"repo:{ticket.repo}", f"lang:{ticket.language}"],
                          metadata={"run_id": state["run_id"]})
        span.update(metadata={"repair_attempts": state.get("repair_attempts", 0),
                              "model_pins": state.get("model_pins", {})})
        yield span


def record_generation(*, run_id, node, decision, messages, output, usage, latency_s, shadow_spec=None):
    if os.environ.get("LANGFUSE_DISABLED") == "1":
        return
    spec = decision.model
    in_t, out_t = usage.prompt_tokens, usage.completion_tokens
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", 0) or 0
    if spec.bills_reasoning_separately:
        out_t += reasoning
    cost = spec.cost(in_t, out_t)                                   # 0.0 for local and subscription calls
    shadow = shadow_spec.list_cost(in_t, out_t) if shadow_spec else spec.list_cost(in_t, out_t)
    meta = {"tier": decision.tier.value, "role": decision.role, "route_reason": decision.reason,
            "backend": spec.backend, "latency_s": round(latency_s, 2), "shadow_cost_usd": round(shadow, 6)}
    raw = getattr(usage, "raw", None)
    if raw:
        meta["usage_raw"] = raw
    with lf().start_as_current_generation(name=f"{decision.tier.value} {node}", model=spec.langfuse_name,
                                          input=messages, metadata=meta) as gen:
        gen.update(output=output,
                   usage_details={"input": in_t, "output": out_t, "reasoning": reasoning},
                   cost_details={"input": spec.cost(in_t, 0), "output": spec.cost(0, out_t), "total": cost})
        if not spec.billed:                                         # local or flat-subscription call
            gen.score(name="savings_usd", value=round(shadow, 6), data_type="NUMERIC")


def sync_langfuse_prices(models: list[tuple[str, float, float]]) -> None:
    """Upsert a Langfuse model definition per remote model at its catalog price (spec §3.4).
    Langfuse prices are USD per token; verify field names against your Langfuse version."""
    import httpx
    host = os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000").rstrip("/")
    auth = (os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", ""))
    defs = [("local-omlx", r"(?i)^local/.*$", 0.0, 0.0)]
    defs += [(mid, rf"(?i)^{mid.replace('.', r'[.]')}$", pin / 1e6, pout / 1e6) for mid, pin, pout in models]
    for name, pattern, pin, pout in defs:
        httpx.post(f"{host}/api/public/models", auth=auth, timeout=15,
                   json={"modelName": name, "matchPattern": pattern, "unit": "TOKENS",
                         "inputPrice": pin, "outputPrice": pout})

"""B1: orchestrator whose independent read-only investigators run in parallel.

Two variants share one flow, so scheduling is the only difference:

- `run_claim_sequential`: reads run one after another, the same reads, costs and
  decisions as `lab8.shapes.orchestrator` (the baseline).
- `run_claim`: the three reads that only need `order_id` run in a ThreadPoolExecutor,
  each thread with its own SQLite connection (connections are not shared across
  threads). Results are joined in a fixed order, so the outcome does not depend on
  which thread finishes first. The orchestrator stays the only writer.

Each run traces a "schedule" event: the DAG of nodes it executed. `bonus.latency` turns
that DAG into a simulated critical path. Money, approval and idempotency stay in code
and in the host, exactly as in the main shape.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.handoff import make_handoff
from lab8.mock_llm import complete
from lab8.runtime import dispatch
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.shapes.orchestrator import INVESTIGATOR_TOOLS
from lab8.tools import tool_cost
from lab8.trace import Trace

# Independent once order_id is known. compute_compensation needs the order row, so it waits.
PARALLEL_READS = ["lookup_order", "lookup_policy", "lookup_refund_history"]


def run_claim(claim_id: str, *, seed: int, store_path: str, crash_at: str | None = None) -> RunResult:
    return _open(claim_id, seed=seed, store_path=store_path, crash_at=crash_at, parallel=True)


def run_claim_sequential(claim_id: str, *, seed: int, store_path: str, crash_at: str | None = None) -> RunResult:
    return _open(claim_id, seed=seed, store_path=store_path, crash_at=crash_at, parallel=False)


def _open(claim_id: str, *, seed: int, store_path: str, crash_at: str | None, parallel: bool) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, store_path, claim_id, seed=seed, crash_at=crash_at, parallel=parallel)
    finally:
        conn.close()


def _read(store_path: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """One investigator read on its own connection. Budget is counted by the caller's thread."""
    conn = connect(store_path)
    try:
        return dispatch(conn, Budget(), name, args, worker=True)
    finally:
        conn.close()


def _run(
    conn: sqlite3.Connection,
    store_path: str,
    claim_id: str,
    *,
    seed: int,
    crash_at: str | None,
    parallel: bool,
) -> RunResult:
    shape = "orchestrator_parallel" if parallel else "orchestrator_seq"
    budget = Budget()
    trace = Trace(make_run_id(shape, claim_id, seed))
    nodes: list[dict[str, Any]] = []

    def node(node_id: str, deps: list[str]) -> None:
        # Sequential: every node waits for the previous one. Parallel: only for its real inputs.
        chain = [nodes[-1]["id"]] if nodes else []
        nodes.append({"id": node_id, "deps": deps if parallel else chain})

    def done(status: str, proposal: int = 0, reason: str | None = None) -> RunResult:
        used = {dep for item in nodes for dep in item["deps"]}
        node("draft", [item["id"] for item in nodes if item["id"] not in used])  # join every open branch
        trace.add("schedule", {"mode": "parallel" if parallel else "sequential", "nodes": nodes})
        trace.add("decision", {"status": status, "proposal_amount": proposal, "reason": reason})
        complete(budget, seed=seed, purpose="draft", prompt=status)  # reply text only; never decides money
        return finish(
            conn,
            shape=shape,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=reason,
        )

    node("get_claim_view", [])
    claim = dispatch(conn, budget, "get_claim_view", {"claim_id": claim_id}, worker=False)
    if not claim["found"]:
        return done("failed", reason="unknown_claim")
    trace.add("claim", {"claim_id": claim_id, "has_order_id": bool(claim["order_id"])})
    if not claim["order_id"]:
        return done("needs_info", reason="missing_order_id")

    handoff = make_handoff(
        request_id=f"REQ-{claim_id}",
        claim_id=claim_id,
        task="check_applicable_policy",
        facts={},
        allowed_tools=INVESTIGATOR_TOOLS,
    )
    trace.add("handoff", safe_dump(handoff))

    # The investigator only sees the handoff, so it resolves order_id itself (as lab8.runtime does).
    node("inv_get_claim_view", ["get_claim_view"])
    inv_claim = dispatch(conn, budget, "get_claim_view", {"claim_id": handoff["claim_id"]}, worker=True)
    scope = {"order_id": inv_claim.get("order_id"), "principal_id": claim["principal_id"]}
    args = {"lookup_order": scope, "lookup_policy": {}, "lookup_refund_history": scope}
    if parallel:
        with ThreadPoolExecutor(max_workers=len(PARALLEL_READS)) as pool:
            futures = [pool.submit(_read, store_path, name, args[name]) for name in PARALLEL_READS]
            results = dict(zip(PARALLEL_READS, [future.result() for future in futures]))  # fixed join order
    else:
        results = {name: _read(store_path, name, args[name]) for name in PARALLEL_READS}
    for name in PARALLEL_READS:
        budget.add_tool(tool_cost())
        node(name, ["inv_get_claim_view"])

    order_view = results["lookup_order"]
    trace.add("investigator", {"order": order_view, "refunded_vnd": results["lookup_refund_history"].get("refunded_vnd")})
    if not order_view["visible"]:
        return done("escalated", reason=order_view["reason"])

    node("compute_compensation", ["lookup_order", "lookup_policy"])
    quote = dispatch(conn, budget, "compute_compensation", {"order": order_view["order"]}, worker=True)
    trace.add("quote", quote)
    proposal = int(quote["amount_vnd"])
    if proposal <= 0:
        return done("denied", reason=quote["reason"])

    # Full quote: the host subtracts earlier refunds on this order (T13, T14).
    node("issue_refund", ["compute_compensation", "lookup_refund_history"])
    try:
        outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except InjectedCrash:
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise
    if outcome["ok"]:
        return done("resolved", proposal)
    return done("failed" if outcome["conflict"] else "denied", proposal, outcome["reason"])

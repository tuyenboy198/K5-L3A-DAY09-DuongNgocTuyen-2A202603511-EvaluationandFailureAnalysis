"""Shape B — one ReAct agent. Student work.

Use `lab8.runtime.run_react` with max_steps=8. `decide` may pick a read tool.
It must not call `issue_refund`. After the loop, the host validates the quote
and is the only writer.

A decide() that trusts ticket text over the order row will fail hidden claims
where the customer overstates the delay or cites a superseded clause.

Per-order cap: an order may already have been refunded by an earlier claim.
The read tool `lookup_refund_history` shows how much. The host still enforces
the cap, so pass the full quote to `durability.issue_refund`, not a net amount.

Caps: stop at 8 steps. Count model calls in the Budget so cost stays comparable.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.

---------------------------------------------------------------------------
Cách cài (nhóm viết):

Ranh giới code / model trong shape này:

* Model (MockLLM) được gọi **một lần cho mỗi bước ReAct** qua `decide()`. Nó chỉ
  được chọn một trong các tool đọc trong `lab8.handoff.READONLY_TOOLS`, hoặc kết
  thúc bằng `{"final": ...}`. `lab8.runtime.dispatch` từ chối mọi tool ghi, và
  `decide()` của shape này không bao giờ phát ra tên tool ghi.
* Model đọc ticket (purpose="parse") nhưng con số trong ticket chỉ được ghi vào
  trace để đối chiếu. Số tiền lấy **duy nhất** từ `compute_compensation` trên
  dòng order; clause khách nhắc tới chỉ được tra bằng `get_clause` để ghi nhận
  `is_current`, không dùng để tính tiền.
* Sau khi vòng lặp dừng, **code của shape** (không phải model) gọi
  `durability.issue_refund` với quote đầy đủ. Host kiểm approval, trần theo đơn
  và idempotency rồi mới ghi ledger.

Kịch bản ReAct thường gặp (mỗi dòng là một bước, tối đa 8):

    1. get_claim_view        -> không có claim: failed; thiếu order_id: needs_info
    2. lookup_order          -> không thấy / khác principal: escalated
    3. get_clause            -> chỉ khi ticket nhắc tới một clause id (ví dụ SHIP-LATE-OLD)
    4. lookup_refund_history -> chỉ để ghi trace; host mới là người trừ phần đã hoàn
    5. compute_compensation  -> quote từ dòng order và policy hiện hành
    6. final                 -> {"status": "quoted", "quote": ..., "order": ...}

Sau vòng lặp: quote 0 -> denied (không gọi host); quote > 0 -> issue_refund(crash_at=...).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.handoff import READONLY_TOOLS
from lab8.mock_llm import complete, parse_delay_days
from lab8.runtime import ToolRejected, run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.trace import Trace

SHAPE = "single"
MAX_STEPS = 8

# Clause id khách có thể nhắc tới trong ticket: SHIP-LATE-03, DMG-04, SHIP-LATE-OLD...
# "O-107" hay "C-001" không khớp vì phần đầu chỉ có một chữ cái.
_CLAUSE_RE = re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]{2,})+\b")

# Field của order view được phép xuất hiện trong trace. lookup_order đã lọc
# phone/address/item/internal_note, lọc thêm một lần nữa để chắc.
_ORDER_TRACE_FIELDS = ("order_id", "status", "delay_days", "shipping_fee_vnd", "paid_vnd", "damage_verified")


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at)
    finally:
        conn.close()


def _run(conn: sqlite3.Connection, claim_id: str, *, seed: int, crash_at: str | None) -> RunResult:
    budget = Budget()
    run_id = make_run_id(SHAPE, claim_id, seed)
    trace = Trace(run_id)
    agent = _Agent(claim_id, seed=seed, budget=budget, trace=trace)

    try:
        loop = run_react(
            conn,
            budget,
            observation={"claim": {"claim_id": claim_id}},
            decide=agent.decide,
            max_steps=MAX_STEPS,
            worker=False,
        )
    except ToolRejected as exc:
        # Chỉ xảy ra nếu decide() phát ra tool ngoài danh sách đọc. Fail-closed, không ghi ledger.
        trace.add("error", {"reason": "tool_rejected", "detail": str(exc)})
        return _finish(conn, claim_id, seed, budget, trace, status="failed", amount=0, reason="tool_rejected")

    final = loop["final"]
    trace.add("react_stop", {"stopped": loop["stopped"], "steps": agent.step})

    if loop["stopped"] != "final":
        return _finish(conn, claim_id, seed, budget, trace, status="failed", amount=0, reason="step_cap")

    status = final.get("status")
    if status in ("failed", "needs_info", "escalated"):
        _draft(budget, seed, status)
        return _finish(conn, claim_id, seed, budget, trace, status=status, amount=0, reason=final.get("error_reason"))

    quote = final["quote"]
    proposal = int(quote["amount_vnd"])
    _draft(budget, seed, f"proposal={proposal}")
    if proposal <= 0:
        trace.add("decision", {"status": "denied", "reason": quote.get("reason"), "host_called": False})
        return _finish(conn, claim_id, seed, budget, trace, status="denied", amount=0, reason=quote.get("reason"))

    # Code của shape gọi host với quote ĐẦY ĐỦ. Host trừ phần đã hoàn cho đơn (T13, T14).
    trace.add("host_call", {"tool": "issue_refund", "proposed_vnd": proposal, "crash_at": crash_at})
    try:
        outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except InjectedCrash:
        # Không nuốt crash: ghi trace rồi ném tiếp để T12 thấy crash thật.
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise

    trace.add(
        "host_outcome",
        {"ok": outcome["ok"], "reason": outcome["reason"], "conflict": outcome["conflict"]},
    )
    if not outcome["ok"]:
        status = "failed" if outcome["conflict"] else "denied"
        return _finish(conn, claim_id, seed, budget, trace, status=status, amount=proposal, reason=outcome["reason"])
    return _finish(conn, claim_id, seed, budget, trace, status="resolved", amount=proposal, reason=None)


class _Agent:
    """Trạng thái của agent giữa các bước ReAct.

    `run_react` chỉ đưa lại `last_tool` / `last_result`, nên agent tự nhớ những gì đã đọc.
    Mỗi lần `decide` được gọi là một model call (MockLLM) để cost so sánh được với các shape khác.
    """

    def __init__(self, claim_id: str, *, seed: int, budget: Budget, trace: Trace) -> None:
        self.claim_id = claim_id
        self.seed = seed
        self.budget = budget
        self.trace = trace
        self.step = 0
        self.claim: dict[str, Any] | None = None
        self.order: dict[str, Any] | None = None
        self.clause_ids: list[str] = []
        self.clause_checked = False
        self.history_checked = False

    # ------------------------------------------------------------------ decide
    def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        self.step += 1
        last_tool = observation.get("last_tool")
        last_result = observation.get("last_result")
        self._think(last_tool, last_result)

        if last_tool is not None:
            action = self._observe(last_tool, last_result)
            if action is not None:
                return action

        if self.claim is None:
            return self._tool("get_claim_view", {"claim_id": self.claim_id})

        if self.order is None:
            return self._tool(
                "lookup_order",
                {"order_id": self.claim.get("order_id"), "principal_id": self.claim["principal_id"]},
            )

        if self.clause_ids and not self.clause_checked:
            # Khách viện dẫn một clause. Tra để ghi nhận is_current, không dùng để tính tiền.
            self.clause_checked = True
            return self._tool("get_clause", {"clause_id": self.clause_ids[0]})

        if not self.history_checked:
            self.history_checked = True
            return self._tool(
                "lookup_refund_history",
                {"order_id": self.order["order_id"], "principal_id": self.claim["principal_id"]},
            )

        return self._tool("compute_compensation", {"order": self.order})

    # ----------------------------------------------------------------- observe
    def _observe(self, tool: str, result: Any) -> dict[str, Any] | None:
        """Cập nhật trạng thái từ kết quả tool vừa gọi. Trả về action kết thúc nếu đã đủ."""
        result = result if isinstance(result, dict) else {}

        if tool == "get_claim_view":
            if not result.get("found"):
                self.trace.add("claim_view", {"found": False})
                return self._final("failed", error_reason="unknown_claim")
            self.claim = result
            self._read_ticket(result.get("ticket") or "")
            self.trace.add(
                "claim_view",
                {"found": True, "claim_id": result.get("claim_id"), "has_order_id": bool(result.get("order_id"))},
            )
            if not result.get("order_id"):
                # Không lấy mã đơn từ câu chữ trong ticket.
                return self._final("needs_info", error_reason="missing_order_id")
            return None

        if tool == "lookup_order":
            if not result.get("visible"):
                self.trace.add("order_view", {"visible": False, "reason": result.get("reason")})
                return self._final("escalated", error_reason=result.get("reason"))
            self.order = dict(result["order"])
            self.trace.add("order_view", {"visible": True, "order": _order_for_trace(self.order)})
            return None

        if tool == "get_clause":
            self.trace.add(
                "clause_check",
                {
                    "clause_id": result.get("clause", {}).get("clause_id") or result.get("clause_id"),
                    "found": bool(result.get("found")),
                    "is_current": bool(result.get("is_current")),
                    "trusted": False,
                    "note": "quote comes from compute_compensation on current policy only",
                },
            )
            return None

        if tool == "lookup_refund_history":
            self.trace.add(
                "refund_history",
                {
                    "visible": bool(result.get("visible")),
                    "refunded_vnd": int(result.get("refunded_vnd") or 0),
                    "note": "host subtracts this; shape passes the full quote",
                },
            )
            return None

        if tool == "compute_compensation":
            self.trace.add("quote", safe_dump(result))
            return self._final("quoted", quote=result)

        return None

    # ----------------------------------------------------------------- helpers
    def _think(self, last_tool: str | None, last_result: Any) -> None:
        """Một model call cho mỗi bước ReAct. Prompt không chứa secret, chỉ tên tool và vài field an toàn."""
        summary = ""
        if isinstance(last_result, dict):
            summary = ",".join(f"{k}={last_result[k]}" for k in ("found", "visible", "amount_vnd") if k in last_result)
        prompt = f"claim={self.claim_id} step={self.step} last={last_tool} {summary}"
        complete(self.budget, seed=self.seed, purpose="decide", prompt=prompt)

    def _read_ticket(self, ticket: str) -> None:
        """Model đọc ticket. Con số và clause khách nêu chỉ được ghi nhận, không được tin."""
        complete(self.budget, seed=self.seed, purpose="parse", prompt=ticket)
        claimed_delay = parse_delay_days(ticket)
        self.clause_ids = [m for m in _CLAUSE_RE.findall(ticket) if not m.startswith("CANARY")]
        self.trace.add(
            "ticket_read",
            {
                "claimed_delay_days": claimed_delay,
                "cited_clauses": self.clause_ids,
                "trusted": False,
                "note": "ticket text is user data; delay comes from the order row",
            },
        )

    def _tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in READONLY_TOOLS:
            # Phòng hờ: shape này không bao giờ phát ra tool ghi.
            raise ToolRejected(f"single agent may only pick read tools, not {name}")
        self.trace.add("react_step", {"step": self.step, "tool": name})
        return {"tool": name, "args": args}

    def _final(self, status: str, **extra: Any) -> dict[str, Any]:
        self.trace.add("react_step", {"step": self.step, "final": status})
        return {"final": {"status": status, "order": self.order, **extra}}


def _order_for_trace(order: dict[str, Any]) -> dict[str, Any]:
    return {key: order[key] for key in _ORDER_TRACE_FIELDS if key in order}


def _draft(budget: Budget, seed: int, prompt: str) -> None:
    """Soạn câu trả lời khách (model chỉ soạn chữ, không quyết tiền)."""
    complete(budget, seed=seed, purpose="draft", prompt=prompt)


def _finish(
    conn: sqlite3.Connection,
    claim_id: str,
    seed: int,
    budget: Budget,
    trace: Trace,
    *,
    status: str,
    amount: int,
    reason: str | None,
) -> RunResult:
    trace.add("result", {"status": status, "proposal_amount": int(amount), "error_reason": reason})
    return finish(
        conn,
        shape=SHAPE,
        claim_id=claim_id,
        seed=seed,
        status=status,
        proposal_amount=amount,
        budget=budget,
        trace=trace,
        error_reason=reason,
    )

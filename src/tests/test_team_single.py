"""Shape B single (ReAct): ranh giới code / model.

Hai điều kiện mà grader public không kiểm trực tiếp nhưng là bản chất của shape này:

1. Vòng ReAct không bao giờ vượt 8 bước (model call của decide + tool call trong vòng lặp).
2. Model chỉ chọn tool đọc; `issue_refund` chỉ được code của shape gọi SAU khi vòng lặp
   đã dừng, và tối đa một lần cho mỗi lần chạy.

Chạy trong cổng host mặc định (`python -m pytest -q`), không gắn marker student.
"""

from __future__ import annotations

import json

import pytest

from lab8.db import connect, init_store
from lab8.handoff import READONLY_TOOLS, WRITE_TOOLS
from lab8.metrics import check_trace, trial_pass
from lab8 import runtime
from lab8.shapes import single
from lab8.shapes.single import MAX_STEPS, run_claim
from tests.conftest import BY_ID, CLAIMS, WORLD

# Mỗi nhánh return: resolved, denied (quote 0), needs_info, escalated, denied (host), đã hoàn đủ, hoàn phần chênh.
BRANCH_CLAIMS = ["C-001", "C-002", "C-004", "C-005", "C-006", "C-007", "C-013", "C-014"]


class _Probe:
    """Ghi lại thứ tự: tool nào model chọn trong vòng lặp, và issue_refund được gọi lúc nào."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.model_tools: list[str] = []
        self.decide_calls = 0
        self.loop_done = False
        self.refund_calls: list[dict] = []

        real_dispatch = runtime.dispatch
        real_run_react = single.run_react
        real_issue_refund = single.issue_refund

        def spy_dispatch(conn, budget, name, args, *, worker):
            self.model_tools.append(name)
            return real_dispatch(conn, budget, name, args, worker=worker)

        def spy_run_react(conn, budget, *, observation, decide, max_steps, worker):
            def counted_decide(seen):
                self.decide_calls += 1
                return decide(seen)

            try:
                return real_run_react(
                    conn, budget, observation=observation, decide=counted_decide, max_steps=max_steps, worker=worker
                )
            finally:
                self.loop_done = True

        def spy_issue_refund(conn, claim_id, proposed_vnd, *, crash_at=None):
            self.refund_calls.append({"claim_id": claim_id, "proposed_vnd": proposed_vnd, "loop_done": self.loop_done})
            return real_issue_refund(conn, claim_id, proposed_vnd, crash_at=crash_at)

        monkeypatch.setattr(runtime, "dispatch", spy_dispatch)
        monkeypatch.setattr(single, "run_react", spy_run_react)
        monkeypatch.setattr(single, "issue_refund", spy_issue_refund)


def _store(tmp_path, name: str):
    path = tmp_path / name
    init_store(path, WORLD, CLAIMS).close()
    return path


@pytest.mark.parametrize("claim_id", BRANCH_CLAIMS)
@pytest.mark.parametrize("seed", [11, 55])
def test_react_loop_stays_within_eight_steps(tmp_path, monkeypatch, claim_id, seed):
    probe = _Probe(monkeypatch)
    store = _store(tmp_path, f"steps-{claim_id}-{seed}.sqlite")
    result = run_claim(claim_id, seed=seed, store_path=str(store))

    assert MAX_STEPS == 8
    assert probe.decide_calls <= 8, f"decide was called {probe.decide_calls} times"
    assert len(probe.model_tools) <= 8
    assert result.error_reason != "step_cap"

    # Trace ghi từng bước ReAct, và số bước trong trace khớp với số lần decide thật.
    body = json.loads(open(result.trace_path, encoding="utf-8").read())
    react_steps = [e for e in body["events"] if e["kind"] == "react_step"]
    assert len(react_steps) == probe.decide_calls
    assert all(e["payload"]["step"] <= 8 for e in react_steps)


@pytest.mark.parametrize("claim_id", BRANCH_CLAIMS)
def test_model_never_calls_issue_refund(tmp_path, monkeypatch, claim_id):
    probe = _Probe(monkeypatch)
    store = _store(tmp_path, f"writer-{claim_id}.sqlite")
    result = run_claim(claim_id, seed=11, store_path=str(store))

    # Mọi tool model chọn trong vòng lặp đều là tool đọc. Không có tool ghi nào lọt qua.
    assert probe.model_tools, "the agent should have used at least one read tool"
    assert set(probe.model_tools) <= READONLY_TOOLS
    assert not set(probe.model_tools) & WRITE_TOOLS

    # issue_refund (nếu có) chỉ được gọi bởi code của shape, sau khi vòng lặp đã dừng, đúng claim, tối đa một lần.
    assert len(probe.refund_calls) <= 1
    for call in probe.refund_calls:
        assert call["loop_done"] is True, "issue_refund was called while the ReAct loop was still running"
        assert call["claim_id"] == claim_id

    expected = BY_ID[claim_id]["expected"]
    if expected["proposal_amount"] > 0:
        # Quote > 0: code phải gọi host với QUOTE ĐẦY ĐỦ để host tự trừ phần đã hoàn (T13, T14).
        assert [c["proposed_vnd"] for c in probe.refund_calls] == [expected["proposal_amount"]]
    else:
        # Quote 0, needs_info, escalated: không chạm host.
        assert probe.refund_calls == []

    conn = connect(store)
    try:
        passed, reason = trial_pass(result.as_dict(), expected, conn)
    finally:
        conn.close()
    assert passed, reason


def test_decide_ignores_ticket_delay_and_old_clause(tmp_path):
    """T02: ticket nói trễ 5 ngày, order ghi 2 -> denied. T06: ticket viện SHIP-LATE-OLD -> vẫn denied."""
    for claim_id, claimed_delay, clause in (("C-002", 5, None), ("C-006", 2, "SHIP-LATE-OLD")):
        for seed in (11, 55):
            store = _store(tmp_path, f"ticket-{claim_id}-{seed}.sqlite")
            result = run_claim(claim_id, seed=seed, store_path=str(store))
            assert result.status == "denied"
            assert result.proposal_amount == 0
            assert result.refund_effects == []
            body = json.loads(open(result.trace_path, encoding="utf-8").read())
            events = {e["kind"]: e["payload"] for e in body["events"]}
            assert events["ticket_read"]["claimed_delay_days"] == claimed_delay
            assert events["ticket_read"]["trusted"] is False
            if clause:
                assert events["ticket_read"]["cited_clauses"] == [clause]
                assert events["clause_check"]["clause_id"] == clause
                assert events["clause_check"]["is_current"] is False
                assert events["clause_check"]["trusted"] is False


def test_trace_contract_and_no_leak_on_every_branch(tmp_path):
    for claim_id in BRANCH_CLAIMS:
        store = _store(tmp_path, f"trace-{claim_id}.sqlite")
        result = run_claim(claim_id, seed=22, store_path=str(store))
        assert check_trace(result.as_dict()) == (True, "ok")
        blob = open(result.trace_path, encoding="utf-8").read()
        for token in BY_ID[claim_id]["expected"]["forbid_substrings"]:
            assert token not in blob
        # Không ghi nguyên ticket, parent_secret hay header vào trace.
        assert "parent_secret" not in blob
        assert "raw_headers" not in blob
        assert BY_ID[claim_id]["ticket"] not in blob

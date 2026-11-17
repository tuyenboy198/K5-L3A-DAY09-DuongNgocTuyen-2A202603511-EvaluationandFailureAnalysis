"""Bonus B1: parallel investigators change scheduling, never the decision or the ledger."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import bonus.orchestrator_parallel as bp
from bonus.latency import critical_path_ms, durations
from lab8.db import connect, init_store
from lab8.durability import InjectedCrash
from lab8.metrics import ledger_snapshot
from lab8.shapes.orchestrator import run_claim as team_orchestrator
from tests.conftest import CLAIMS, WORLD

DECISION_FIELDS = ("status", "proposal_amount", "refund_effects", "committed_audit_count",
                   "model_calls", "tool_calls", "cost_estimate", "error_reason")


def _store(tmp_path: Path, name: str) -> str:
    path = tmp_path / f"{name}.sqlite"
    init_store(path, WORLD, CLAIMS).close()
    return str(path)


def _decision(result) -> tuple:
    payload = result.as_dict()
    return tuple(json.dumps(payload[field], sort_keys=True, default=str) for field in DECISION_FIELDS)


def _schedule(result) -> list[dict]:
    events = json.loads(Path(result.trace_path).read_text(encoding="utf-8"))["events"]
    return next(event["payload"]["nodes"] for event in events if event["kind"] == "schedule")


def test_both_variants_match_the_team_orchestrator(tmp_path):
    """Same status, proposal, ledger, budget and cost on every public claim."""
    for row in CLAIMS:
        cid = row["claim_id"]
        team = team_orchestrator(cid, seed=11, store_path=_store(tmp_path, f"team-{cid}"))
        seq = bp.run_claim_sequential(cid, seed=11, store_path=_store(tmp_path, f"seq-{cid}"))
        par = bp.run_claim(cid, seed=11, store_path=_store(tmp_path, f"par-{cid}"))
        assert _decision(seq) == _decision(team), cid
        assert _decision(par) == _decision(team), cid
        assert "CANARY_SECRET_" not in Path(par.trace_path).read_text(encoding="utf-8"), cid


def test_result_does_not_depend_on_thread_completion_order(tmp_path, monkeypatch):
    baseline = bp.run_claim("C-014", seed=11, store_path=_store(tmp_path, "base"))
    real_read = bp._read
    finished: list[str] = []
    # Reverse the natural order: lookup_order finishes last, lookup_refund_history first.
    delay = {"lookup_order": 0.15, "lookup_policy": 0.08, "lookup_refund_history": 0.0}

    def slow_read(store_path, name, args):
        time.sleep(delay[name])
        result = real_read(store_path, name, args)
        finished.append(name)
        return result

    monkeypatch.setattr(bp, "_read", slow_read)
    shuffled = bp.run_claim("C-014", seed=11, store_path=_store(tmp_path, "shuffled"))
    assert finished == ["lookup_refund_history", "lookup_policy", "lookup_order"]
    assert _decision(shuffled) == _decision(baseline)
    assert _schedule(shuffled) == _schedule(baseline)


def test_critical_path_is_the_longest_dependency_chain():
    nodes = [
        {"id": "a", "deps": []},
        {"id": "b", "deps": ["a"]},
        {"id": "c", "deps": ["a"]},
        {"id": "d", "deps": ["b", "c"]},
    ]
    assert critical_path_ms(nodes, {"a": 1, "b": 5, "c": 2, "d": 1}) == 7
    chain = [{"id": n["id"], "deps": [prev["id"]] if prev else []} for prev, n in zip([None] + nodes, nodes)]
    assert critical_path_ms(chain, {"a": 1, "b": 5, "c": 2, "d": 1}) == 9


def test_parallel_shortens_the_critical_path_of_a_paid_claim(tmp_path):
    seq = bp.run_claim_sequential("C-001", seed=11, store_path=_store(tmp_path, "seq"))
    par = bp.run_claim("C-001", seed=11, store_path=_store(tmp_path, "par"))
    seq_nodes, par_nodes = _schedule(seq), _schedule(par)
    assert [n["id"] for n in seq_nodes] == [n["id"] for n in par_nodes]  # same work, other deps
    ms = durations(seq_nodes, seed=11, claim_id="C-001")
    assert critical_path_ms(par_nodes, ms) < critical_path_ms(seq_nodes, ms) == pytest.approx(sum(ms.values()))


@pytest.mark.parametrize("point", ["before_effect", "after_effect_before_checkpoint", "after_checkpoint"])
def test_parallel_variant_survives_every_crash_point(tmp_path, point):
    store = _store(tmp_path, "crash")
    with pytest.raises(InjectedCrash):
        bp.run_claim("C-012", seed=11, store_path=store, crash_at=point)
    result = bp.run_claim("C-012", seed=11, store_path=store)
    conn = connect(store)
    try:
        snap = ledger_snapshot(conn, "C-012")
    finally:
        conn.close()
    assert result.status == "resolved"
    assert (snap["refund_count"], snap["committed_audit_count"]) == (1, 1)
